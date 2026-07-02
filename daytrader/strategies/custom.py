"""Declarative, agent-authored custom strategies.

Lets a desk define a brand-new setup from rules — without a developer — and run
it through the SAME backtest engine, cost model, and metrics as the built-ins.

A strategy is a small JSON-ish config:

    {
      "name": "rsi_dip_in_uptrend",
      "side": "long",
      "entry": [
        {"left": "ema9",  "op": ">",  "right": "ema21"},   # uptrend
        {"left": "rsi",   "op": "<",  "right": 35},          # pulled back
        {"left": "price", "op": ">",  "right": "vwap"},      # above VWAP
        {"left": "adx",   "op": ">=", "right": 20}
      ],
      "stop_atr_mult": 1.5,     # protective stop = entry -/+ mult * ATR
      "rr": 2.0,                # target at rr * risk
      "max_entries_per_day": 1,
      "no_entry_before": "09:45",
      "no_entry_after": "15:00"
    }

All ``entry`` conditions are AND-ed. Each condition compares a FEATURE to either
a number or another feature. Append ``_prev`` to a feature to read the prior
bar's value (e.g. crossovers). Operators: < <= > >= == != cross_above
cross_below. Exits are handled by the engine (ATR stop, RR target, EOD flat),
exactly like the built-ins — so results are directly comparable.

Everything is causal: a decision on bar i uses only data through bar i, and the
engine fills at the next bar's open. The config is validated on construction and
never executes arbitrary code — it's a fixed feature/operator vocabulary.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core import indicators as ind
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy

_OPS = {"<", "<=", ">", ">=", "==", "!=", "cross_above", "cross_below"}

# Canonical feature names + accepted aliases.
_FEATURE_ALIASES = {
    "close": "price", "last": "price",
    "rsi14": "rsi", "rsi_14": "rsi",
    "atr14": "atr", "atr_14": "atr",
    "adx14": "adx", "adx_14": "adx",
    "ema_9": "ema9", "ema_21": "ema21", "ema_50": "ema50",
    "macd_line": "macd", "macd_sig": "macd_signal", "macd_histogram": "macd_hist",
    "bollinger_upper": "bb_upper", "bollinger_lower": "bb_lower", "bollinger_mid": "bb_mid",
    "vwap_dist_pct": "vs_vwap_pct",
}

_FEATURES = {
    "price", "open", "high", "low", "volume",
    "ema9", "ema21", "ema50", "sma20",
    "rsi", "rsi2", "atr", "atr_pct", "adx",
    "vwap", "vs_vwap_pct",
    "macd", "macd_signal", "macd_hist",
    "bb_upper", "bb_lower", "bb_mid", "bb_pct",
    "day_change_pct", "gap_pct", "ret1", "ret3",
}


class StrategyConfigError(ValueError):
    """Raised when a custom-strategy config is malformed."""


def _canon_feature(name: str) -> tuple[str, bool]:
    """Return (canonical_feature, is_prev). Raises on unknown features."""
    key = str(name).strip().lower()
    is_prev = key.endswith("_prev")
    if is_prev:
        key = key[:-5]
    key = _FEATURE_ALIASES.get(key, key)
    if key not in _FEATURES:
        raise StrategyConfigError(
            f"unknown feature {name!r}; valid features: {sorted(_FEATURES)}")
    return key, is_prev


def validate_config(config: dict) -> dict:
    """Validate + normalize a config. Raises StrategyConfigError on problems."""
    if not isinstance(config, dict):
        raise StrategyConfigError("config must be an object")
    side = str(config.get("side", "long")).strip().lower()
    if side not in ("long", "short"):
        raise StrategyConfigError("side must be 'long' or 'short'")
    entry = config.get("entry") or config.get("conditions")
    if not isinstance(entry, list) or not entry:
        raise StrategyConfigError("entry must be a non-empty list of conditions")
    norm_entry = []
    for i, cond in enumerate(entry):
        if not isinstance(cond, dict):
            raise StrategyConfigError(f"condition {i} must be an object")
        op = str(cond.get("op", "")).strip()
        if op not in _OPS:
            raise StrategyConfigError(f"condition {i}: op must be one of {sorted(_OPS)}")
        if "left" not in cond or "right" not in cond:
            raise StrategyConfigError(f"condition {i}: needs 'left' and 'right'")
        lf, lp = _canon_feature(cond["left"])
        right = cond["right"]
        if isinstance(right, (int, float)):
            rf, rp, rconst = None, False, float(right)
        else:
            rf, rp = _canon_feature(right)
            rconst = None
        norm_entry.append({"op": op, "lf": lf, "lp": lp, "rf": rf, "rp": rp, "rconst": rconst})
    out = {
        "name": str(config.get("name") or "custom")[:40],
        "side": side,
        "entry": norm_entry,
        "stop_atr_mult": float(config.get("stop_atr_mult", 1.5)),
        "rr": float(config.get("rr", 2.0)),
        "max_entries_per_day": int(config.get("max_entries_per_day", 1)),
        "no_entry_before": config.get("no_entry_before"),
        "no_entry_after": config.get("no_entry_after"),
    }
    if out["stop_atr_mult"] <= 0:
        raise StrategyConfigError("stop_atr_mult must be > 0")
    if out["rr"] <= 0:
        raise StrategyConfigError("rr must be > 0")
    return out


def _parse_time(v, default):
    if v is None:
        return default
    try:
        hh, mm = str(v).split(":")[:2]
        return dtime(int(hh), int(mm))
    except Exception:  # noqa: BLE001
        return default


class CustomRuleStrategy(Strategy):
    name = "Custom"

    def __init__(self, config: dict):
        self.cfg = validate_config(config)
        self.name = self.cfg["name"]
        super().__init__(config=self.cfg)

    # -- feature matrix (all causal) ------------------------------------
    def _features(self, df: pd.DataFrame) -> dict:
        close = df["close"]
        high, low = df["high"], df["low"]
        macd_line, macd_sig, macd_hist = ind.macd(close)
        bb_mid, bb_up, bb_lo, _bb_width = ind.bollinger(close, 20, 2.0)
        vwap = ind.vwap_session(df)
        atr = ind.atr(df, 14)
        prior_close = close.shift(1)
        day = df.index.normalize()
        # True session open = the first bar's OPEN (not its close).
        day_open = df["open"].groupby(day).transform("first")
        # Prior SESSION's close, broadcast to every bar of the current day.
        # (Do NOT use transform("last").shift(1): that shifts by one ROW, so
        # every bar after a day's first would read TODAY's close — a look-ahead
        # leak. Group to one value per day, shift by one DAY, then map back.)
        last_by_day = close.groupby(day).last().shift(1)
        prev_day_close = pd.Series(
            np.asarray(day.map(last_by_day), dtype="float64"), index=df.index)
        feats = {
            "price": close, "open": df["open"], "high": high, "low": low,
            "volume": df["volume"],
            "ema9": ind.ema(close, 9), "ema21": ind.ema(close, 21),
            "ema50": ind.ema(close, 50), "sma20": ind.sma(close, 20),
            "rsi": ind.rsi(close, 14), "rsi2": ind.rsi(close, 2),
            "atr": atr, "atr_pct": atr / close * 100,
            "adx": ind.adx(df, 14),
            "vwap": vwap, "vs_vwap_pct": (close / vwap - 1) * 100,
            "macd": macd_line, "macd_signal": macd_sig, "macd_hist": macd_hist,
            "bb_upper": bb_up, "bb_lower": bb_lo, "bb_mid": bb_mid,
            "bb_pct": (close - bb_lo) / (bb_up - bb_lo).replace(0, np.nan) * 100,
            "day_change_pct": (close / day_open - 1) * 100,
            "gap_pct": (day_open / prev_day_close - 1) * 100,
            "ret1": close.pct_change(1) * 100,
            "ret3": close.pct_change(3) * 100,
        }
        return {k: v.values for k, v in feats.items()}

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        n = len(df)
        if n < 60:
            return []
        symbol = df["symbol"].iloc[0]
        F = self._features(df)
        c = df["close"].values
        atr = F["atr"]
        idx = df.index
        day = df.index.normalize()
        no_before = _parse_time(self.cfg["no_entry_before"], dtime(9, 35))
        no_after = _parse_time(self.cfg["no_entry_after"], dtime(15, 45))
        side = Side.LONG if self.cfg["side"] == "long" else Side.SHORT
        smult, rr = self.cfg["stop_atr_mult"], self.cfg["rr"]
        max_per_day = self.cfg["max_entries_per_day"]

        def operand(spec_f, spec_prev, i):
            arr = F[spec_f]
            j = i - 1 if spec_prev else i
            if j < 0:
                return np.nan
            return arr[j]

        signals: list[Signal] = []
        per_day: dict = {}
        for i in range(55, n):
            t = idx[i].time()
            if t < no_before or t >= no_after:
                continue
            if np.isnan(atr[i]) or atr[i] <= 0:
                continue
            d = day[i]
            if per_day.get(d, 0) >= max_per_day:
                continue

            ok = True
            for cond in self.cfg["entry"]:
                ln = operand(cond["lf"], cond["lp"], i)
                if cond["rconst"] is not None:
                    rn = cond["rconst"]
                else:
                    rn = operand(cond["rf"], cond["rp"], i)
                op = cond["op"]
                if op in ("cross_above", "cross_below"):
                    lp = operand(cond["lf"], True, i)
                    rp = cond["rconst"] if cond["rconst"] is not None else operand(cond["rf"], True, i)
                    if np.isnan(ln) or np.isnan(rn) or np.isnan(lp) or np.isnan(rp):
                        ok = False; break
                    if op == "cross_above":
                        ok = lp <= rp and ln > rn
                    else:
                        ok = lp >= rp and ln < rn
                else:
                    if np.isnan(ln) or (isinstance(rn, float) and np.isnan(rn)):
                        ok = False; break
                    if op == "<": ok = ln < rn
                    elif op == "<=": ok = ln <= rn
                    elif op == ">": ok = ln > rn
                    elif op == ">=": ok = ln >= rn
                    elif op == "==": ok = abs(ln - rn) < 1e-9
                    elif op == "!=": ok = abs(ln - rn) >= 1e-9
                if not ok:
                    break

            if not ok:
                continue

            price = c[i]
            if side == Side.LONG:
                stop = price - smult * atr[i]
                risk = price - stop
                target = price + rr * risk
            else:
                stop = price + smult * atr[i]
                risk = stop - price
                target = price - rr * risk
            if risk <= 0:
                continue
            signals.append(Signal(
                ts=idx[i], symbol=symbol, side=side, type=SignalType.ENTRY,
                strategy=self.name, stop=round(stop, 4), target=round(target, 4),
                reason=f"{self.name} {side.value} @ {price:.2f}",
            ))
            per_day[d] = per_day.get(d, 0) + 1
        return signals
