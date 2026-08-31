"""Polygon.io adapter (read-only): quotes, news, market movers.

RECONSTRUCTED after the source was lost to a .gitignore issue — verify endpoints
against https://polygon.io/docs if a call misbehaves. Degrades gracefully.
Auth: apiKey query param. Key env: POLYGON_API_KEY.
"""
from __future__ import annotations

from typing import Any

from .base import env, http_json

NAME = "polygon"
_BASE = "https://api.polygon.io"
_MAX_ROWS = 12


def _key() -> str | None:
    return env("POLYGON_API_KEY", "POLYGON_KEY")


def is_configured() -> bool:
    return bool(_key())


def _get(path: str, params: dict | None = None) -> Any:
    key = _key()
    if not key:
        return {"error": "polygon not configured (set POLYGON_API_KEY)"}
    p = dict(params or {})
    p["apiKey"] = key
    return http_json(_BASE + path, params=p)


def _quote(inp: dict) -> dict:
    sym = str((inp or {}).get("symbol", "")).upper()
    if not sym:
        return {"error": "symbol required"}
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
    if isinstance(data, dict) and data.get("error"):
        return data
    t = (data or {}).get("ticker", {}) if isinstance(data, dict) else {}
    day, last = t.get("day", {}), t.get("lastTrade", {})
    return {"symbol": sym, "price": last.get("p"), "day_open": day.get("o"),
            "day_high": day.get("h"), "day_low": day.get("l"), "volume": day.get("v"),
            "change_pct": t.get("todaysChangePerc")}


def _news(inp: dict) -> dict:
    sym = str((inp or {}).get("symbol", "")).upper()
    try:
        limit = min(int((inp or {}).get("limit", 6)), _MAX_ROWS)
    except (TypeError, ValueError):
        limit = 6
    params = {"limit": limit, "order": "desc", "sort": "published_utc"}
    if sym:
        params["ticker"] = sym
    data = _get("/v2/reference/news", params)
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in (data or {}).get("results", [])[:limit]:
        out.append({"title": r.get("title"), "publisher": (r.get("publisher") or {}).get("name"),
                    "published": r.get("published_utc"), "url": r.get("article_url"),
                    "tickers": r.get("tickers")})
    return {"symbol": sym or None, "count": len(out), "news": out}


