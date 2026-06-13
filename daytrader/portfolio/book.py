"""The production book: which strategies trade, in which regime, at what weight.

This is the single place that defines the live strategy mix. Each strategy is
assigned the market regime it has an edge in (trend-followers only fire when
ADX says a trend exists; mean-reverters only fire in ranges). Weights are tuned
by the walk-forward optimizer in tune.py and pasted back here.

Strategies are imported lazily so a single broken/missing module doesn't take
down the whole book during development.
"""
from __future__ import annotations

from daytrader.portfolio.ensemble import Allocation, Ensemble, Regime

# (module, class, regimes, weight). Regimes gate when the strategy may fire.
_SPEC = [
    ("daytrader.strategies.orb", "OpeningRangeBreakout", {Regime.ANY.value}, 1.0),
    ("daytrader.strategies.vwap_reversion", "VwapReversion", {Regime.RANGE.value}, 1.0),
    ("daytrader.strategies.vwap_trend", "VwapTrend", {Regime.TREND.value}, 1.0),
    ("daytrader.strategies.rsi2", "Rsi2Reversion", {Regime.RANGE.value}, 1.0),
    ("daytrader.strategies.bollinger_fade", "BollingerFade", {Regime.RANGE.value}, 1.0),
    ("daytrader.strategies.ema_pullback", "EmaPullback", {Regime.TREND.value}, 1.0),
    ("daytrader.strategies.macd_trend", "MacdTrend", {Regime.TREND.value}, 1.0),
    ("daytrader.strategies.pivot_reversal", "PivotReversal", {Regime.RANGE.value}, 1.0),
    ("daytrader.strategies.gap_fade", "GapFade", {Regime.ANY.value}, 1.0),
]


def _load(spec):
    import importlib
    allocs = []
    for module, cls, regimes, weight in spec:
        try:
            mod = importlib.import_module(module)
            strat_cls = getattr(mod, cls)
            allocs.append(Allocation(strategy=strat_cls(), regimes=regimes, weight=weight))
        except Exception as e:  # noqa: BLE001
            print(f"[book] skipping {module}.{cls}: {e}")
    return allocs


def build_book(weights: dict[str, float] | None = None,
               regime_overrides: dict[str, set] | None = None,
               adx_threshold: float = 25.0,
               market_filter: bool = False) -> Ensemble:
    spec = []
    for module, cls, regimes, weight in _SPEC:
        if regime_overrides and cls in regime_overrides:
            regimes = regime_overrides[cls]
        if weights and cls in weights:
            weight = weights[cls]
        if weight <= 0:
            continue
        spec.append((module, cls, regimes, weight))
    return Ensemble(_load(spec), adx_threshold=adx_threshold, market_filter=market_filter)


def all_strategies() -> Ensemble:
    """Every strategy, no regime gating — for diagnostics/correlation."""
    spec = [(m, c, {Regime.ANY.value}, 1.0) for m, c, _, _ in _SPEC]
    return Ensemble(_load(spec))
