"""Realized-performance breakdowns for the desks.

Groups closed trades by strategy and/or time-of-day so a desk can see which
setups and which session windows actually carry positive expectancy — and
concentrate risk there. Pure functions over the trade list (no DB/network), so
they're easy to test.

Time-of-day note: trade timestamps are recorded in the container's local time
(``time.localtime``), which on the deployed image is UTC. The session buckets
the desks reason in are Eastern, so we convert each timestamp to ET before
bucketing — interpreting a naive timestamp in the system's local zone (the zone
it was written in) and converting to America/New_York.
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Session windows (ET). Bonus 'open' window is the high-alpha 9:30-10:00.
_BUCKETS = [
    ("open", dtime(9, 30), dtime(10, 0)),
    ("morning", dtime(10, 0), dtime(12, 0)),
    ("midday", dtime(12, 0), dtime(14, 0)),
    ("late", dtime(14, 0), dtime(16, 0)),
]


def _to_et(ts_str):
    """Parse a recorded timestamp and convert to ET. None on failure."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_str))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        # Written via time.localtime(); attach the system local zone, then
        # convert. astimezone() with no arg gives the local tz.
        local_tz = datetime.now().astimezone().tzinfo
        dt = dt.replace(tzinfo=local_tz)
    try:
        return dt.astimezone(ET)
    except Exception:  # noqa: BLE001
        return None


def tod_bucket(ts_str) -> str:
    """ET time-of-day bucket for a recorded timestamp."""
    dt = _to_et(ts_str)
    if dt is None:
        return "other"
    t = dt.time()
    for name, lo, hi in _BUCKETS:
        if lo <= t < hi:
            return name
    return "other"


