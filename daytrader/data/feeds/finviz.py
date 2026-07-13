"""Finviz Elite screener adapter (read-only) via the CSV export endpoint.

RECONSTRUCTED after the source was lost to a .gitignore issue — verify the
export URL/filters against your Finviz Elite account if a call misbehaves.
Degrades gracefully. Auth: auth token on the export URL. Env: FINVIZ_AUTH_TOKEN.
"""
from __future__ import annotations

import csv
import io

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


def get_tools() -> list[dict]:
    return [
        {"name": "finviz_screener",
         "description": ("Run a Finviz Elite screener and get the matching tickers (top rows). "
                         "'filters' is a comma-separated Finviz filter string (e.g. "
                         "'sh_avgvol_o500,ta_change_u5' = avg vol >500k AND up >5%); 'order' "
                         "sorts (e.g. '-change' = biggest gainers, 'change' = losers)."),
         "input_schema": {"type": "object", "properties": {
             "filters": {"type": "string"}, "order": {"type": "string"}}}},
    ]


def get_handlers() -> dict:
    return {"finviz_screener": _screener}
