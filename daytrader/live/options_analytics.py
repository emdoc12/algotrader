"""Selection-time analytics for the premium-selling lane.

get_option_chain answers "what can I trade right now" (live bid/ask, streaming
Greeks). It does not answer the question that comes BEFORE that one: is this
name's volatility actually rich, where should short strikes sit, and is there
an earnings print inside the window. Four of the five options lanes on the
mandate (wheel_csp, bull_put_spread, iron_condor, earnings_vol_crush) are
gated on exactly those questions (issue #43), so this module computes them
from data the chain fetch already paid for, rather than leaving every desk to
eyeball it or reinvent it per-strategy.

IV RANK (``iv_rank_252d`` / ``iv_pct_252d``): a real 52-week IV rank needs a
year of historical ATM IV. Rebuilding that from Alpha Vantage's
HISTORICAL_OPTIONS would cost one request per historical date against a
25/day budget already reserved for the live-chain fallback (see
alphavantage.py) — the exact cost problem issue #43 flagged as the reason no
one had built this yet. Instead of buying that history, this module RECORDS
it: every live/fallback chain fetch already computes an ATM IV to pick
near-the-money strikes, so one sample per symbol per day is appended to a
small on-disk series (free — no extra request), and today's value is ranked
against whatever has accumulated so far. Day one has one sample and says so
(``iv_sample_days: 1``); the rank becomes a real, non-fabricated 52-week
figure as the system keeps running. This is the same "pay once, keep
forever" trade alphavantage.py's chain cache already makes, just funded by
calls a desk was making anyway instead of a metered fetch.

EXPECTED MOVE: the ATM straddle mid when both legs are quoted (what the
market is actually pricing), falling back to spot * ATM_IV * sqrt(DTE/365)
when a leg's bid/ask is missing (thin quote, or a stale/historical fallback
chain that only carries a mark). Attached per expiration on the chain the
desk already asked for.

NEXT EARNINGS DATE: Polygon's Benzinga earnings add-on (polygon.py's
next_earnings). A separate optional add-on on top of the base plan the
options-chain fallback already uses — this module treats "plan doesn't
include it" the same as any other degraded source: report why, return
next_earnings_date=None, never block a desk's plan on it.

Every function here is defensive and never raises: this is selection
guidance, not execution data, and a bug in a percentile calculation must
never take down get_option_chain.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date as _date
from pathlib import Path

log = logging.getLogger("daytrader.options_analytics")

# How many daily ATM-IV samples make a "52-week" IV rank. Real time, not
# calendar days — matches the ~252 trading days a year actually has.
_LOOKBACK_DAYS = 252


def _cache_dir() -> Path:
    d = Path(os.environ.get("OPTIONS_CACHE_DIR")
             or os.environ.get("DAYTRADER_CACHE_DIR", "cache")) / "iv_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iv_file(symbol: str) -> Path:
    return _cache_dir() / f"{symbol.upper()}.json"


def _load_samples(symbol: str) -> list[dict]:
    try:
        return json.loads(_iv_file(symbol).read_text()).get("samples") or []
    except Exception:  # noqa: BLE001 - missing/corrupt cache just starts fresh
        return []


def _save_samples(symbol: str, samples: list[dict]) -> None:
    try:
        _iv_file(symbol).write_text(json.dumps({"samples": samples}))
    except Exception:  # noqa: BLE001
        pass


def _atm_iv(spot: float | None, chain: dict) -> tuple[float | None, int | None, str | None]:
    """(IV, DTE, expiration) for the strike closest to spot in the expiration
    closest to 30 DTE — the standard reference point premium-selling mandates
    quote IV rank against. Averages call/put IV where both are present."""
    if not chain:
        return None, None, None
    best_exp, best_diff = None, None
    for exp_key, block in chain.items():
        dte = block.get("days_to_expiration")
        if dte is None:
            continue
        diff = abs(dte - 30)
        if best_diff is None or diff < best_diff:
            best_diff, best_exp = diff, exp_key
    if best_exp is None:
        return None, None, None
    block = chain[best_exp]
    strikes = list((block.get("strikes") or {}).values())
    if not strikes:
        return None, block.get("days_to_expiration"), best_exp
    if spot is not None:
        closest = min(strikes, key=lambda s: abs((s.get("strike") or 0.0) - spot))
    else:
        closest = strikes[0]
    ivs = [leg.get("iv") for side in ("call", "put")
           if (leg := closest.get(side)) and leg.get("iv") is not None]
    if not ivs:
        return None, block.get("days_to_expiration"), best_exp
    return sum(ivs) / len(ivs), block.get("days_to_expiration"), best_exp


def _rank_and_percentile(values: list[float], current: float) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    rank = (current - lo) / (hi - lo) * 100.0 if hi > lo else 50.0
    pct = sum(1 for v in values if v <= current) / len(values) * 100.0
    return round(rank, 1), round(pct, 1)


def record_and_rank_iv(symbol: str, spot: float | None, chain: dict) -> dict:
    """Record today's ATM IV (once per day) and rank it against local history.

    Always returns the four fields below; each degrades to None rather than
    guessing when the chain has no resolvable ATM IV this call.
    """
    out = {"atm_iv_30d": None, "atm_iv_dte": None, "atm_iv_expiration": None,
           "iv_rank_252d": None, "iv_pct_252d": None, "iv_sample_days": 0}
    sym = str(symbol or "").upper().strip()
    if not sym:
        return out
    try:
        atm_iv, dte, exp = _atm_iv(spot, chain)
    except Exception as e:  # noqa: BLE001
        log.info("options_analytics: atm_iv for %s failed (%s)", sym, e)
        return out
    if atm_iv is None:
        return out
    out["atm_iv_30d"] = round(atm_iv, 4)
    out["atm_iv_dte"] = dte
    out["atm_iv_expiration"] = exp

    try:
        today = _date.today().isoformat()
        samples = [s for s in _load_samples(sym) if s.get("date") != today]
        samples.append({"date": today, "atm_iv": atm_iv})
        samples.sort(key=lambda s: s["date"])
        samples = samples[-_LOOKBACK_DAYS:]
        _save_samples(sym, samples)
        values = [s["atm_iv"] for s in samples if s.get("atm_iv") is not None]
        rank, pct = _rank_and_percentile(values, atm_iv)
        out["iv_rank_252d"] = rank
        out["iv_pct_252d"] = pct
        out["iv_sample_days"] = len(values)
        if len(values) < _LOOKBACK_DAYS:
            out["iv_rank_note"] = (
                f"based on {len(values)} recorded trading day(s), not a full "
                f"{_LOOKBACK_DAYS}-day year — there is no paid historical-IV source "
                "configured, so this is built for free from ATM IV seen on live/fallback "
                "chain fetches, one sample per day. It will not fabricate a year of history "
                "it doesn't have; treat with reduced confidence until iv_sample_days "
                "approaches 252.")
    except Exception as e:  # noqa: BLE001
        log.info("options_analytics: iv rank for %s failed (%s)", sym, e)
    return out


def _mid(leg: dict) -> float | None:
    bid, ask = leg.get("bid"), leg.get("ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None


def expected_move(spot: float | None, block: dict) -> dict | None:
    """Expected move for one expiration block: ATM straddle mid, cross-checked
    against spot * ATM_IV * sqrt(DTE/365). None if neither is computable."""
    if spot is None or not block:
        return None
    dte = block.get("days_to_expiration")
    strikes = list((block.get("strikes") or {}).values())
    if not strikes:
        return None
    closest = min(strikes, key=lambda s: abs((s.get("strike") or 0.0) - spot))
    call, put = closest.get("call") or {}, closest.get("put") or {}

    call_mid, put_mid = _mid(call), _mid(put)
    straddle = round(call_mid + put_mid, 4) if call_mid is not None and put_mid is not None else None

    iv_based = None
    ivs = [v for v in (call.get("iv"), put.get("iv")) if v is not None]
    if ivs and dte is not None and dte >= 0:
        atm_iv = sum(ivs) / len(ivs)
        iv_based = round(spot * atm_iv * math.sqrt(dte / 365.0), 4)

    move = straddle if straddle is not None else iv_based
    if move is None:
        return None
    return {"move": move,
            "method": "atm_straddle_mid" if straddle is not None else "atm_iv_formula",
            "straddle_mid": straddle, "iv_formula": iv_based,
            "atm_strike": closest.get("strike"),
            "low": round(spot - move, 4), "high": round(spot + move, 4)}


def next_earnings(symbol: str) -> dict | None:
    """Best-effort next-earnings lookup via Polygon's Benzinga add-on. None if
    Polygon isn't configured; never raises."""
    try:
        from daytrader.data.feeds import polygon as poly
        if not poly.is_configured():
            return None
        return poly.next_earnings(symbol)
    except Exception as e:  # noqa: BLE001
        log.info("options_analytics: next_earnings for %s failed (%s)", symbol, e)
        return None


def enrich_chain(symbol: str, spot: float | None, chain: dict) -> dict:
    """The combined payload get_option_chain / the snapshot attach: IV rank,
    expected move per expiration, and next earnings. Additive-only — every
    key here is new; nothing in the base chain response is touched."""
    out: dict = {"next_earnings_date": None, "earnings_confirmed": False,
                 "earnings_source": None}
    out.update(record_and_rank_iv(symbol, spot, chain))

    try:
        moves = {}
        for exp_key, block in (chain or {}).items():
            em = expected_move(spot, block)
            if em:
                moves[exp_key] = em
        if moves:
            out["expected_move"] = moves
    except Exception as e:  # noqa: BLE001
        log.info("options_analytics: expected_move for %s failed (%s)", symbol, e)

    earn = next_earnings(symbol)
    if earn:
        out["next_earnings_date"] = earn.get("next_earnings_date")
        out["earnings_confirmed"] = earn.get("confirmed")
        out["earnings_source"] = earn.get("source")
        if earn.get("note"):
            out["earnings_note"] = earn["note"]
        if earn.get("error"):
            out["earnings_error"] = earn["error"]
    return out
