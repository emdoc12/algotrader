"""Look-ahead / causality regression tests.

The agent-authored custom-strategy DSL is the highest-risk surface: a feature
that leaks future data lets a desk 'discover' a fake edge and deploy it. These
tests assert that every custom feature and every custom strategy's signals are
CAUSAL — perturbing bars strictly after index i must not change any feature
value or signal at or before i.

Run: python3 -m pytest tests/test_causality.py -q
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from daytrader.strategies.custom import CustomRuleStrategy, _FEATURES


def _synth(days=3, per_day=40, seed=0):
    rng = np.random.RandomState(seed)
    idxs = []
    for d in range(days):
        idxs.append(pd.date_range(f"2026-06-{22+d} 09:30", periods=per_day,
                                   freq="5min", tz="America/New_York"))
    idx = idxs[0]
    for extra in idxs[1:]:
        idx = idx.append(extra)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.001, len(idx)))
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.0003, len(idx))),
        "high": close * 1.002, "low": close * 0.998,
        "close": close, "volume": rng.randint(1e5, 1e6, len(idx)),
        "symbol": "X",
    }, index=idx)


def test_custom_features_are_causal():
    """Perturbing future bars must not change any feature value at/before i."""
    df = _synth()
    strat = CustomRuleStrategy({"name": "probe", "side": "long",
                                "entry": [{"left": "rsi", "op": "<", "right": 50}]})
    base = strat._features(df)
    i = len(df) - 15  # cut point with future bars to corrupt
    corrupt = df.copy()
    corrupt.iloc[i + 1:, corrupt.columns.get_indexer(["open", "high", "low", "close"])] *= 1.5
    pert = strat._features(corrupt)
    for feat in _FEATURES:
        b = np.asarray(base[feat][:i + 1], dtype="float64")
        p = np.asarray(pert[feat][:i + 1], dtype="float64")
        mask = ~(np.isnan(b) | np.isnan(p))
        assert np.allclose(b[mask], p[mask], rtol=1e-6, atol=1e-6), (
            f"feature {feat!r} leaks future data (values at/before bar {i} changed)")


def test_gap_pct_uses_prior_session_close():
    """gap_pct on day N must reference day N-1's close, constant across the day."""
    d1 = pd.date_range("2026-06-25 09:30", periods=10, freq="5min", tz="America/New_York")
    d2 = pd.date_range("2026-06-26 09:30", periods=10, freq="5min", tz="America/New_York")
    idx = d1.append(d2)
    close = np.concatenate([np.linspace(100, 120, 10), np.linspace(110, 90, 10)])
    df = pd.DataFrame({"open": np.concatenate([[100] * 10, [110] * 10]),
                       "high": close * 1.001, "low": close * 0.999,
                       "close": close, "volume": 1000, "symbol": "X"}, index=idx)
    strat = CustomRuleStrategy({"name": "g", "side": "long",
                                "entry": [{"left": "gap_pct", "op": ">", "right": -999}]})
    gap = strat._features(df)["gap_pct"]
    day2 = gap[10:20]
    assert np.isnan(gap[:10]).all(), "day 1 has no prior session; gap should be NaN"
    # day-2 open 110 vs prior-session close 120 => ~-8.33%, identical on every bar
    assert np.allclose(day2, day2[0]), "gap_pct leaks the current day's close"
    assert abs(day2[0] - (110 / 120 - 1) * 100) < 0.01


def test_custom_signals_are_causal():
    """Signals stamped at/before bar i must not change when future bars change."""
    df = _synth(seed=3)
    strat = CustomRuleStrategy({"name": "s", "side": "long", "stop_atr_mult": 1.5, "rr": 2.0,
                                "entry": [{"left": "ema9", "op": ">", "right": "ema21"},
                                          {"left": "rsi", "op": "<", "right": 45}]})
    full = {s.ts for s in strat.generate(df)}
    i = len(df) - 12
    cut_ts = df.index[i]
    corrupt = df.copy()
    corrupt.iloc[i + 1:, corrupt.columns.get_indexer(["open", "high", "low", "close"])] *= 0.8
    pert = {s.ts for s in strat.generate(corrupt)}
    before_full = {t for t in full if t <= cut_ts}
    before_pert = {t for t in pert if t <= cut_ts}
    assert before_full == before_pert, "custom strategy signals depend on future bars"
