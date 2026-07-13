"""Shared contract + helpers for external market-data providers.

Each provider (Polygon, Unusual Whales, Quiver, Finviz, BullFlow, plus the
always-on web/YouTube tools) lives in its own module and exposes the SAME small
interface, so the agent desks can call any configured source as an on-demand
research tool. Nothing here trades — these are read-only data lookups the models
use to hunt for an edge (options flow, news, screeners, congressional/insider
activity, etc.).

A provider module MUST define:

    NAME: str                       # short id, e.g. "polygon"
    def is_configured() -> bool     # True iff its API key/token is in the env
    def get_tools() -> list[dict]    # Anthropic-style tool schemas; names MUST be
                                     # prefixed with the provider id (e.g. "uw_flow")
    def get_handlers() -> dict[str, Callable[[dict], dict]]  # tool name -> handler

Every handler takes the tool input dict and returns a JSON-serializable dict.
Handlers MUST be defensive: never raise, return {"error": "..."} on failure,
and keep payloads compact (top-N rows) so they don't blow the model's context.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

_CACHE: dict[str, tuple[float, Any]] = {}

# Cap every response read so a giant/hostile page can't OOM the container.
_MAX_BYTES = 2 * 1024 * 1024
_SSRF_MSG = "blocked: URL resolves to a private/loopback/link-local address"


def env(*names: str) -> str | None:
    """First non-empty value among the given env var names."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _host_is_public(host: str) -> bool:
    """True only if every resolved address for host is a public unicast IP."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return bool(infos)


def safe_public_url(url: str) -> bool:
    """http(s) scheme + a host that resolves only to public addresses. Used to
    guard agent-supplied URLs (web_fetch) against SSRF into the LAN / metadata."""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    return _host_is_public(u.hostname)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        if not safe_public_url(newurl):
            raise urllib.error.URLError(_SSRF_MSG)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirect)


def http_json(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 12.0,
    cache_ttl: float = 30.0,
    enforce_public: bool = False,
) -> Any:
    """Defensive GET returning parsed JSON (dict/list) or {"error": ...}.

    Short-TTL cached by full URL so repeated lookups in one cycle are cheap and
    don't hammer provider rate limits. Never raises. Reads are byte-capped. Set
    ``enforce_public`` for agent-supplied URLs to block SSRF to private hosts.
    """
    try:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if enforce_public and not safe_public_url(url):
            return {"error": _SSRF_MSG}
        now = time.time()
        hit = _CACHE.get(url)
        if hit and now - hit[0] < cache_ttl:
            return hit[1]
        req = urllib.request.Request(url, headers=headers or {})
        opener = _SAFE_OPENER if enforce_public else urllib.request
        raw = opener.open(req, timeout=timeout).read(_MAX_BYTES)
        data = json.loads(raw)
        _CACHE[url] = (now, data)
        return data
    except urllib.error.HTTPError as e:  # noqa: PERF203
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:  # noqa: BLE001
            body = ""
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:  # noqa: BLE001 - network/json/timeout
        return {"error": repr(e)[:200]}


def http_text(url: str, params: dict | None = None, headers: dict | None = None,
              timeout: float = 12.0, enforce_public: bool = False) -> str | None:
    """Defensive GET returning raw text (for CSV exports etc.). None on failure.
    Byte-capped; ``enforce_public`` guards agent-supplied URLs against SSRF."""
    try:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if enforce_public and not safe_public_url(url):
            return None
        req = urllib.request.Request(url, headers=headers or {})
        opener = _SAFE_OPENER if enforce_public else urllib.request
        return opener.open(req, timeout=timeout).read(_MAX_BYTES).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None


# Registry of provider modules (import lazily to avoid hard deps at import time).
_PROVIDER_MODULES = [
    "daytrader.data.feeds.web",
    "daytrader.data.feeds.polygon",
    "daytrader.data.feeds.unusual_whales",
    "daytrader.data.feeds.quiver",
    "daytrader.data.feeds.finviz",
    "daytrader.data.feeds.bullflow",
]


def data_tools() -> tuple[list[dict], dict[str, Callable[[dict], dict]]]:
    """Aggregate (schemas, handlers) from every CONFIGURED provider."""
    import importlib
    schemas: list[dict] = []
    handlers: dict[str, Callable[[dict], dict]] = {}
    for mod_name in _PROVIDER_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            if not mod.is_configured():
                continue
            schemas.extend(mod.get_tools())
            handlers.update(mod.get_handlers())
        except Exception as e:  # noqa: BLE001
            print(f"[feeds] skipping {mod_name}: {e}")
    return schemas, handlers


def configured_providers() -> list[str]:
    import importlib
    out = []
    for mod_name in _PROVIDER_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            if mod.is_configured():
                out.append(mod.NAME)
        except Exception:  # noqa: BLE001
            pass
    return out
