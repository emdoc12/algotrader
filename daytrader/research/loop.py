"""The research loop: drain pending hypotheses, judge them, stay quiet.

Runs on pure compute — no LLM calls, no tokens. The desks generate hypotheses
during the trading day (``propose_hypothesis``); this drains the queue after
hours and reports only what survives.
"""
from __future__ import annotations

from daytrader.research.evaluate import evaluate
from daytrader.research.gate import decide
from daytrader.research.registry import ResearchDB


def run_pending(db: ResearchDB | None = None, limit: int = 25,
                starting_equity: float = 25_000.0) -> dict:
    """Evaluate every pending hypothesis. Returns a summary; logs the survivors."""
    import json

    own = db is None
    db = db or ResearchDB()
    tested, accepted = [], []
    try:
        for row in db.pending(limit=limit):
            canon = json.loads(row["spec"])
            criteria = json.loads(row["criteria"])
            result = evaluate(canon, criteria, starting_equity=starting_equity)
            # The ordinal counts THIS test, so the bar tightens as the family grows.
            verdict = decide(result, criteria, db.next_ordinal())
            db.record_result(
                row["id"], result=result, p_value=verdict["p_value"],
                required_alpha=verdict["required_alpha"],
                accepted=verdict["accepted"],
                reject_reason="" if verdict["accepted"] else verdict["reason"])
            tested.append(row["id"])
            if verdict["accepted"]:
                accepted.append({
                    "id": row["id"], "team": row["team"], "name": row["name"],
                    "p_value": verdict["p_value"],
                    "required_alpha": verdict["required_alpha"],
                    "periods": f"{result['n_periods_profitable']}/{result['n_periods']}",
                    "n_trades": result["n_trades"],
                    "total_net_pnl": result["total_net_pnl"],
                })
        summary = db.summary()
    finally:
        if own:
            db.close()

    # Silence is the expected output — notify ONLY on a survivor.
    for a in accepted:
        try:
            from daytrader.live.competition import _notify
            _notify(
                f"🔬 Research: hypothesis #{a['id']} ({a['team']}) SURVIVED — "
                f"{a['periods']} periods profitable, {a['n_trades']} trades, "
                f"p={a['p_value']:.5f} < α={a['required_alpha']:.6f}",
                throttle_key=f"research_{a['id']}")
        except Exception:  # noqa: BLE001
            pass

    return {"tested": len(tested), "accepted": accepted, "summary": summary}