def _stats(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp, gl = sum(wins), -sum(losses)
    return {
        "n_trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        # None = undefined (no losing trades in this group yet), not "PF = gross profit".
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


# Collapse the LLM's free-text strategy labels into the 8 canonical built-in
# buckets (+ "other"), so the breakdown isn't fragmented into ~40 near-dup rows
# like "MACD" / "macd_with_trend_short" / "MACD trend continuation".
def canonical_strategy(label) -> str:
    s = (label or "").lower()
    if "macd" in s:
        return "macd"
    if "orb" in s or "opening range" in s or "opening-range" in s or "openingrange" in s:
        return "orb"
    if "vwap" in s and ("revers" in s or "fade" in s or "mean" in s):
        return "vwap_reversion"
    if "vwap" in s:
        return "vwap_trend"
    if "ema" in s or "pullback" in s:
        return "ema_pullback"
    if "rsi" in s:
        return "rsi2"
    if "bollinger" in s or "bband" in s or "bb_" in s or "boll" in s:
        return "bollinger"
    if "pivot" in s:
        return "pivot"
    if "gap" in s:
        return "gap_fade"
    if "breakout" in s:
        return "orb"
    if "revers" in s or "mean revert" in s or "fade" in s:
        return "vwap_reversion"
    return "other"


# INVERSE ETFs — a LONG in one of these is mechanically a SHORT on the
# underlying index/sector, so "with trend" has to be judged on the EFFECTIVE
# market direction the trade expresses, not the raw order side. Leveraged-but-
# not-inverse names (TQQQ, SOXL, SPXL, UPRO, TNA) are deliberately absent: they
# track their underlying, so they keep the +1 multiplier.
_INVERSE_ETFS = {
    # broad index
    "SH", "SDS", "SPXU", "SPXS", "PSQ", "QID", "SQQQ", "DOG", "DXD", "SDOW",
    "RWM", "TWM", "TZA", "SRTY", "MYY", "MZZ",
    # sector / thematic
    "SOXS", "TECS", "FAZ", "SKF", "ERY", "DRV", "SRS", "LABD", "DUG", "SSG",
    "YANG", "FXP", "EDZ", "EUM", "DRIP", "SCO", "KOLD", "ZSL", "DUST", "JDST",
    # long-volatility (rallies when the tape falls)
    "UVXY", "VIXY", "VXX", "VIXM", "SVIX",
}


def direction_multiplier(symbol) -> int:
    """+1 for a normal instrument, -1 for an inverse ETF (long = short the tape)."""
    return -1 if (symbol or "").strip().upper() in _INVERSE_ETFS else 1


def effective_direction(symbol, side) -> str | None:
    """The market direction a trade actually expresses.

    A SQQQ *long* is a bet the tape goes DOWN, so it returns "down". Returns
    None when the side can't be read.
    """
    s = (getattr(side, "value", None) or str(side or "")).strip().lower()
    if s.startswith("l") or s == "buy":
        sign = 1
    elif s.startswith("s") or s == "sell":
        sign = -1
    else:
        return None
    sign *= direction_multiplier(symbol)
    return "up" if sign > 0 else "down"


def with_trend_label(symbol, side, spy_direction) -> str | None:
    """with_trend / counter_trend for a trade, judged on EFFECTIVE direction.

    This is what makes a SQQQ long into a *with-trend* trade on a down day
    instead of being mis-bucketed as counter-trend. Returns None when SPY's
    direction is unknown (flat/missing), matching the previous behavior.
    """
    if spy_direction not in ("up", "down"):
        return None
    eff = effective_direction(symbol, side)
    if eff is None:
        return None
    return "with_trend" if eff == spy_direction else "counter_trend"


def with_trend_tag(label) -> str:
    """Infer whether a label describes a with-trend or counter-trend setup."""
    s = (label or "").lower()
    if any(k in s for k in ("with trend", "with-trend", "with_trend", "continuation", "trend follow", "momentum", "breakout")):
        return "with_trend"
    if any(k in s for k in ("counter", "revers", "fade", "mean")):
        return "counter_trend"
    return "unknown"


# Every dimension performance_breakdown can group by. Exported so callers echo
# what was actually applied instead of guessing.
VALID_GROUP_BY = ("strategy", "strategy_raw", "direction", "with_trend", "tod_bucket",
                  "breadth_bucket", "breadth_trend", "sector", "sector_adx_bucket")


def breadth_bucket_of(t: dict) -> str:
    """Bucket a trade by the breadth recorded AT ENTRY.

    Trades predating breadth capture return "unknown" rather than being silently
    lumped into a bucket — an unlabelled trade is not evidence.
    """
    from daytrader.core.breadth import bucket
    b = t.get("breadth_bucket")
    if b:
        return str(b)
    pct = t.get("breadth_pct")
    return bucket(pct) or "unknown"


def _numeric_bucket(v, edges, labels) -> str:
    if v is None:
        return "unknown"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "unknown"
    for edge, label in zip(edges, labels):
        if v <= edge:
            return label
    return labels[-1]


def _apply_filters(trades, filters) -> list:
    """Keep only trades matching {field: value | {min,max} | [allowed,...]}.

    Lets a desk ask the pointed question directly — "how do my EMA shorts do when
    breadth was <= 35?" — instead of grouping and eyeballing.
    """
    if not filters or not isinstance(filters, dict):
        return list(trades)
    out = []
    for t in trades:
        keep = True
        for field, cond in filters.items():
            v = t.get(field)
            if field == "breadth_bucket" and v is None:
                v = breadth_bucket_of(t)
            if isinstance(cond, dict):
                lo, hi = cond.get("min"), cond.get("max")
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    keep = False; break
                if lo is not None and fv < float(lo):
                    keep = False; break
                if hi is not None and fv > float(hi):
                    keep = False; break
            elif isinstance(cond, (list, tuple, set)):
                if v not in cond:
                    keep = False; break
            elif v != cond:
                keep = False; break
        if keep:
            out.append(t)
    return out


def performance_breakdown(trades, group_by=("strategy",), filters=None) -> list[dict]:
    """Group realized (closed, pnl-bearing) trades and compute per-group stats.

    group_by may contain "strategy" (canonicalized to a built-in bucket),
    "direction" (long/short, from the trade side), "with_trend"
    (with_trend/counter_trend/unknown, inferred from the label), and
    "tod_bucket" (ET session window). Rows are sorted by total P&L descending.
    """
    dims = [d for d in (group_by or []) if d in VALID_GROUP_BY]
    if not dims:
        dims = ["strategy"]
    trades = _apply_filters(trades, filters)
    groups: dict[tuple, list] = {}
    for t in trades:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        key = []
        for d in dims:
            if d == "strategy":
                key.append(canonical_strategy(t.get("strategy")))
            elif d == "strategy_raw":
                # preserve the exact label (so custom strategies don't collapse to "other")
                key.append((t.get("strategy") or "unknown").strip() or "unknown")
            elif d == "direction":
                key.append((t.get("side") or "").lower() or "unknown")
            elif d == "with_trend":
                # prefer the value RECORDED at entry (from SPY direction);
                # fall back to inferring from the label for older trades.
                wt = t.get("with_trend")
                key.append(wt if wt else with_trend_tag(t.get("strategy")))
            elif d == "breadth_bucket":
                key.append(breadth_bucket_of(t))
            elif d == "breadth_trend":
                # Deterioration vs improvement at entry — often the real signal.
                key.append(_numeric_bucket(t.get("breadth_change_20m"),
                                           [-5.0, 5.0],
                                           ["deteriorating", "flat", "improving"]))
            elif d == "sector":
                key.append(t.get("sector") or "unknown")
            elif d == "sector_adx_bucket":
                key.append(_numeric_bucket(t.get("sector_avg_adx"), [20.0, 30.0],
                                           ["weak", "moderate", "strong"]))
            else:
                key.append(tod_bucket(t.get("entry_ts")))
        groups.setdefault(tuple(key), []).append(float(pnl))
    rows = []
    for key, pnls in groups.items():
        row = {dims[i]: key[i] for i in range(len(dims))}
        row.update(_stats(pnls))
        rows.append(row)
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows
