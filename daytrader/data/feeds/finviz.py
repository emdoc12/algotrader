"""Finviz Elite screener adapter (read-only) via the CSV export endpoint.

RECONSTRUCTED after the source was lost to a .gitignore issue — verify the
export URL/filters against your Finviz Elite account if a call misbehaves.
Degrades gracefully. Auth: auth token on the export URL. Env: FINVIZ_AUTH_TOKEN.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date as _date, datetime as _dt, timedelta as _td

from .base import env, http_text

NAME = "finviz"
_EXPORT = "https://elite.finviz.com/export.ashx"
_MAX_ROWS = 20


def _token() -> str | None:
    return env("FINVIZ_AUTH_TOKEN", "FINVIZ_TOKEN")


def is_configured() -> bool:
    return bool(_token())


def _screener(inp: dict) -> dict:
    tok = _token()
    if not tok:
        return {"error": "finviz not configured (set FINVIZ_AUTH_TOKEN)"}
    filters = (inp or {}).get("filters", "")   # e.g. "sh_avgvol_o500,ta_change_u5"
    order = (inp or {}).get("order", "-change")
    # v=152 is a broad export view (ticker, company, sector, price, change, volume, etc.)
    params = {"v": "152", "auth": tok, "o": order}
    if filters:
        params["f"] = filters
    text = http_text(_EXPORT, params=params, timeout=15)
    if not text:
        return {"error": "finviz export failed (check token / filters)"}
    try:
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for r in reader:
            rows.append({k: r.get(k) for k in list(r.keys())[:10]})
            if len(rows) >= _MAX_ROWS:
                break
        return {"count": len(rows), "filters": filters, "results": rows}
    except Exception as e:  # noqa: BLE001
        return {"error": f"parse failed: {e!r}"}


def _parse_earnings_date(raw: str) -> str | None:
    """Finviz's Earnings column reads like ``"Sep 03 AMC"``, ``"Sep 03/a"`` or
    ``"-"`` when unknown — no year, and a session marker (before/after market
    close) or actual-vs-estimate flag stuck on the end. Best-effort: strip the
    marker, parse month/day, and infer the year (Finviz always shows the NEXT
    upcoming report, so a month/day that looks like it already happened this
    year belongs to next year instead)."""
    raw = (raw or "").strip()
    if not raw or raw in ("-", "N/A"):
        return None
    cleaned = re.sub(r"\b(AMC|BMO)\b", "", raw, flags=re.I)
    cleaned = re.sub(r"[/(]\s*[ae]\s*\)?", "", cleaned, flags=re.I).strip()
    today = _date.today()
    for fmt in ("%b %d %Y", "%b %d", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            d = _dt.strptime(cleaned, fmt)
        except ValueError:
            continue
        if fmt == "%b %d":
            d = d.replace(year=today.year)
            if d.date() < today - _td(days=3):
                d = d.replace(year=today.year + 1)
        return d.date().isoformat()
    return None


def next_earnings(symbol: str) -> dict:
    """Best-effort next earnings date for ``symbol`` from Finviz Elite's
    screener export (dev request #44's free fallback for when Polygon's
    Benzinga add-on isn't on the plan). ``v=161`` is Finviz's "Financial"
    screener tab, the one view whose columns include "Earnings" — unverified
    against a live account in this environment (no FINVIZ_AUTH_TOKEN configured
    here); if Finviz has renumbered/renamed it, this degrades to the explicit
    "no Earnings column" error below rather than silently returning the wrong
    field, same as the "RECONSTRUCTED, verify against your account" disclaimer
    already at the top of this file.

    Returns the same shape as polygon.py's ``next_earnings`` — never raises.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"error": "symbol required"}
    tok = _token()
    if not tok:
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "finviz", "error": "finviz not configured (set FINVIZ_AUTH_TOKEN)"}
    params = {"v": "161", "auth": tok, "t": sym}
    text = http_text(_EXPORT, params=params, timeout=15)
    if not text:
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "finviz", "error": "finviz export failed (check token)"}
    try:
        row = next(csv.DictReader(io.StringIO(text)), None)
    except Exception as e:  # noqa: BLE001
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "finviz", "error": f"parse failed: {e!r}"}
    if not row:
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "finviz", "note": f"finviz returned no row for {sym}"}
    earn_key = next((k for k in row if k and "earn" in k.lower()), None)
    if not earn_key:
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "finviz",
                "error": ("finviz view v=161 did not include an Earnings column "
                          f"(got: {list(row.keys())[:15]}) — the view id likely needs "
                          "updating; verify against your Finviz Elite account")}
    raw = (row.get(earn_key) or "").strip()
    parsed = _parse_earnings_date(raw)
    if parsed is None:
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "finviz", "note": f"no parseable earnings date (got {raw!r})"}
    return {"symbol": sym, "next_earnings_date": parsed, "confirmed": False,
            "source": "finviz", "raw": raw}


def get_tools() -> list[dict]:
    return [
        {"name": "finviz_screener",
         "description": ("Run a Finviz Elite screener and get the matching tickers (top rows). "
                         "'filters' is a comma-separated Finviz filter string (e.g. "
                         "'sh_avgvol_o500,ta_change_u5' = avg vol >500k AND up >5%); 'order' "
                         "sorts (e.g. '-change' = biggest gainers, 'change' = losers)."),
         "input_schema": {"type": "object", "properties": {
             "filters": {"type": "string"}, "order": {"type": "string"}}}},
        {"name": "fv_next_earnings",
         "description": ("Best-effort next earnings date for a ticker from Finviz Elite's "
                         "screener (dev request #44 — the free fallback poly_next_earnings "
                         "reaches for when Polygon's Benzinga add-on isn't on the plan)."),
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}},
                          "required": ["symbol"]}},
    ]


def get_handlers() -> dict:
    return {"finviz_screener": _screener,
            "fv_next_earnings": lambda inp: next_earnings((inp or {}).get("symbol", ""))}
