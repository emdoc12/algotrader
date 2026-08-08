"""Cross-sectional market context: breadth and sector-cluster series.

Every other feature in this system is computed from ONE symbol's bars. Breadth
is different — it is a property of the universe at a point in time, and the
desks kept losing money on setups that were technically clean on a single chart
while the tape underneath disagreed (a SPY short taken at breadth 13/21 when the
plan wanted <=7).

Producing these as **time series over history**, not as snapshot scalars, is
what lets the same numbers serve three places that previously could not agree:

  * the live snapshot (what the desk sees now),
  * the custom-strategy DSL, so a rule can *condition* on breadth in a backtest
    and a hypothesis can be walk-forward validated on it,
  * the trade record, so ``get_performance_breakdown`` can group realized
    results by the breadth that actually prevailed at entry.

Causality: every value at bar ``t`` uses only data available at ``t`` — the
day's open through the current close, and causal indicators. A symbol with no
print at ``t`` carries its last known value forward, which is what an observer
would have seen.
"""
from __future__ import annotations

import pandas as pd

from daytrader.core import indicators as ind

# Breadth buckets, as the desks reason about them: a "weak tape" is one where
# most of the universe is red, regardless of what any single chart shows.
WEAK_MAX = 35.0     # <= 35% advancing  -> weak (broad-down)
STRONG_MIN = 65.0   # >= 65% advancing  -> strong (broad-up)


def bucket(pct) -> str | None:
    """weak / mixed / strong from percent-advancing."""
    if pct is None or pd.isna(pct):
        return None
    p = float(pct)
    if p <= WEAK_MAX:
        return "weak"
    if p >= STRONG_MIN:
        return "strong"
    return "mixed"


def _day_change_pct(df: pd.DataFrame) -> pd.Series:
    """Percent change from THIS day's open, per bar. Causal by construction."""
    day = df.index.normalize()
    day_open = df.groupby(day)["open"].transform("first")
    return (df["close"] / day_open - 1.0) * 100.0


def _aligned(data: dict, fn, index: pd.Index) -> pd.DataFrame:
    """Apply ``fn`` per symbol and align onto a shared index, forward-filled."""
    cols = {}
    for sym, df in data.items():
        if df is None or len(df) < 2:
            continue
        try:
            s = fn(df)
        except Exception:  # noqa: BLE001 - one bad symbol must not kill breadth
            continue
        if s is None or not len(s):
            continue
        cols[sym] = s.reindex(index).ffill()
    return pd.DataFrame(cols, index=index) if cols else pd.DataFrame(index=index)


def market_series(data: dict, bars_20m: int = 4) -> dict:
    """Universe-wide breadth series aligned to a shared timeline.

    Returns {"index", "breadth_pct", "advancers", "total", "breadth_change_20m"}.
    ``bars_20m`` is how many bars span ~20 minutes at the data's interval
    (4 x 5m by default; pass 1 for 15m bars, etc.).
    """
    data = {s: df for s, df in (data or {}).items() if df is not None and len(df) > 1}
    if not data:
        return {}
    index = sorted({ts for df in data.values() for ts in df.index})
    index = pd.DatetimeIndex(index)
    chg = _aligned(data, _day_change_pct, index)
    if chg.empty:
        return {}
    total = chg.notna().sum(axis=1)
    advancers = (chg > 0).sum(axis=1)
    breadth_pct = (advancers / total.replace(0, pd.NA)) * 100.0
    return {
        "index": index,
        "advancers": advancers.astype("float64"),
        "total": total.astype("float64"),
        "breadth_pct": breadth_pct.astype("float64"),
        # Deterioration/improvement is often the signal, not the level.
        "breadth_change_20m": breadth_pct.diff(bars_20m).astype("float64"),
    }


def sector_series(data: dict, sectors: dict, bars_20m: int = 4) -> dict:
    """Per-sector cluster series: {sector: {avg_adx, pct_down, breadth_pct, ...}}.

    ``sectors`` is {sector_name: {members}}. A sector needs >= 3 members present
    in the data to be reported — fewer is not a cluster, it's a coincidence.
    """
    data = {s: df for s, df in (data or {}).items() if df is not None and len(df) > 30}
    if not data:
        return {}
    index = pd.DatetimeIndex(sorted({ts for df in data.values() for ts in df.index}))
    out = {}
    for sector, members in (sectors or {}).items():
        present = {s: df for s, df in data.items() if s in members}
        if len(present) < 3:
            continue
        adx = _aligned(present, lambda d: ind.adx(d, 14), index)
        ema9 = _aligned(present, lambda d: ind.ema(d["close"], 9), index)
        ema21 = _aligned(present, lambda d: ind.ema(d["close"], 21), index)
        chg = _aligned(present, _day_change_pct, index)
        if adx.empty or ema9.empty:
            continue
        down = (ema9 < ema21)
        valid = ema9.notna() & ema21.notna()
        n_valid = valid.sum(axis=1).replace(0, pd.NA)
        n_up = (chg > 0).sum(axis=1)
        n_chg = chg.notna().sum(axis=1).replace(0, pd.NA)
        out[sector] = {
            "sector_avg_adx": adx.mean(axis=1).astype("float64"),
            "sector_avg_adx_slope": adx.diff(3).mean(axis=1).astype("float64"),
            "sector_pct_down": ((down & valid).sum(axis=1) / n_valid * 100.0).astype("float64"),
            "sector_breadth_pct": (n_up / n_chg * 100.0).astype("float64"),
            "members": sorted(present),
        }
    return out


def sector_of(symbol: str, sectors: dict) -> str | None:
    for sector, members in (sectors or {}).items():
        if symbol in members:
            return sector
    return None


def bars_per_20m(interval: str) -> int:
    """How many bars ~20 minutes spans at a given interval (>=1)."""
    per = {"1m": 20, "2m": 10, "5m": 4, "15m": 1, "30m": 1, "1h": 1, "1d": 1}
    return max(1, per.get(str(interval or "5m").lower(), 4))
