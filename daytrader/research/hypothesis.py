"""The hypothesis grammar: a structured, machine-testable proposal.

A hypothesis is NOT free text. It is a custom-strategy DSL config (the same
grammar ``backtest_custom_strategy`` already validates and the causality tests
already cover) plus the research metadata that makes a result trustworthy:
the universe, the bar interval, and — critically — the pass/fail criteria,
which are fixed at registration time and can never be edited afterwards.

The canonical hash is what makes the failure log work. Two proposals that
differ only in name, key order, or a cosmetic default collapse to the same
hash, so a rejected idea cannot be quietly re-proposed and later "discovered".
"""
from __future__ import annotations

import hashlib
import json

from daytrader.strategies.custom import StrategyConfigError, validate_config

# Bar intervals a hypothesis may be tested on, and how much history each can
# actually serve. Yahoo caps 5m at ~60 days, which is why research defaults to
# 1h: five genuine 30-day periods need ~150 days of history minimum.
INTERVAL_HISTORY_DAYS = {"5m": 60, "15m": 60, "1h": 730, "1d": 3650}
DEFAULT_INTERVAL = "1h"


class HypothesisError(ValueError):
    """Raised when a proposal is malformed or violates pre-registration rules."""


# Keys that describe an OUTCOME. Their presence in a proposal means someone is
# trying to register a hypothesis with its results already attached, which
# defeats pre-registration entirely.
_RESULT_KEYS = {
    "result", "results", "metrics", "pnl", "profit_factor", "win_rate",
    "sharpe", "periods_profitable", "p_value", "status", "accepted",
    "rejected", "test_ordinal", "equity", "trades",
}


def canonical_spec(spec: dict) -> dict:
    """Validate a proposal and reduce it to canonical, hashable form.

    Runs the same ``validate_config`` the live backtester uses, so a hypothesis
    that registers is guaranteed to be runnable — a proposal can never fail
    later for a reason that was knowable at registration.
    """
    if not isinstance(spec, dict):
        raise HypothesisError("spec must be an object")
    leaked = _RESULT_KEYS & {str(k).strip().lower() for k in spec}
    if leaked:
        raise HypothesisError(
            f"spec carries outcome field(s) {sorted(leaked)}; a hypothesis must be "
            "registered BEFORE any result exists")

    rule = dict(spec.get("rule") or spec.get("config") or {})
    if not rule:
        raise HypothesisError("spec.rule (a custom-strategy config) is required")
    # Validate for runnability, but keep the RAW rule: validate_config rewrites
    # conditions into internal form (lf/rf/rconst), which CustomRuleStrategy
    # cannot re-parse. The normalized output is used only for hashing, where it
    # is exactly what we want — it resolves aliases and fills defaults, so
    # `close`/`price` and an omitted `rr` collapse to the same identity.
    try:
        norm_rule = validate_config(rule)
    except StrategyConfigError as e:
        raise HypothesisError(f"rule is not a valid strategy config: {e}") from e

    symbols = spec.get("universe") or spec.get("symbols") or ["SPY"]
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if not symbols:
        raise HypothesisError("universe must name at least one symbol")

    interval = str(spec.get("interval") or DEFAULT_INTERVAL).strip().lower()
    if interval not in INTERVAL_HISTORY_DAYS:
        raise HypothesisError(
            f"interval must be one of {sorted(INTERVAL_HISTORY_DAYS)} (got {interval!r})")

    execution = canonical_execution(spec)
    return {
        "rule": rule,             # runnable: fed straight to CustomRuleStrategy
        "identity": norm_rule,    # normalized: hashed for dedup / failure log
        "universe": symbols,
        "interval": interval,
        "execution": execution,
    }


def canonical_execution(spec: dict) -> dict:
    """How the rule is HELD and EXITED — part of the hypothesis, not a detail.

    The same entry rule tested intraday and as a swing is two different claims
    about the market, so execution is hashed into the identity: a swing variant
    of a rejected intraday rule is a genuinely new hypothesis, not a re-proposal.
    """
    horizon = str(spec.get("horizon") or "intraday").strip().lower()
    if horizon not in ("intraday", "swing"):
        raise HypothesisError("horizon must be 'intraday' or 'swing'")

    def _num(key, lo=0.0, hi=1e6):
        v = spec.get(key)
        if v in (None, ""):
            return 0.0
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise HypothesisError(f"{key} must be a number") from None
        if not lo <= v <= hi:
            raise HypothesisError(f"{key} must be between {lo} and {hi}")
        return v

    max_hold_days = _num("max_hold_days", 0.0, 365.0)
    if horizon == "swing" and max_hold_days <= 0:
        raise HypothesisError(
            "a swing hypothesis needs max_hold_days > 0 — without a time stop a rule "
            "that never hits stop or target degenerates into buy-and-hold, which "
            "measures the market, not the rule")
    return {
        "horizon": horizon,
        "max_hold_days": max_hold_days,
        "trail_atr_mult": _num("trail_atr_mult", 0.0, 20.0),
        "trail_pct": _num("trail_pct", 0.0, 50.0),
        "breakeven_at_r": _num("breakeven_at_r", 0.0, 20.0),
    }


def spec_hash(canon: dict) -> str:
    """Stable content hash of a canonical spec — the failure log's identity key.

    Hashes the NORMALIZED rule, so alias spellings (`close` vs `price`) and
    omitted-but-defaulted fields cannot disguise a re-proposal. The cosmetic
    ``name`` is excluded for the same reason: relabelling a rejected idea must
    not make it look new.
    """
    ident = canon.get("identity") or canon["rule"]
    payload = {
        "rule": {k: v for k, v in ident.items() if k != "name"},
        "universe": canon["universe"],
        "interval": canon["interval"],
        # Execution is part of the claim: the same entry rule held intraday vs
        # as a swing are different hypotheses and must hash differently.
        "execution": canon.get("execution") or {},
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def canonical_criteria(criteria: dict | None, n_periods: int) -> dict:
    """Normalize the PRE-REGISTERED pass/fail bar.

    These are fixed at registration. The evaluator reads them; nothing may
    rewrite them once a result exists.
    """
    c = dict(criteria or {})
    n_periods = int(n_periods)
    if n_periods < 3:
        raise HypothesisError("n_periods must be >= 3 for a walk-forward to mean anything")
    min_profitable = int(c.get("min_periods_profitable", max(3, (n_periods * 4) // 5)))
    if not 1 <= min_profitable <= n_periods:
        raise HypothesisError(
            f"min_periods_profitable must be in 1..{n_periods} (got {min_profitable})")
    min_trades = int(c.get("min_trades", 30))
    if min_trades < 10:
        raise HypothesisError("min_trades must be >= 10; fewer cannot support inference")
    return {
        "n_periods": n_periods,
        "min_periods_profitable": min_profitable,
        "min_trades": min_trades,
        # Family-wise base alpha BEFORE the multiple-comparison correction.
        "base_alpha": float(c.get("base_alpha", 0.05)),
    }
