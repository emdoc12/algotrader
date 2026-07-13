"""Always-on web + YouTube research tools (no API key required).

Lets the desks browse the open web and read what traders/influencers teach, so
they can discover and learn ANY strategy — not just the built-ins. All handlers
degrade gracefully (return {"error": ...}) and cap payload size.

Note: YouTube transcript fetches are often IP-blocked from datacenter ranges but
work from a residential IP (e.g. a home Unraid server).
"""
from __future__ import annotations

import re
import urllib.parse

from .base import http_text

NAME = "web"

_UA = {"User-Agent": "Mozilla/5.0 (compatible; daytrader-research/1.0)"}
_MAX_PAGE_CHARS = 6000
_MAX_RESULTS = 8


def is_configured() -> bool:
    return True  # no key needed; always available


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#39;|&apos;", "'", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return re.sub(r"\s+", " ", text).strip()


def _ddg_unwrap(href: str) -> str:
    """DuckDuckGo html results wrap the target in a redirect with ?uddg=."""
    try:
        q = urllib.parse.urlparse(href).query
        u = urllib.parse.parse_qs(q).get("uddg", [""])[0]
        return urllib.parse.unquote(u) or href
    except Exception:  # noqa: BLE001
        return href


def _video_id(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", s)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def web_search(inp: dict) -> dict:
    query = (inp or {}).get("query", "").strip()
    if not query:
        return {"error": "query required"}
    body = http_text("https://html.duckduckgo.com/html/",
                     params={"q": query}, headers=_UA, timeout=12)
    if not body:
        return {"error": "search failed"}
    results = []
    for m in re.finditer(r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body):
        href = _ddg_unwrap(m.group(1))
        title = _strip_html(m.group(2))
        if title and href.startswith("http"):
            results.append({"title": title[:160], "url": href})
        if len(results) >= _MAX_RESULTS:
            break
    return {"query": query, "count": len(results), "results": results}


def web_fetch(inp: dict) -> dict:
    url = (inp or {}).get("url", "").strip()
    if not url:
        return {"error": "url required"}
    if not url.startswith("http"):
        url = "https://" + url
    # enforce_public: this URL is agent-supplied, so block SSRF into the LAN,
    # loopback, or cloud metadata endpoints.
    body = http_text(url, headers=_UA, timeout=14, enforce_public=True)
    if not body:
        return {"error": "fetch failed or blocked (non-public URL)"}
    title = ""
    mt = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    if mt:
        title = _strip_html(mt.group(1))[:200]
    text = _strip_html(body)
    truncated = len(text) > _MAX_PAGE_CHARS
    return {"url": url, "title": title, "text": text[:_MAX_PAGE_CHARS], "truncated": truncated}


def youtube_search(inp: dict) -> dict:
    query = (inp or {}).get("query", "").strip()
    if not query:
        return {"error": "query required"}
    body = http_text("https://www.youtube.com/results",
                     params={"search_query": query}, headers=_UA, timeout=12)
    if not body:
        return {"error": "search failed"}
    seen, results = set(), []
    for m in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})".*?"text":"([^"]{3,120})"', body):
        vid, title = m.group(1), m.group(2)
        if vid in seen:
            continue
        seen.add(vid)
        results.append({"title": title, "video_id": vid,
                        "url": f"https://www.youtube.com/watch?v={vid}"})
        if len(results) >= _MAX_RESULTS:
            break
    return {"query": query, "count": len(results), "results": results}


def youtube_transcript(inp: dict) -> dict:
    vid = _video_id((inp or {}).get("url") or (inp or {}).get("video_id") or "")
    if not vid:
        return {"error": "provide a YouTube url or video_id"}
    # Fetch the timedtext transcript track (English). Often IP-blocked from
    # datacenter ranges; works from a residential IP.
    xml = http_text("https://www.youtube.com/api/timedtext",
                    params={"lang": "en", "v": vid}, headers=_UA, timeout=12)
    if not xml:
        return {"video_id": vid, "error": "transcript unavailable (may be IP-blocked from datacenter; works from a residential IP)"}
    parts = re.findall(r"(?s)<text[^>]*>(.*?)</text>", xml)
    text = _strip_html(" ".join(parts))
    if not text:
        return {"video_id": vid, "error": "no transcript text found"}
    truncated = len(text) > _MAX_PAGE_CHARS
    return {"video_id": vid, "text": text[:_MAX_PAGE_CHARS], "truncated": truncated}


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #
def get_tools() -> list[dict]:
    return [
        {"name": "web_search",
         "description": "Search the open web (DuckDuckGo). Returns titles, urls, and snippets. Use to research strategies, news, or context.",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "web_fetch",
         "description": "Fetch a web page and return its readable text (truncated). Use to read an article/strategy writeup after web_search.",
         "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
        {"name": "youtube_search",
         "description": "Search YouTube for strategy/trading videos. Returns titles and video ids/urls. Use to find how traders/influencers run a setup.",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        {"name": "youtube_transcript",
         "description": "Fetch a YouTube video's transcript text so you can learn the strategy. Pass a url or video_id.",
         "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "video_id": {"type": "string"}}}},
    ]


def get_handlers() -> dict:
    return {
        "web_search": web_search,
        "web_fetch": web_fetch,
        "youtube_search": youtube_search,
        "youtube_transcript": youtube_transcript,
    }
