"""Rolling-window standing: how each desk is doing against the mandate.

The competition used to be judged the way a tournament is — a fixed line in the
calendar, everyone measured at once. That shape has a known failure: a desk
that is behind near the deadline has every incentive to increase variance,
because a coin-flip that might save it costs nothing when the downside is
elimination either way. It is a documented effect in real fund management, and
it would corrupt exactly the data the owner is using to choose a manager.

So there is no deadline. Every desk is judged on a TRAILING window that moves
with it, and on a RISK-ADJUSTED score rather than raw return:

  * the window slides, so no day is more consequential than another and there
    is no moment where recklessness pays;
  * the score divides annualized return by the drawdown it took to get there,
    so a lucky swing on terrible risk scores worse than a steady grind — the
    gamble stops being a rational response to being behind;
  * an absolute floor sits underneath it, so trading microscopically small to
    flatter the ratio does not pass either.

The bar itself is the owner's mandate: beat SPY, clear 8% annualized, and aim
for 15-30%.
"""
from __future__ import annotations

import os

# The mandate, in one place. Everything else in this module reads from here.
TARGET_FLOOR_PCT = float(os.environ.get("TARGET_ANNUAL_FLOOR_PCT", "8"))
TARGET_BAND_LOW_PCT = float(os.environ.get("TARGET_ANNUAL_LOW_PCT", "15"))
TARGET_BAND_HIGH_PCT = float(os.environ.get("TARGET_ANNUAL_HIGH_PCT", "30"))
WINDOW_DAYS = int(os.environ.get("STANDING_WINDOW_DAYS", "90"))
# Below this many closed trades in the window a desk is not ranked against the
# others at all. Seven trades in ninety days is not a bad record, it is an
# absent one, and relegating on it would be relegating on noise.
MIN_TRADES = int(os.environ.get("STANDING_MIN_TRADES", "20"))


