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
        "profit_factor": round(gp / gl, 2) if gl > 0 else (round(gp, 2) if gp else 0.0),
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


def performance_breakdown(trades, group_by=("strategy",)) -> list[dict]:
    """Group realized (closed, pnl-bearing) trades and compute per-group stats.

    group_by may contain "strategy" and/or "tod_bucket". Rows are sorted by
    total P&L descending so the bleeders sort to the bottom.
    """
    dims = [d for d in (group_by or []) if d in ("strategy", "tod_bucket")]
    if not dims:
        dims = ["strategy"]
    groups: dict[tuple, list] = {}
    for t in trades:
        pnl = t.get("pnl")
        if pnl is None:
            continue
        key = []
        for d in dims:
            if d == "strategy":
                key.append(t.get("strategy") or "unknown")
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
