"""Strategy mandate: declare a lane, then stay in it.

Seven desks given an open mandate converged on the same undifferentiated
discretionary trading and the same flat result. Open-ended discovery gives them
freedom they demonstrably cannot use; a *committed* strategy gives them real
choices with clear rules and a measurable record per lane.

Two things make the commitment real rather than decorative:

  * A **switch cooldown**. A desk that can change approach every cycle is not
    running a strategy, it is reacting — and a lane tested for one day produces
    no evidence about anything. Committing for a stretch is what makes the
    per-strategy P&L mean something.
  * A **capital cap per strategy**, so one conviction cannot become the whole
    account.

Nothing here judges whether a strategy is good. It enforces that a desk picked
one, said so in advance, and can be held to it afterwards.
"""
from __future__ import annotations

import json
import os

# How long a declared strategy must be kept before switching again.
COMMIT_DAYS = int(os.environ.get("STRATEGY_COMMIT_DAYS", "5"))
# Max share of buying power any single declared strategy may consume.
MAX_STRATEGY_ALLOCATION_PCT = float(os.environ.get("MAX_STRATEGY_ALLOCATION_PCT", "50"))

# The allowed menu. Options-based lanes are listed but gated until an options
# engine exists — a desk must not declare a strategy it cannot actually run.
STRATEGY_MENU = {
    "momentum_20d_high": {
        "executable": True,
        "summary": ("Long-only shares making NEW 20-DAY HIGHS on above-average volume. "
                    "Risk 1% per trade, hard stop under the recent swing low or ATR-based. "
                    "Time stop: exit if not profitable within 2-3 days. "
                    "Max 2-3 concurrent positions."),
        "rules": {"max_concurrent": 3, "time_stop_days": 3, "long_only": True},
    },
    "trend_position": {
        "executable": True,
        "summary": ("Medium-term directional holds in the UNDERLYING (the share-only "
                    "expression of a LEAP-style view): names above key moving averages "
                    "with strong relative strength. Scale out in thirds into strength; "
                    "hard stop 25-30% of the position's initial risk."),
        "rules": {"max_concurrent": 4, "horizon": "long"},
    },
    "mean_reversion_range": {
        "executable": True,
        "summary": ("Fade stretched moves back toward VWAP/EMA in range-bound, "
                    "non-trending tape only. Defined stop beyond the extreme."),
        "rules": {"max_concurrent": 3},
    },
    # ---- gated: require an options engine -------------------------------
    "wheel_csp": {
        "executable": False,
        "summary": ("Cash-secured puts / wheel on IV-Rank>40 underlyings; 20-35 delta, "
                    "30-45 DTE, take profit at 50-60% of max, roll or accept assignment, "
                    "then 30-40 delta covered calls."),
    },
    "bull_put_spread": {
        "executable": False,
        "summary": ("Defined-risk bull put credit spreads; 30-45 DTE, short strike 20-30 "
                    "delta, $5-10 wide, IV Rank>30, close at 50% profit or 21 DTE."),
    },
    "iron_condor": {
        "executable": False,
        "summary": ("Iron condors / defined-wing strangles; 30-45 DTE, ~15-20 delta short "
                    "strikes, close at 50% profit, manage the tested side."),
    },
    "leap_debit": {
        "executable": False,
        "summary": ("6-12 month calls or call debit spreads on strong RS names; scale out "
                    "in thirds; hard stop 25-30% of debit paid."),
    },
    "earnings_vol_crush": {
        "executable": False,
        "summary": ("Defined-risk condors/credit spreads 3-7 days pre-earnings on elevated "
                    "IV; close the day after or at 50% profit. Reduced size for gap risk."),
    },
}


def executable_strategies() -> list[str]:
    return sorted(k for k, v in STRATEGY_MENU.items() if v.get("executable"))


def gated_strategies() -> list[str]:
    return sorted(k for k, v in STRATEGY_MENU.items() if not v.get("executable"))


def _today() -> str:
    from daytrader.live.competition import _today_et
    return _today_et()


def _days_between(a: str, b: str) -> int:
    from datetime import date
    try:
        ya, ma, da = (int(x) for x in str(a)[:10].split("-"))
        yb, mb, db_ = (int(x) for x in str(b)[:10].split("-"))
        return abs((date(yb, mb, db_) - date(ya, ma, da)).days)
    except Exception:  # noqa: BLE001
        return 999


def current(db) -> dict:
    """The desk's active declaration, or an empty dict if it has none."""
    try:
        raw = db.kv_get("declared_strategy")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def declare(db, name: str, plan: str = "", allocation_pct: float | None = None) -> dict:
    """Commit to a strategy. Refuses a switch inside the cooldown."""
    name = str(name or "").strip().lower()
    if name not in STRATEGY_MENU:
        return {"ok": False, "error_code": "unknown_strategy",
                "error": f"{name!r} is not on the menu.",
                "executable": executable_strategies(),
                "gated_pending_options_engine": gated_strategies()}
    entry = STRATEGY_MENU[name]
    if not entry.get("executable"):
        return {"ok": False, "error_code": "not_executable",
                "error": (f"{name} is an OPTIONS strategy and cannot be run yet — there is "
                          "no options engine (no multiplier, premium, assignment or "
                          "exercise model), so declaring it would mean planning trades the "
                          "system will reject."),
                "executable_now": executable_strategies()}

    cur = current(db)
    today = _today()
    if cur.get("name") and cur["name"] != name:
        held = _days_between(cur.get("declared_on", today), today)
        if held < COMMIT_DAYS:
            return {"ok": False, "error_code": "commit_period",
                    "error": (f"you declared '{cur['name']}' {held} day(s) ago and must keep "
                              f"it for {COMMIT_DAYS}. A lane switched every cycle is not a "
                              "strategy and produces no evidence about anything."),
                    "current": cur, "can_switch_after_days": COMMIT_DAYS - held}

    alloc = float(allocation_pct if allocation_pct is not None else MAX_STRATEGY_ALLOCATION_PCT)
    alloc = max(1.0, min(alloc, MAX_STRATEGY_ALLOCATION_PCT))
    rec = {"name": name, "declared_on": today, "plan": str(plan or "")[:1000],
           "allocation_pct": alloc, "rules": entry.get("rules", {}),
           "summary": entry["summary"]}
    db.kv_set("declared_strategy", json.dumps(rec))
    try:
        db.add_journal("desk", "strategy", f"Declared {name} on {today}. {plan}"[:2000])
        db.log_agent("desk", "declare_strategy", name)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, **rec,
            "commit_days": COMMIT_DAYS,
            "note": (f"Committed to {name} for {COMMIT_DAYS} trading days. Trade its rules, "
                     "tag every trade with this strategy name, and let the record judge it.")}


def status(db, broker=None) -> dict:
    """Declaration + how much of it is used — for the snapshot."""
    cur = current(db)
    out = {"declared": cur or None,
           "commit_days": COMMIT_DAYS,
           "executable_strategies": executable_strategies(),
           "gated_pending_options_engine": gated_strategies()}
    if cur.get("declared_on"):
        held = _days_between(cur["declared_on"], _today())
        out["days_held"] = held
        out["can_switch"] = held >= COMMIT_DAYS
        out["can_switch_in_days"] = max(0, COMMIT_DAYS - held)
    if not cur:
        out["action_required"] = (
            "No strategy declared. Call declare_strategy with one of "
            f"{executable_strategies()} before trading — an undeclared desk is exactly "
            "the undifferentiated discretionary trading that has not worked.")
    return out