def chain(symbol: str, min_dte: int | None = None, max_dte: int | None = None,
          strike_pct_window: float | None = None, max_expirations: int = 2,
          max_strikes: int = 16) -> dict:
    """Live option chain snapshot: bid/ask, open interest, IV and greeks.

    One call to /v3/snapshot/options/{underlying} returns every contract in the
    requested window with its quote, greeks and OI already attached — no
    separate contracts-list-then-per-contract-quote round trips. This is
    get_option_chain's Polygon fallback (dev request #28): unlike Alpha
    Vantage's HISTORICAL_OPTIONS, this endpoint is a live snapshot and does
    not depend on tastytrade's OAuth session, so it stays usable when either
    of those is down. Returns ``{"symbol", "chain", "n_contracts", "spot"}``
    shaped like tastytrade's chain, or ``{"error": ...}``.

    Field names verified against polygon.io/docs/rest/options/snapshots/
    option-chain-snapshot (2026-08-19) — a plan without options quotes/greeks
    access omits those keys rather than erroring, so every field read here is
    ``.get()``-defensive.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"error": "symbol required"}
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    lo_dte = max(0, int(min_dte)) if min_dte is not None else 0
    hi_dte = int(max_dte) if max_dte is not None else 60
    if hi_dte < lo_dte:
        hi_dte = lo_dte
    params = {
        "expiration_date.gte": (today + _td(days=lo_dte)).isoformat(),
        "expiration_date.lte": (today + _td(days=hi_dte)).isoformat(),
        "order": "asc", "sort": "strike_price", "limit": 250,
    }
    # Narrow by strike so a wide DTE window doesn't drag home the whole chain
    # (SPY alone lists thousands of contracts across all its expirations).
    spot = None
    q = _quote({"symbol": sym})
    if isinstance(q, dict) and not q.get("error"):
        spot = q.get("price")
    if spot and strike_pct_window:
        span = float(strike_pct_window) / 100.0 * float(spot)
        params["strike_price.gte"] = round(float(spot) - span, 2)
        params["strike_price.lte"] = round(float(spot) + span, 2)

    data = _get(f"/v3/snapshot/options/{sym}", params)
    if isinstance(data, dict) and data.get("error"):
        return data
    if not isinstance(data, dict):
        return {"error": "unexpected response from polygon"}
    if data.get("status") not in (None, "OK"):
        return {"error": f"polygon returned status {data.get('status')!r}"}
    results = list(data.get("results") or [])
    # A second page comfortably covers a several-percent strike window across
    # a couple of expirations; a request still hungry past that should narrow
    # its window rather than this fallback paginating indefinitely.
    next_url = data.get("next_url")
    if next_url and len(results) < 500:
        key = _key()
        page2 = http_json(next_url, params={"apiKey": key} if key else None)
        if isinstance(page2, dict) and not page2.get("error"):
            results.extend(page2.get("results") or [])

    if not results:
        return {"error": f"polygon returned no contracts for {sym} in the requested window"}

    def _dte(exp: str):
        try:
            y, m, d = (int(x) for x in exp.split("-"))
            return (_date(y, m, d) - today).days
        except Exception:  # noqa: BLE001
            return None

    exps: dict[str, list] = {}
    for r in results:
        exp = (r.get("details") or {}).get("expiration_date")
        if exp:
            exps.setdefault(exp, []).append(r)

    chain_out: dict = {}
    for exp in sorted(exps)[: max(1, int(max_expirations))]:
        rows_e = exps[exp]
        strikes = sorted({float(k) for k in
                          ((r.get("details") or {}).get("strike_price") for r in rows_e)
                          if k is not None})
        if spot:
            strikes.sort(key=lambda k: abs(k - float(spot)))
        keep = set(strikes[: max(1, int(max_strikes))])
        block = {"expiration": exp, "days_to_expiration": _dte(exp), "strikes": {}}
        for r in rows_e:
            det = r.get("details") or {}
            k = det.get("strike_price")
            if k is None or float(k) not in keep:
                continue
            side = "call" if det.get("contract_type") == "call" else "put"
            lq = r.get("last_quote") or {}
            gk = r.get("greeks") or {}
            slot = block["strikes"].setdefault(str(float(k)), {"strike": float(k)})
            slot[side] = {
                "bid": lq.get("bid"), "ask": lq.get("ask"),
                "delta": gk.get("delta"), "gamma": gk.get("gamma"),
                "theta": gk.get("theta"), "vega": gk.get("vega"),
                "iv": r.get("implied_volatility"),
                "open_interest": r.get("open_interest"),
            }
        if block["strikes"]:
            chain_out[exp] = block

    if not chain_out:
        return {"error": f"no {sym} contracts matched the requested dte/strike window"}
    n = sum(len(b["strikes"]) for b in chain_out.values())
    return {"symbol": sym, "chain": chain_out, "n_contracts": n, "spot": spot}


def next_earnings(symbol: str) -> dict:
    """Best-effort next earnings date for ``symbol`` from Polygon's Benzinga
    earnings add-on (polygon.io/docs -> Benzinga -> Earnings; verify against
    current docs if this stops returning data, same as the ``chain()``
    disclaimer above). This is a separate, OPTIONAL add-on on top of the base
    plan the options-chain fallback uses — a 403/plan error here means "this
    plan doesn't include Benzinga," not "Polygon is broken," and is reported
    as such rather than raised.

    Returns ``{"symbol", "next_earnings_date", "confirmed", "time",
    "fiscal_period", "source"}`` (``next_earnings_date`` is None with a
    ``note``/``error`` when nothing is available) — never raises.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"error": "symbol required"}
    from datetime import date as _date
    data = _get("/benzinga/v1/earnings", {
        "ticker": sym, "date.gte": _date.today().isoformat(),
        "order": "asc", "sort": "date", "limit": 1,
    })
    if isinstance(data, dict) and data.get("error"):
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "polygon_benzinga", "error": data.get("error"),
                "error_code": data.get("error_code"), "hint": data.get("hint")}
    results = (data or {}).get("results") or []
    if not results:
        return {"symbol": sym, "next_earnings_date": None, "confirmed": False,
                "source": "polygon_benzinga",
                "note": "no upcoming earnings date returned for this symbol"}
    r = results[0]
    return {"symbol": sym, "next_earnings_date": r.get("date"),
            "confirmed": bool(r.get("date_confirmed")), "time": r.get("time"),
            "fiscal_period": r.get("fiscal_period"), "source": "polygon_benzinga"}


def _next_earnings(inp: dict) -> dict:
    return next_earnings((inp or {}).get("symbol", ""))


def _movers(inp: dict) -> dict:
    direction = str((inp or {}).get("direction", "gainers")).lower()
    if direction not in ("gainers", "losers"):
        direction = "gainers"
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in (data or {}).get("tickers", [])[:_MAX_ROWS]:
        out.append({"symbol": r.get("ticker"), "change_pct": r.get("todaysChangePerc"),
                    "price": (r.get("lastTrade") or {}).get("p"),
                    "volume": (r.get("day") or {}).get("v")})
    return {"direction": direction, "count": len(out), "movers": out}


def get_tools() -> list[dict]:
    return [
        {"name": "poly_quote", "description": "Polygon.io snapshot for a ticker: last price, day OHLC, volume, % change.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
        {"name": "poly_news", "description": "Recent news articles (optionally for a ticker) from Polygon.io.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "limit": {"type": "integer"}}}},
        {"name": "poly_movers", "description": "Market movers (gainers/losers) from Polygon.io.",
         "input_schema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["gainers", "losers"]}}}},
        {"name": "poly_next_earnings",
         "description": ("Best-effort next earnings date for a ticker (Polygon's Benzinga "
                         "add-on), with a confirmed/estimated flag. get_option_chain already "
                         "attaches this for the symbol you're pricing; use this tool directly "
                         "to screen a name BEFORE spending a chain fetch on it — the single "
                         "largest tail risk in a 30-45 DTE premium-selling trade is an "
                         "earnings print inside the window."),
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    ]


def get_handlers() -> dict:
    return {"poly_quote": _quote, "poly_news": _news, "poly_movers": _movers,
            "poly_next_earnings": _next_earnings}
