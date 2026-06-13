"""Connectivity self-test for the team model APIs.

Makes a tiny live call to each configured provider and reports whether the key
works, so the owner can verify all four teams from the dashboard or CLI without
waiting for the market to open. Uses each team's own model/endpoint, so it tests
exactly what the desk will use.
"""
from __future__ import annotations

import time

PING_SYSTEM = "You are a connectivity test. Reply with exactly the word: OK"
PING_USER = "ping"


def check_providers() -> list[dict]:
    """Ping every team's model. Returns one row per team:
    {team, model, configured, ok, latency_ms, detail}."""
    from daytrader.live import settings
    from daytrader.live.providers import default_team_providers, has_key

    settings.apply_to_env()
    rows = []
    for name, provider in default_team_providers().items():
        model = getattr(provider, "model", "?")
        if not has_key(provider):
            rows.append({"team": name, "model": model, "configured": False,
                         "ok": False, "latency_ms": 0, "detail": "no API key set"})
            continue
        t0 = time.time()
        res = provider.run_loop(PING_SYSTEM, tools=[], handlers={},
                                user_message=PING_USER, max_tokens=20, max_iterations=1)
        ms = int((time.time() - t0) * 1000)
        ok = bool(res.text) and not res.error and not res.refused
        if res.error:
            detail = res.error
        elif res.refused:
            detail = "refused"
        else:
            detail = (res.text or "").strip()[:80] or "(empty reply)"
        rows.append({"team": name, "model": model, "configured": True,
                     "ok": ok, "latency_ms": ms, "detail": detail})
    return rows


def health_snapshot() -> dict:
    """Cheap, DB-only health view (no live API calls) for the Health tab to
    poll: market/data status, per-team status, recent errors, dev requests."""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    from daytrader.live import settings, tastytrade_data
    from daytrader.live.competition import START_CASH, team_db_path
    from daytrader.live.db import LiveDB
    from daytrader.live.providers import default_team_providers, has_key

    settings.apply_to_env()
    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    market_open = now.weekday() < 5 and dtime(9, 30) <= now.time() < dtime(16, 0)
    today = now.date().isoformat()

    teams, recent_errors, dev = [], [], []
    for name, provider in default_team_providers().items():
        row = {"team": name, "model": getattr(provider, "model", "?"),
               "configured": has_key(provider), "equity": round(START_CASH, 2),
               "halted": False, "errors_today": 0, "last_cycle": None, "open_positions": 0}
        try:
            db = LiveDB(team_db_path(name))
            last = db.last_equity()
            if last:
                row["equity"] = round(float(last.get("equity", START_CASH)), 2)
            row["open_positions"] = len(db.load_open_positions())
            log = db.recent_agent_log(limit=200)
            if log:
                row["last_cycle"] = log[0].get("ts")
            for e in log:
                ts = e.get("ts", ""); act = e.get("action", ""); detail = (e.get("detail") or "")
                problem = act in ("error", "circuit_breaker") or "refus" in detail.lower() \
                    or "error" in detail.lower() or "exception" in detail.lower()
                if act == "circuit_breaker":
                    row["halted"] = True
                if problem:
                    if ts[:10] == today:
                        row["errors_today"] += 1
                    if len(recent_errors) < 50:
                        recent_errors.append({"ts": ts, "team": name, "agent": e.get("agent", ""),
                                              "action": act, "detail": detail[:180]})
            for d in db.open_dev_requests():
                dev.append({"team": name, "ts": d.get("ts"), "title": d.get("title"),
                            "status": d.get("status"), "url": d.get("url")})
            db.close()
        except Exception:  # noqa: BLE001
            pass
        teams.append(row)

    recent_errors.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return {
        "now_et": now.isoformat(),
        "market_open": market_open,
        "data_feed": {"yahoo": True, "tastytrade_configured": tastytrade_data.is_configured()},
        "teams": teams,
        "recent_errors": recent_errors[:50],
        "open_dev_requests": dev,
    }
