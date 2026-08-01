"""The gate — where almost everything dies, on purpose.

Silence is the expected output. A researcher that surfaces a winner weekly is
broken, and this module is what makes that true: the significance bar scales
with the cumulative number of hypotheses ever tested, so the 200th idea must
clear a far higher bar than the 1st.

Correction is Bonferroni over the running family: ``required_alpha =
base_alpha / n_tests_to_date``. It is deliberately harsh and deliberately
simple — every desk can see exactly why its idea died, and the raw p-value is
stored alongside, so a different policy can be applied later WITHOUT retesting
anything (retesting is what manufactures the false positives in the first place).

The count is global across all seven desks. Per-desk families would hand each
desk its own 5% budget and leave the real family-wise error rate near 30%.
"""
from __future__ import annotations


def required_alpha(base_alpha: float, n_tests_to_date: int) -> float:
    """Bonferroni-corrected threshold for the next test in the family."""
    return float(base_alpha) / max(1, int(n_tests_to_date))


# A bootstrap over N trades cannot resolve a p-value below ~1/n_boot. Once the
# corrected bar drops under that, rejection is an arithmetic artifact rather
# than evidence about the strategy, and the family needs a policy decision
# (raise base_alpha, or retire the family and start a new one) instead of
# silently rejecting everything forever.
BOOTSTRAP_RESOLUTION = 1.0 / 5000


def feasible(alpha: float) -> bool:
    """Can any evaluation still clear this bar, even in principle?"""
    return float(alpha) >= BOOTSTRAP_RESOLUTION


def decide(result: dict, criteria: dict, n_tests_to_date: int) -> dict:
    """Accept/reject a finished evaluation against its PRE-REGISTERED criteria.

    ``n_tests_to_date`` is the count INCLUDING this test, so the very first
    hypothesis faces base_alpha and the bar tightens from there.
    """
    base_alpha = float(criteria.get("base_alpha", 0.05))
    alpha = required_alpha(base_alpha, n_tests_to_date)

    if not result.get("ok"):
        return {"accepted": False, "required_alpha": alpha, "p_value": 1.0,
                "reason": f"not evaluable: {result.get('error', 'unknown')}"}

    reasons = []
    need_periods = int(criteria["min_periods_profitable"])
    got_periods = int(result["n_periods_profitable"])
    if got_periods < need_periods:
        reasons.append(
            f"profitable in {got_periods}/{result['n_periods']} periods, "
            f"pre-registered bar was {need_periods}")

    need_trades = int(criteria["min_trades"])
    if int(result["n_trades"]) < need_trades:
        reasons.append(
            f"{result['n_trades']} trades, pre-registered minimum was {need_trades} "
            "(too few to support inference)")

    if float(result["total_net_pnl"]) <= 0:
        reasons.append(f"net P&L {result['total_net_pnl']:+.2f} over the full window")

    p = float(result["p_value"])
    if p >= alpha:
        reasons.append(
            f"p={p:.5f} does not clear the corrected bar α={alpha:.6f} "
            f"(0.05/{n_tests_to_date} tests to date)")

    accepted = not reasons
    out = {
        "accepted": accepted,
        "p_value": p,
        "required_alpha": alpha,
        "n_tests_to_date": int(n_tests_to_date),
        "feasible": feasible(alpha),
        "reason": ("cleared every pre-registered criterion and the corrected "
                   f"significance bar (p={p:.5f} < α={alpha:.6f})")
        if accepted else "; ".join(reasons),
    }
    if not out["feasible"]:
        out["reason"] += (
            f" [NOTE: α={alpha:.8f} is below the bootstrap's resolution "
            f"({BOOTSTRAP_RESOLUTION:.5f}); this family can no longer accept "
            "anything on arithmetic alone — raise base_alpha or retire the family]")
    return out
