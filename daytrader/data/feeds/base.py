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

# Providers front their APIs with Cloudflare, which bans the stdlib's default
# "Python-urllib/3.x" User-Agent outright (error 1010, browser_signature_banned).
# That surfaces as a 403 BEFORE the request ever reaches the API, so it looks
# exactly like an auth or plan failure and sends you off checking a key that was
# never wrong. Sending a real UA — the same one the bar loader has always used —
# gets the request to the API, where a genuine 401/403 means what it says.
_UA = "Mozilla/5.0 (compatible; daytrader/1.0)"


def _with_ua(headers: dict | None) -> dict:
    h = dict(headers or {})
    if not any(k.lower() == "user-agent" for k in h):
        h["User-Agent"] = _UA
    return h


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
        req = urllib.request.Request(url, headers=_with_ua(headers))
        # NB: the urllib.request MODULE exposes urlopen(), not open(); only an
        # OpenerDirector (the SSRF-guarded opener) has .open().
        opener = _SAFE_OPENER.open if enforce_public else urllib.request.urlopen
        raw = opener(req, timeout=timeout).read(_MAX_BYTES)
        data = json.loads(raw)
        _CACHE[url] = (now, data)
        return data
    except urllib.error.HTTPError as e:  # noqa: PERF203
        try:
            body = e.read().decode("utf-8", "ignore")[:300]
        except Exception:  # noqa: BLE001
            body = ""
        out = diagnose_http(e.code, url, body)
        record_provider_error(url, out)
        return out
    except Exception as e:  # noqa: BLE001 - network/json/timeout
        out = {"ok": False, "error_code": "network_error",
               "error": repr(e)[:200],
               "hint": "Transient network/timeout or a malformed response. Retry once; "
                       "if it persists the provider is likely down."}
        record_provider_error(url, out)
        return out


# --------------------------------------------------------------------------- #
# provider health: make an unavailable feed VISIBLE instead of silent          #
# --------------------------------------------------------------------------- #
# A bare "HTTP 403" tells a desk nothing about whether the key is missing, the
# plan lacks the endpoint, or it is simply rate-limited — so it cannot decide
# between waiting, switching source, or trading without the confluence. These
# map each status onto the one action that actually resolves it.
_KEY_ENV_BY_HOST = {
    "unusualwhales.com": ("UNUSUAL_WHALES_API_KEY", "Unusual Whales"),
    "bullflow.io": ("BULLFLOW_API_KEY", "BullFlow"),
    "polygon.io": ("POLYGON_API_KEY", "Polygon"),
    "quiverquant.com": ("QUIVER_API_KEY", "Quiver"),
    "finviz.com": ("FINVIZ_AUTH_TOKEN", "Finviz Elite"),
}

# provider -> {code, error, ts} for the most recent failure.
_PROVIDER_HEALTH: dict[str, dict] = {}


def _provider_for(url: str) -> tuple[str, str | None]:
    host = ""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        pass
    for suffix, (env_key, label) in _KEY_ENV_BY_HOST.items():
        if host == suffix or host.endswith("." + suffix):
            return label, env_key
    return host or "unknown", None


def diagnose_http(code: int, url: str, body: str = "") -> dict:
    """Turn a status code into an actionable error the desk can act on."""
    label, env_key = _provider_for(url)
    has_key = bool(os.environ.get(env_key)) if env_key else None
    base = {"ok": False, "provider": label, "http_status": code,
            "detail": (body or "")[:300]}
    blob = (body or "").lower()
    if "cloudflare" in blob or "error_code\":1010" in blob or "browser_signature_banned" in blob:
        return {**base, "error_code": "blocked_by_cdn",
                "error": (f"{label}'s CDN blocked the request before it reached the API "
                          f"({code}) — this is NOT an auth or plan problem."),
                "hint": ("The provider's CDN rejected the client signature (Cloudflare "
                         "1010). The API key is irrelevant here. Usually a User-Agent "
                         "issue — daytrader sends a real one; if this appears, the "
                         "provider has tightened bot rules.")}
    if code in (401, 403):
        if env_key and not has_key:
            return {**base, "error_code": "missing_credentials",
                    "error": f"{label} rejected the request ({code}) and no API key is set.",
                    "hint": f"Add {env_key} in the dashboard Settings tab."}
        return {**base, "error_code": "auth_or_plan",
                "error": (f"{label} returned {code} WITH a key present — the key is "
                          "invalid/expired, or your plan does not include this endpoint."),
                "hint": (f"Check the {label} subscription covers this endpoint, then "
                         f"re-issue {env_key} in Settings. This is NOT a transient error; "
                         "retrying will not help.")}
    if code == 429:
        return {**base, "error_code": "rate_limited",
                "error": f"{label} rate limit hit ({code}).",
                "hint": "Back off and retry later; results are cached for 30s per URL."}
    if code == 404:
        return {**base, "error_code": "not_found",
                "error": f"{label} has no such endpoint or symbol ({code}).",
                "hint": "Check the ticker; the provider may not cover it."}
    if 500 <= code < 600:
        return {**base, "error_code": "provider_down",
                "error": f"{label} server error ({code}).",
                "hint": "Provider-side; retry later."}
    return {**base, "error_code": f"http_{code}", "error": f"{label} returned HTTP {code}."}


def record_provider_error(url: str, err: dict) -> None:
    label, _ = _provider_for(url)
    _PROVIDER_HEALTH[label] = {
        "error_code": err.get("error_code"), "error": err.get("error"),
        "hint": err.get("hint"), "http_status": err.get("http_status"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }


def provider_health() -> dict:
    """Configured providers plus the most recent failure for each, if any.

    Surfaced in the snapshot so a desk knows a confluence source is unavailable
    BEFORE it builds a plan around it, rather than discovering it mid-cycle.
    """
    out = {"configured": configured_providers(), "degraded": {}}
    for label, info in _PROVIDER_HEALTH.items():
        out["degraded"][label] = info
    if out["degraded"]:
        out["note"] = ("These data sources failed on their last call. An "
                       "'auth_or_plan' or 'missing_credentials' code will NOT fix itself "
                       "— do not gate an entry on confluence you cannot fetch; either "
                       "trade the setup on its own merits and say so, or stand aside.")
    return out


def http_text(url: str, params: dict | None = None, headers: dict | None = None,
              timeout: float = 12.0, enforce_public: bool = False) -> str | None:
    """Defensive GET returning raw text (for CSV exports etc.). None on failure.
    Byte-capped; ``enforce_public`` guards agent-supplied URLs against SSRF."""
    try:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if enforce_public and not safe_public_url(url):
            return None
        req = urllib.request.Request(url, headers=_with_ua(headers))
        opener = _SAFE_OPENER.open if enforce_public else urllib.request.urlopen
        return opener(req, timeout=timeout).read(_MAX_BYTES).decode("utf-8", "ignore")
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
    "daytrader.data.feeds.alphavantage",
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
