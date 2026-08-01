"""Deployment: turning a validated hypothesis into live mechanical signals.

This is the payoff of the research loop. Without it an accepted hypothesis is
just a green row in a log — the desk still has to eyeball its own rule each
cycle and decide, which is exactly the discretionary judgement the leaderboard
showed the models have no edge at.

A deployed strategy is evaluated by the same code that validated it, on the
live frame, every cycle. The desk's job shifts from *deciding* to *supervising*:
the signal arrives already specified (symbol, side, entry, stop, target) with
its out-of-sample record attached, and the desk executes or explains why not.

Deployment is per-desk and only from that desk's OWN accepted hypotheses —
research that earns a live slot belongs to the desk that did it, and the seven
books stay independent rather than converging on identical trades.
"""
from __future__ import annotations

import json

_TABLE = """
CREATE TABLE IF NOT EXISTS deployed_strategies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    config        TEXT NOT NULL,
    universe      TEXT NOT NULL,
    evidence      TEXT,
    deployed_ts   TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
)
"""


def ensure_table(db) -> None:
    db.conn.execute(_TABLE)
    db.conn.commit()


def deploy(db, hypothesis_id: int, team: str) -> dict:
    """Promote one of this desk's ACCEPTED hypotheses to live signal generation."""
    from daytrader.live.db import _now_iso
    from daytrader.research.registry import ResearchDB

    rdb = None
    try:
        rdb = ResearchDB()
        row = rdb.get(int(hypothesis_id))
    finally:
        if rdb is not None:
            rdb.close()

    if row is None:
        return {"ok": False, "error": f"no hypothesis #{hypothesis_id}"}
    if row["status"] != "accepted":
        return {"ok": False, "error": (
            f"#{hypothesis_id} is {row['status']}, not accepted. Only a hypothesis that "
            "cleared out-of-sample validation and the corrected significance bar can be "
            "deployed — that gate is the whole point.")}
    if (row["team"] or "") != team:
        return {"ok": False, "error": (
            f"#{hypothesis_id} was proposed by '{row['team']}'. A desk deploys only its "
            "own validated research.")}

    canon = json.loads(row["spec"])
    evidence = json.loads(row["result"]) if row["result"] else {}
    ensure_table(db)
    db.conn.execute(
        "INSERT INTO deployed_strategies (hypothesis_id, name, config, universe, "
        "evidence, deployed_ts, active) VALUES (?,?,?,?,?,?,1) "
        "ON CONFLICT(hypothesis_id) DO UPDATE SET active=1, deployed_ts=excluded.deployed_ts",
        (int(hypothesis_id), row["name"] or f"hyp{hypothesis_id}",
         json.dumps(canon["rule"]), json.dumps(canon["universe"]),
         json.dumps({k: evidence.get(k) for k in
                     ("n_periods_profitable", "n_periods", "n_trades",
                      "win_rate", "total_net_pnl", "interval")}),
         _now_iso()),
    )
    db.conn.commit()
    return {"ok": True, "hypothesis_id": int(hypothesis_id),
            "name": row["name"], "universe": canon["universe"],
            "note": ("Live. Its signals now appear in the snapshot under "
                     "'deployed_signals' each cycle, already specified with entry, stop "
                     "and target. Execute them unless you have a concrete reason not to — "
                     "second-guessing a validated rule is the discretionary judgement the "
                     "research loop exists to replace.")}


def undeploy(db, hypothesis_id: int) -> dict:
    ensure_table(db)
    cur = db.conn.execute(
        "UPDATE deployed_strategies SET active=0 WHERE hypothesis_id=?", (int(hypothesis_id),))
    db.conn.commit()
    if not cur.rowcount:
        return {"ok": False, "error": f"#{hypothesis_id} is not deployed"}
    return {"ok": True, "hypothesis_id": int(hypothesis_id), "active": False}


def active(db) -> list[dict]:
    """Currently-deployed strategies for this desk."""
    try:
        ensure_table(db)
        cur = db.conn.execute(
            "SELECT * FROM deployed_strategies WHERE active=1 ORDER BY id")
        out = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["config"] = json.loads(d["config"])
                d["universe"] = json.loads(d["universe"])
                d["evidence"] = json.loads(d["evidence"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            out.append(d)
        return out
    except Exception:  # noqa: BLE001
        return []


def live_signals(db, data: dict, lookback_bars: int = 2) -> list[dict]:
    """Signals from this desk's deployed strategies on the current frame.

    Same rule, same evaluator that validated it — only the data is newer. Kept
    to the most recent bars so the desk sees what is firing NOW, not a history
    of what it missed.
    """
    deployed = active(db)
    if not deployed or not data:
        return []
    from daytrader.strategies.custom import CustomRuleStrategy
    from daytrader.core.types import SignalType

    spy_close = data["SPY"]["close"] if "SPY" in data else None
    out = []
    for d in deployed:
        try:
            strat = CustomRuleStrategy(dict(d["config"]))
        except Exception:  # noqa: BLE001 - a malformed saved rule must not break the cycle
            continue
        if spy_close is not None:
            strat._spy_close = spy_close
        for sym in d["universe"]:
            df = data.get(sym)
            if df is None or len(df) < 60:
                continue
            try:
                sigs = strat.generate(df)
            except Exception:  # noqa: BLE001
                continue
            if not sigs or len(df) <= lookback_bars:
                continue
            cutoff = df.index[-lookback_bars]
            for s in sigs:
                if getattr(s, "type", None) != SignalType.ENTRY or s.ts < cutoff:
                    continue
                out.append({
                    "symbol": s.symbol,
                    "side": s.side.value,
                    "strategy": d["name"],
                    "hypothesis_id": d["hypothesis_id"],
                    "stop": round(s.stop, 2) if s.stop else None,
                    "target": round(s.target, 2) if s.target else None,
                    "ts": s.ts.isoformat(),
                    "reason": s.reason,
                    "validation": d["evidence"],
                })
    return out