def _window_start(days: int) -> str:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    return (datetime.now(ZoneInfo("America/New_York"))
            - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _annualize(pct: float, days: float) -> float:
    """Window return -> annualized. Guarded: a 3-day window annualizes to
    nonsense, so short windows return the raw figure rather than a fantasy."""
    if days < 21:
        return round(pct, 2)
    try:
        return round(((1 + pct / 100.0) ** (365.0 / days) - 1) * 100.0, 2)
    except (OverflowError, ValueError, ZeroDivisionError):
        return round(pct, 2)


def window_metrics(db, days: int = WINDOW_DAYS) -> dict | None:
    """One desk's trailing-window performance. None if it has no history here.

    Return is measured on the desk's own equity curve inside the window, with
    any owner deposit made during it subtracted — capital given is not capital
    earned, and a top-up landing mid-window would otherwise read as a good
    quarter.
    """
    start = _window_start(days)
    try:
        rows = db.conn.execute(
            "SELECT ts, equity FROM equity_snapshots WHERE ts >= ? ORDER BY id ASC",
            (start,)).fetchall()
    except Exception:  # noqa: BLE001
        return None
    if len(rows) < 2:
        return None
    eq = [float(r["equity"]) for r in rows]
    first, last = eq[0], eq[-1]
    if first <= 0:
        return None

    deposits = 0.0
    try:
        d = db.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM capital_events WHERE ts >= ?",
            (start,)).fetchone()
        deposits = float(d["s"] or 0.0)
    except Exception:  # noqa: BLE001
        deposits = 0.0

    ret_pct = (last - first - deposits) / first * 100.0
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)

    try:
        trades = db.conn.execute(
            "SELECT pnl FROM trades WHERE exit_ts >= ?", (start,)).fetchall()
        pnls = [float(t["pnl"] or 0.0) for t in trades]
    except Exception:  # noqa: BLE001
        pnls = []

    # Span the snapshots actually cover, not the nominal window — a desk that
    # started three weeks ago must not be annualized as if it ran for ninety days.
    span = days
    try:
        from datetime import datetime
        a = datetime.fromisoformat(str(rows[0]["ts"])[:19])
        b = datetime.fromisoformat(str(rows[-1]["ts"])[:19])
        span = max(1.0, (b - a).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001
        pass

    ann = _annualize(ret_pct, span)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gl = -sum(losses)
    return {
        "window_days": round(span, 1),
        "return_pct": round(ret_pct, 2),
        "annualized_pct": ann,
        "max_drawdown_pct": round(max_dd, 2),
        "n_trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "profit_factor": round(sum(wins) / gl, 2) if gl > 0 else None,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "deposits_in_window": round(deposits, 2),
    }


def risk_score(m: dict) -> float | None:
    """Annualized return per unit of drawdown — the ranking number.

    This is the piece that makes announcing relegation safe. Raw return rewards
    a desperate swing; this does not: the drawdown the swing opens divides the
    return it might earn, so variance is priced in rather than ignored. A
    drawdown under 1% is floored at 1% so a desk that has barely traded cannot
    manufacture an enormous ratio out of a rounding-error denominator.
    """
    if not m or m.get("annualized_pct") is None:
        return None
    dd = max(float(m.get("max_drawdown_pct") or 0.0), 1.0)
    return round(float(m["annualized_pct"]) / dd, 2)


def classify(m: dict, spy_return_pct: float | None) -> dict:
    """Where this desk sits against the mandate, in plain terms."""
    ann = float(m.get("annualized_pct") or 0.0)
    beats_spy = (spy_return_pct is None) or (float(m.get("return_pct") or 0.0) > spy_return_pct)
    if m.get("n_trades", 0) < MIN_TRADES:
        status, label = "inactive", (
            f"not enough activity to judge — {m.get('n_trades', 0)} closed trades in "
            f"{m.get('window_days')} days, {MIN_TRADES} needed. This is its own "
            "failure, not a safe third place: a desk that will not take positions "
            "is not doing the job.")
    elif ann >= TARGET_BAND_LOW_PCT and beats_spy:
        status, label = "on_target", (
            f"in the target band — {ann:.1f}% annualized against a "
            f"{TARGET_BAND_LOW_PCT:.0f}-{TARGET_BAND_HIGH_PCT:.0f}% goal, and ahead of SPY.")
    elif ann >= TARGET_FLOOR_PCT and beats_spy:
        status, label = "passing", (
            f"above the floor — {ann:.1f}% annualized clears {TARGET_FLOOR_PCT:.0f}% "
            f"and beats SPY, but is short of the {TARGET_BAND_LOW_PCT:.0f}% target.")
    elif not beats_spy:
        status, label = "below_bar", (
            f"losing to the index — {m.get('return_pct'):.2f}% against SPY's "
            f"{spy_return_pct:.2f}% over the same window. Owning SPY and doing "
            "nothing would have paid more than this desk did.")
    else:
        status, label = "below_bar", (
            f"under the floor — {ann:.1f}% annualized against a "
            f"{TARGET_FLOOR_PCT:.0f}% minimum.")
    return {"status": status, "assessment": label, "beats_spy": beats_spy}


def evaluate(team_names, team_db_path, spy_return_pct: float | None = None,
             days: int = WINDOW_DAYS) -> dict:
    """Rank every desk on the rolling window. Returns the full standing."""
    from daytrader.live.db import LiveDB

    ranked, inactive = [], []
    for name in team_names:
        try:
            db = LiveDB(team_db_path(name))
            try:
                m = window_metrics(db, days)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            continue
        row = {"team": name, **m, "risk_score": risk_score(m),
               **classify(m, spy_return_pct)}
        (inactive if row["status"] == "inactive" else ranked).append(row)

    ranked.sort(key=lambda r: (r["risk_score"] is None, -(r["risk_score"] or 0)))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    # Only a RANKED desk can be relegated, and only when there are at least two
    # to compare — relegating the sole survivor of an empty field is not a
    # judgement about anything.
    at_risk = ranked[-1]["team"] if len(ranked) >= 2 else None
    for r in ranked:
        r["at_risk"] = (r["team"] == at_risk)
    return {
        "window_days": days,
        "spy_return_pct": spy_return_pct,
        "targets": {"floor_annual_pct": TARGET_FLOOR_PCT,
                    "band_annual_pct": [TARGET_BAND_LOW_PCT, TARGET_BAND_HIGH_PCT],
                    "min_trades": MIN_TRADES},
        "ranked": ranked,
        "inactive": inactive,
        "at_risk": at_risk,
        "rule": (
            f"Judged on a rolling {days}-day window — it moves with you, so no "
            "date matters more than any other and there is nothing to be gained "
            "by taking risk you would not otherwise take. Ranking is annualized "
            "return divided by max drawdown, so a lucky swing on bad risk scores "
            "WORSE than a steady grind. Last place on that score is relegated; "
            f"fewer than {MIN_TRADES} closed trades in the window is its own "
            "failure, not a hiding place."),
    }
