"""Pre-registration store and failure log — the integrity layer.

One SHARED database across all desks, deliberately. If each of seven desks kept
its own book, each would get its own 5% false-positive budget and the effective
family-wise error rate would be ~30%. The multiple-comparison correction is only
honest if every test any desk has ever run counts toward the same total.

Two invariants the code enforces (not conventions — the API refuses):

  1. A hypothesis is registered with its pass/fail criteria and WITHOUT any
     result. ``register`` rejects a spec carrying outcome fields.
  2. A result may only be attached to a row that is still ``registered``, and
     only once. ``record_result`` refuses to overwrite, so a disappointing
     result cannot be re-run and re-recorded until it passes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

from daytrader.research.hypothesis import (
    HypothesisError,
    canonical_criteria,
    canonical_spec,
    spec_hash,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_hash      TEXT NOT NULL UNIQUE,
    registered_ts  TEXT NOT NULL,
    team           TEXT,
    name           TEXT,
    spec           TEXT NOT NULL,
    rationale      TEXT,
    criteria       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'registered',
    tested_ts      TEXT,
    test_ordinal   INTEGER,
    p_value        REAL,
    required_alpha REAL,
    result         TEXT,
    reject_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_hyp_status ON hypotheses(status);
CREATE INDEX IF NOT EXISTS idx_hyp_team ON hypotheses(team);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def research_db_path() -> str:
    """Shared research DB, alongside the team DBs."""
    from daytrader.live.competition import DATA_DIR
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.environ.get("RESEARCH_DB_PATH") or os.path.join(DATA_DIR, "research.db")


class ResearchDB:
    def __init__(self, path: str | None = None):
        self.path = path or research_db_path()
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- registration ---------------------------------------------------- #
    def register(self, spec: dict, team: str = "", rationale: str = "",
                 criteria: dict | None = None, n_periods: int = 5) -> dict:
        """Pre-register a hypothesis. No result may exist yet.

        Returns {ok, id, spec_hash} — or {ok: False, reason} when the idea has
        already been tested, which is the failure log doing its job.
        """
        canon = canonical_spec(spec)          # raises HypothesisError if malformed
        crit = canonical_criteria(criteria, n_periods)
        h = spec_hash(canon)

        prior = self.by_hash(h)
        if prior is not None:
            return {
                "ok": False,
                "duplicate": True,
                "id": prior["id"],
                "spec_hash": h,
                "status": prior["status"],
                "reason": (
                    f"already registered as #{prior['id']} by {prior['team'] or 'unknown'} "
                    f"({prior['status']}"
                    + (f": {prior['reject_reason']}" if prior["reject_reason"] else "")
                    + "). A tested idea is never silently retested."
                ),
            }
        cur = self.conn.execute(
            "INSERT INTO hypotheses (spec_hash, registered_ts, team, name, spec, "
            "rationale, criteria, status) VALUES (?,?,?,?,?,?,?, 'registered')",
            (h, _now(), team, str(spec.get("name") or canon["rule"].get("name") or "")[:60],
             json.dumps(canon, sort_keys=True, default=str), str(rationale or "")[:2000],
             json.dumps(crit, sort_keys=True)),
        )
        self.conn.commit()
        return {"ok": True, "id": int(cur.lastrowid), "spec_hash": h, "criteria": crit}

    # -- results --------------------------------------------------------- #
    def record_result(self, hyp_id: int, *, result: dict, p_value: float,
                      required_alpha: float, accepted: bool,
                      reject_reason: str = "") -> dict:
        """Attach an evaluation to a registered hypothesis, exactly once."""
        row = self.get(hyp_id)
        if row is None:
            return {"ok": False, "reason": f"no hypothesis #{hyp_id}"}
        if row["status"] != "registered":
            return {"ok": False, "reason": (
                f"#{hyp_id} is already {row['status']}; a result cannot be "
                "overwritten (that would allow re-running until it passes)")}
        self.conn.execute(
            "UPDATE hypotheses SET status=?, tested_ts=?, test_ordinal=?, p_value=?, "
            "required_alpha=?, result=?, reject_reason=? WHERE id=? AND status='registered'",
            ("accepted" if accepted else "rejected", _now(), self.next_ordinal(),
             float(p_value), float(required_alpha),
             json.dumps(result, default=str), str(reject_reason or ""), hyp_id),
        )
        self.conn.commit()
        return {"ok": True, "id": hyp_id, "accepted": bool(accepted)}

    # -- reads ----------------------------------------------------------- #
    def get(self, hyp_id: int):
        cur = self.conn.execute("SELECT * FROM hypotheses WHERE id=?", (hyp_id,))
        r = cur.fetchone()
        return dict(r) if r else None

    def by_hash(self, h: str):
        cur = self.conn.execute("SELECT * FROM hypotheses WHERE spec_hash=?", (h,))
        r = cur.fetchone()
        return dict(r) if r else None

    def pending(self, limit: int = 50) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM hypotheses WHERE status='registered' ORDER BY id LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def tested_count(self) -> int:
        """How many hypotheses have been EVALUATED — drives the correction."""
        cur = self.conn.execute(
            "SELECT COUNT(*) c FROM hypotheses WHERE status IN ('accepted','rejected')")
        return int(cur.fetchone()["c"])

    def next_ordinal(self) -> int:
        return self.tested_count() + 1

    def accepted(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM hypotheses WHERE status='accepted' ORDER BY id DESC")
        return [dict(r) for r in cur.fetchall()]

    def recent(self, limit: int = 100) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM hypotheses ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def summary(self) -> dict:
        cur = self.conn.execute(
            "SELECT status, COUNT(*) c FROM hypotheses GROUP BY status")
        counts = {r["status"]: int(r["c"]) for r in cur.fetchall()}
        tested = counts.get("accepted", 0) + counts.get("rejected", 0)
        return {
            "registered_pending": counts.get("registered", 0),
            "tested": tested,
            "accepted": counts.get("accepted", 0),
            "rejected": counts.get("rejected", 0),
            "next_test_ordinal": tested + 1,
        }

    def close(self):
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
