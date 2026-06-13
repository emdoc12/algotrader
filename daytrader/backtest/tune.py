"""Walk-forward strategy selection.

Greedy forward-selection on IN-SAMPLE data, validated on OUT-OF-SAMPLE. We
deliberately keep the search small and bias toward robustness rather than
squeezing the in-sample curve:

  * Objective: maximize in-sample profit factor subject to max DD < dd_cap.
  * Start from the empty book; repeatedly add the single (strategy, regime)
    that most improves the objective; stop when nothing helps.
  * Re-score the chosen book on untouched OOS data and report both.

This won't manufacture an edge that isn't there — if the OOS numbers fall
apart, that is the honest answer and the report will say so.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from daytrader.backtest.engine import EngineConfig
from daytrader.backtest.metrics import Metrics, compute
from daytrader.backtest.runner import benchmark_return
from daytrader.backtest.validate import split_by_date
from daytrader.portfolio.book import _SPEC, _load
from daytrader.portfolio.ensemble import Allocation, Ensemble, Regime


@dataclass
class TuneResult:
    chosen: list[tuple]          # [(class_name, regime_set, weight)]
    is_metrics: Metrics
    oos_metrics: Metrics
    adx_threshold: float
    trace: list[str]


def _score(allocs, data, config, sizer, adx) -> Metrics:
    from daytrader.backtest.engine import BacktestEngine
    ens = Ensemble(allocs, adx_threshold=adx)
    engine = BacktestEngine(config=config, sizer=sizer)
    trades, equity = engine.run(data, ens.generate(data))
    bench = benchmark_return(data, "SPY")
    return compute(trades, equity, config.starting_equity, benchmark_return_pct=bench)


def _objective(m: Metrics, dd_cap: float) -> float:
    """Higher is better. Penalize breaching the DD cap and require >0 trades."""
    if m.n_trades < 5:
        return -1e9
    pf = 5.0 if m.profit_factor == float("inf") else m.profit_factor
    penalty = 0.0 if m.max_drawdown_pct <= dd_cap else (m.max_drawdown_pct - dd_cap) * 0.5
    # reward beating SPY a little so we don't pick a do-nothing book
    return pf - penalty + min(m.alpha_pts, 20) * 0.01


def tune(
    data: dict[str, pd.DataFrame],
    config: EngineConfig | None = None,
    sizer=None,
    oos_fraction: float = 0.35,
    dd_cap: float = 10.0,
    adx_grid=(20.0, 25.0, 30.0),
    candidate_regimes=None,
) -> TuneResult:
    config = config or EngineConfig()
    is_data, oos_data = split_by_date(data, oos_fraction)

    # Candidate pool: each strategy in its naturally-suited regime, plus ANY.
    if candidate_regimes is None:
        candidate_regimes = {
            "OpeningRangeBreakout": [{Regime.ANY.value}],
            "VwapReversion": [{Regime.RANGE.value}, {Regime.ANY.value}],
            "VwapTrend": [{Regime.TREND.value}, {Regime.ANY.value}],
            "Rsi2Reversion": [{Regime.RANGE.value}, {Regime.ANY.value}],
            "BollingerFade": [{Regime.RANGE.value}, {Regime.ANY.value}],
            "EmaPullback": [{Regime.TREND.value}, {Regime.ANY.value}],
            "MacdTrend": [{Regime.TREND.value}, {Regime.ANY.value}],
            "PivotReversal": [{Regime.RANGE.value}, {Regime.ANY.value}],
            "GapFade": [{Regime.ANY.value}],
        }

    spec_by_class = {c: (m, c) for m, c, _, _ in _SPEC}

    best_overall = None
    for adx in adx_grid:
        chosen: list[tuple] = []   # (module, class, regimes, weight)
        chosen_keys: set = set()
        trace = [f"--- ADX={adx} ---"]
        best_obj = -1e9
        improved = True
        while improved:
            improved = False
            best_candidate = None
            for cls, regime_opts in candidate_regimes.items():
                if cls not in spec_by_class:
                    continue
                module, _ = spec_by_class[cls]
                for regimes in regime_opts:
                    key = (cls, frozenset(regimes))
                    if key in chosen_keys:
                        continue
                    trial = chosen + [(module, cls, regimes, 1.0)]
                    allocs = _load(trial)
                    if len(allocs) != len(trial):
                        continue
                    m = _score(allocs, is_data, config, sizer, adx)
                    obj = _objective(m, dd_cap)
                    if obj > best_obj + 1e-6:
                        best_obj = obj
                        best_candidate = (key, trial, m)
            if best_candidate is not None:
                key, trial, m = best_candidate
                chosen = trial
                chosen_keys.add(key)
                improved = True
                trace.append(f"+ {key[0]} {set(key[1])}  ->  PF {m.profit_factor:.2f}, "
                             f"DD {m.max_drawdown_pct:.1f}%, ret {m.total_return_pct:+.1f}%  (obj {best_obj:.3f})")
        if chosen:
            is_m = _score(_load(chosen), is_data, config, sizer, adx)
            if best_overall is None or _objective(is_m, dd_cap) > _objective(best_overall[1], dd_cap):
                best_overall = (chosen, is_m, adx, trace)

    if best_overall is None:
        empty = compute([], pd.Series(dtype=float), config.starting_equity)
        return TuneResult([], empty, empty, adx_grid[0], ["no viable book found"])

    chosen, is_m, adx, trace = best_overall
    oos_m = _score(_load(chosen), oos_data, config, sizer, adx)
    return TuneResult(
        chosen=[(c, r, w) for _, c, r, w in chosen],
        is_metrics=is_m, oos_metrics=oos_m, adx_threshold=adx, trace=trace,
    )
