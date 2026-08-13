"""Alpha Vantage adapter (read-only): HISTORICAL option chains.

This is the piece that makes options *research* possible. Live chains come from
tastytrade — real quotes and streaming Greeks on the owner's own account — but a
live chain can only tell a desk what is true right now. Backtesting a wheel, a
credit spread, or an earnings vol-crush trade needs what every strike was worth
on every past day, and `HISTORICAL_OPTIONS` returns exactly that: the full chain
for a given date with bid/ask, size, volume, open interest, implied volatility
and all five Greeks, going back roughly twenty years.

Two things shape this module:

  * **History never changes.** A chain for 2026-06-10 is the same tomorrow as it
    is today, so every fetch is cached to disk permanently and each
    (symbol, date) pair is paid for exactly once, ever. The seven desks share
    one cache directory, so the second desk to research a date pays nothing.
  * **The free tier is 25 requests/day.** That is a hard, low ceiling, so the
    cache is not an optimization here, it is what makes the feature usable at
    all. Requests are counted against a daily budget and the caller is told how
    many remain rather than being handed an opaque failure once the quota burns.

`REALTIME_OPTIONS` is deliberately not used: it is a premium endpoint, and
tastytrade already covers live chains at no extra cost.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .base import env, http_json

NAME = "alphavantage"
_BASE = "https://www.alphavantage.co/query"

# Free tier allowance. Premium plans lift this; override to match your plan.
_DAILY_LIMIT = int(os.environ.get("ALPHAVANTAGE_DAILY_LIMIT", "25"))


def _key() -> str | None:
    return env("ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY", "ALPHAVANTAGE_KEY")


def is_configured() -> bool:
    return bool(_key())


def cache_dir() -> Path:
    d = Path(os.environ.get("OPTIONS_CACHE_DIR")
             or os.environ.get("DAYTRADER_CACHE_DIR", "cache")) / "options_chains"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# request budget                                                              #
# --------------------------------------------------------------------------- #
def _budget_path() -> Path:
    return cache_dir() / "_budget.json"


def _budget() -> dict:
    today = time.strftime("%Y-%m-%d")
    try:
        b = json.loads(_budget_path().read_text())
        if b.get("date") == today:
            return b
    except Exception:  # noqa: BLE001
        pass
    return {"date": today, "used": 0}


def _spend(n: int = 1) -> None:
    b = _budget()
    b["used"] = int(b.get("used", 0)) + n
    try:
        _budget_path().write_text(json.dumps(b))
    except Exception:  # noqa: BLE001
        pass


def budget_state() -> dict:
    b = _budget()
    used = int(b.get("used", 0))
    return {"date": b.get("date"), "used": used, "limit": _DAILY_LIMIT,
            "remaining": max(0, _DAILY_LIMIT - used)}


# --------------------------------------------------------------------------- #
# historical chains                                                           #
# --------------------------------------------------------------------------- #
def _cache_file(symbol: str, date: str) -> Path:
    return cache_dir() / f"{symbol.upper()}_{date}.json"


def historical_chain(symbol: str, date: str | None = None,
                     use_cache: bool = True) -> dict:
    """Full option chain for ``symbol`` as of ``date`` (YYYY-MM-DD).

    Returns ``{"symbol", "date", "contracts": [...], "source": "cache"|"api"}``
    or ``{"error": ...}``. Never raises.

    Each contract carries: ``expiration, strike, type, bid, ask, mark, last,
    volume, open_interest, implied_volatility, delta, gamma, theta, vega, rho``.
    """
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return {"error": "symbol required"}
    date = str(date).strip()[:10] if date else ""

    if use_cache and date:
        f = _cache_file(symbol, date)
        if f.exists():
            try:
                cached = json.loads(f.read_text())
                cached["source"] = "cache"
                return cached
            except Exception:  # noqa: BLE001
                pass   # corrupt cache entry: refetch below

    key = _key()
    if not key:
        return {"error": ("alphavantage not configured — set ALPHAVANTAGE_API_KEY. "
                          "A free key takes about 20 seconds to claim at "
                          "https://www.alphavantage.co/support/#api-key")}

    state = budget_state()
    if state["remaining"] <= 0:
        return {"error": (f"alphavantage daily request budget exhausted "
                          f"({state['used']}/{state['limit']} used today). Chains "
                          "already fetched stay available from cache; new dates "
                          "resume tomorrow, or raise ALPHAVANTAGE_DAILY_LIMIT to "
                          "match a premium plan."),
                "budget": state}

    params = {"function": "HISTORICAL_OPTIONS", "symbol": symbol, "apikey": key}
    if date:
        params["date"] = date
    raw = http_json(_BASE, params=params, cache_ttl=0)
    _spend(1)
    if isinstance(raw, dict) and raw.get("error"):
        return raw
    if not isinstance(raw, dict):
        return {"error": "unexpected response from alphavantage"}
    # Alpha Vantage reports quota exhaustion and bad keys as a 200 with a note.
    for k in ("Note", "Information", "Error Message"):
        if raw.get(k):
            return {"error": str(raw[k])[:400], "budget": budget_state()}

    rows = raw.get("data") or []
    contracts = [_row(r) for r in rows]
    contracts = [c for c in contracts if c]
    out = {"symbol": symbol,
           "date": date or (contracts[0].get("date") if contracts else None),
           "n_contracts": len(contracts),
           "contracts": contracts,
           "source": "api"}
    # Write through under the date the API actually answered for, so a request
    # that resolved to "latest" is still a cache hit next time it is asked for
    # by that date.
    stamp = out.get("date")
    if stamp:
        try:
            _cache_file(symbol, str(stamp)).write_text(json.dumps(out))
        except Exception:  # noqa: BLE001
            pass
    return out


def _f(v) -> float | None:
    try:
        if v in (None, "", "None"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _row(r: dict) -> dict | None:
    try:
        return {
            "contract_id": r.get("contractID"),
            "expiration": r.get("expiration"),
            "strike": _f(r.get("strike")),
            "type": str(r.get("type") or "").lower(),
            "date": r.get("date"),
            "bid": _f(r.get("bid")), "ask": _f(r.get("ask")),
            "mark": _f(r.get("mark")), "last": _f(r.get("last")),
            "volume": _i(r.get("volume")),
            "open_interest": _i(r.get("open_interest")),
            "implied_volatility": _f(r.get("implied_volatility")),
            "delta": _f(r.get("delta")), "gamma": _f(r.get("gamma")),
            "theta": _f(r.get("theta")), "vega": _f(r.get("vega")),
            "rho": _f(r.get("rho")),
        }
    except Exception:  # noqa: BLE001
        return None


def cached_dates(symbol: str) -> list[str]:
    """Dates already on disk for ``symbol`` — free to backtest over."""
    sym = str(symbol or "").upper()
    out = []
    for f in cache_dir().glob(f"{sym}_*.json"):
        stem = f.stem[len(sym) + 1:]
        if len(stem) == 10:
            out.append(stem)
    return sorted(out)


# --------------------------------------------------------------------------- #
# agent-facing tools                                                          #
# --------------------------------------------------------------------------- #
def _t_historical(inp: dict) -> dict:
    inp = inp or {}
    res = historical_chain(inp.get("symbol", ""), inp.get("date"))
    if res.get("error"):
        return res
    contracts = res.get("contracts") or []
    # Never hand a model 3,000 contracts; filter to what was asked for.
    exp = str(inp.get("expiration") or "").strip()
    if exp:
        contracts = [c for c in contracts if c.get("expiration") == exp]
    right = str(inp.get("type") or "").lower().strip()
    if right in ("call", "put"):
        contracts = [c for c in contracts if c.get("type") == right]
    lo, hi = _f(inp.get("min_strike")), _f(inp.get("max_strike"))
    if lo is not None:
        contracts = [c for c in contracts if (c.get("strike") or 0) >= lo]
    if hi is not None:
        contracts = [c for c in contracts if (c.get("strike") or 0) <= hi]
    limit = min(int(inp.get("limit") or 40), 120)
    expirations = sorted({c.get("expiration") for c in (res.get("contracts") or []) if c.get("expiration")})
    return {"symbol": res.get("symbol"), "date": res.get("date"),
            "source": res.get("source"), "budget": budget_state(),
            "total_contracts": res.get("n_contracts"),
            "expirations_available": expirations[:30],
            "returned": len(contracts[:limit]),
            "contracts": contracts[:limit]}


def _t_budget(_inp: dict) -> dict:
    st = budget_state()
    st["cached_symbols"] = sorted({f.stem.rsplit("_", 1)[0]
                                   for f in cache_dir().glob("*_*.json")
                                   if not f.stem.startswith("_")})[:40]
    st["note"] = ("Cached chains are free and unlimited — history never changes, so "
                  "each (symbol, date) is fetched once and kept forever, shared across "
                  "all desks. Only NEW dates consume the budget.")
    return st


def get_tools() -> list[dict]:
    if not is_configured():
        return []
    return [
        {
            "name": "av_historical_option_chain",
            "description": (
                "HISTORICAL option chain for a symbol on a past date: every strike and "
                "expiration with bid/ask, volume, open interest, implied volatility and "
                "full Greeks. This is how you research an options strategy against real "
                "past prices instead of guessing — check what a 30-delta put actually "
                "paid, how IV behaved into earnings, or whether a spread would have been "
                "closable at 50% profit. Chains already fetched are cached forever and "
                "cost nothing to re-read; only new dates consume the daily budget "
                "(av_options_budget shows what is left). Filter with expiration/type/"
                "strike bounds — a full chain is thousands of contracts."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Underlying, e.g. SPY"},
                    "date": {"type": "string", "description": "YYYY-MM-DD trading date. Omit for the latest available."},
                    "expiration": {"type": "string", "description": "Filter to one expiration (YYYY-MM-DD)"},
                    "type": {"type": "string", "enum": ["call", "put"]},
                    "min_strike": {"type": "number"},
                    "max_strike": {"type": "number"},
                    "limit": {"type": "integer", "description": "Max contracts returned (default 40, cap 120)"},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "av_options_budget",
            "description": ("How many NEW historical-chain fetches remain today, and which "
                            "symbols already have cached chains (free to research)."),
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def get_handlers() -> dict[str, Any]:
    if not is_configured():
        return {}
    return {"av_historical_option_chain": _t_historical,
            "av_options_budget": _t_budget}
