#!/usr/bin/env python3
"""Dump the competition's full results as JSON, for reporting and analysis.

Reads every desk's database and produces one compact document: headline
numbers per desk plus the same trade record cut by strategy, direction,
trend-alignment, time of day, breadth, sector, exit reason, horizon, symbol,
month — and by MARKET REGIME, which is reconstructed here rather than read,
since nothing has been recording "was this a bull tape" at entry time.

Run it on the machine holding the desk databases::

    python tools/competition_report.py                 # -> stdout + report.json
    python tools/competition_report.py --out /tmp/r.json --min-trades 3

Regime reconstruction is deliberately conservative: a trade is labelled from
SPY's own daily bars around its entry date, and if those bars cannot be
loaded the dimension is omitted entirely rather than guessed at. An
unlabelled trade is not evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daytrader.live import analytics  # noqa: E402
from daytrader.live.db import LiveDB  # noqa: E402


def _data_dir() -> str:
    return (os.environ.get("DAYTRADER_DATA_DIR")
            or os.path.dirname(os.environ.get("DAYTRADER_DB_PATH", ""))
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache"))


def _desk_dbs(data_dir: str) -> dict:
    out = {}
    for fn in sorted(os.listdir(data_dir)):
        if fn.startswith("team_") and fn.endswith(".db"):
            out[fn[5:-3]] = os.path.join(data_dir, fn)
    return out


# --------------------------------------------------------------------------- #
# market regime, reconstructed from SPY                                       #
# --------------------------------------------------------------------------- #
def _spy_regime_by_date() -> dict:
    """{date_iso: {"regime": bull|bear|flat, "vol": calm|normal|high"}}.

    Bull/bear is SPY's 20-session return at that date (>+2% / <-2%), which is
    the plain-language question "was the tape going up while we traded". Vol
    buckets the 20-session realized move so a 0.3% day and a 3% day are not
    pooled. Returns {} if SPY daily bars are unavailable — callers then drop
    the dimension instead of labelling trades from nothing.
    """
    try:
        from daytrader.data import loader
        df = loader.load("SPY", interval="1d", max_age_hours=24)
        if df is None or len(df) < 30:
            return {}
        close = df["close"]
        ret20 = close.pct_change(20) * 100.0
        vol20 = close.pct_change().rolling(20).std() * (252 ** 0.5) * 100.0
        out = {}
        for ts, r, v in zip(df.index, ret20, vol20):
            if r != r:      # NaN
                continue
            regime = "bull" if r > 2.0 else ("bear" if r < -2.0 else "flat")
            if v != v:
                vol = "unknown"
            else:
                vol = "calm" if v < 12 else ("high" if v > 22 else "normal")
            out[str(ts.date())] = {"regime": regime, "vol": vol,
                                   "spy_ret20_pct": round(float(r), 2)}
        return out
    except Exception:  # noqa: BLE001 - the dimension is optional, never fatal
        return {}


def _tag_regime(trades: list, by_date: dict) -> int:
    """Annotate trades in place with regime/vol. Returns how many got labelled."""
    n = 0
    for t in trades:
        d = str(t.get("entry_ts") or "")[:10]
        info = by_date.get(d)
        if info:
            t["market_regime"] = info["regime"]
            t["market_vol"] = info["vol"]
            n += 1
        else:
            t["market_regime"] = "unknown"
            t["market_vol"] = "unknown"
    return n


# --------------------------------------------------------------------------- #
# grouping                                                                    #
# --------------------------------------------------------------------------- #
def _group(trades: list, keyfn, min_trades: int) -> list:
    buckets = defaultdict(list)
    for t in trades:
        buckets[keyfn(t)].append(float(t.get("pnl") or 0.0))
    rows = []
    for k, pnls in buckets.items():
        if len(pnls) < min_trades:
            continue
        st = analytics._stats(pnls)
        st["group"] = str(k)
        # Expectancy per trade is the number that actually decides whether a
        # bucket is worth repeating — a 70% win rate with a worse average loss
        # than average win is a losing bucket.
        st["expectancy"] = round(st["total_pnl"] / st["n_trades"], 2)
        rows.append(st)
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)
    return rows


def _equity_curve_stats(db) -> dict:
    try:
        rows = db.conn.execute(
            "SELECT ts, equity FROM equity_snapshots ORDER BY id ASC").fetchall()
    except Exception:  # noqa: BLE001
        return {}
    if not rows:
        return {}
    eq = [float(r["equity"]) for r in rows]
    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)
    return {"first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"],
            "n_snapshots": len(eq), "peak_equity": round(max(eq), 2),
            "trough_equity": round(min(eq), 2), "max_drawdown_pct": round(max_dd, 2)}


def _desk_report(name: str, path: str, by_date: dict, min_trades: int) -> dict:
    db = LiveDB(path)
    try:
        trades = db.recent_trades(limit=100000)
        labelled = _tag_regime(trades, by_date)
        pnls = [float(t.get("pnl") or 0.0) for t in trades]
        last = db.last_equity() or {}
        try:
            capital_base = float(os.environ.get("START_EQUITY", "25000")) + db.capital_contributed()
        except Exception:  # noqa: BLE001
            capital_base = float(os.environ.get("START_EQUITY", "25000"))
        equity = float(last.get("equity") or capital_base)
        head = analytics._stats(pnls)
        head.update({
            "equity": round(equity, 2),
            "capital_base": round(capital_base, 2),
            "pnl_vs_base": round(equity - capital_base, 2),
            "return_pct": round((equity / capital_base - 1) * 100, 2) if capital_base else 0.0,
            "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
            "trades_labelled_with_regime": labelled,
        })
        head.update(_equity_curve_stats(db))
        try:
            head["cost_total_usd"] = round(db.usage_totals()["cost_usd"], 2)
        except Exception:  # noqa: BLE001
            pass

        dims = {
            "strategy": lambda t: analytics.canonical_strategy(t.get("strategy")),
            "direction": lambda t: str(t.get("side") or "?"),
            "with_trend": lambda t: str(t.get("with_trend") or "unknown"),
            "tod_bucket": lambda t: analytics.tod_bucket(t.get("entry_ts")),
            "exit_reason": lambda t: str(t.get("exit_reason") or "unknown"),
            "symbol": lambda t: str(t.get("symbol") or "?"),
            "month": lambda t: str(t.get("entry_ts") or "")[:7] or "unknown",
        }
        if by_date:
            dims["market_regime"] = lambda t: t.get("market_regime", "unknown")
            dims["market_vol"] = lambda t: t.get("market_vol", "unknown")
        breakdown = {k: _group(trades, fn, min_trades) for k, fn in dims.items()}

        # Options are booked separately from share trades; count them so the
        # report cannot silently present an equity-only picture as the whole book.
        try:
            n_opt = db.conn.execute(
                "SELECT COUNT(*) c FROM option_positions").fetchone()["c"]
            n_opt_closed = db.conn.execute(
                "SELECT COUNT(*) c FROM option_positions WHERE status!='open'").fetchone()["c"]
            head["option_structures_total"] = int(n_opt)
            head["option_structures_closed"] = int(n_opt_closed)
        except Exception:  # noqa: BLE001
            pass
        try:
            head["journal_entries"] = db.conn.execute(
                "SELECT COUNT(*) c FROM journal").fetchone()["c"]
            head["hypotheses"] = db.conn.execute(
                "SELECT COUNT(*) c FROM hypotheses").fetchone()["c"]
        except Exception:  # noqa: BLE001
            pass
        return {"headline": head, "breakdown": breakdown}
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=None)
    # Default INTO the data dir, not the working directory: in the container
    # /app is ephemeral and invisible from the host, so a report written there
    # is one `docker restart` from gone and unreachable meanwhile. /app/data is
    # the mounted volume — the file lands on the host share where the owner can
    # actually open it.
    ap.add_argument("--out", default=None,
                    help="output path (default: <data-dir>/competition_report.json)")
    ap.add_argument("--min-trades", type=int, default=2,
                    help="drop breakdown buckets thinner than this (default 2)")
    ap.add_argument("--top", type=int, default=4,
                    help="buckets per dimension in the printed summary (default 4)")
    args = ap.parse_args()

    data_dir = args.data_dir or _data_dir()
    if not args.out:
        args.out = os.path.join(data_dir, "competition_report.json")
    dbs = _desk_dbs(data_dir)
    if not dbs:
        print(f"no team_*.db found in {data_dir}", file=sys.stderr)
        return 1

    by_date = _spy_regime_by_date()
    report = {
        "generated": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "data_dir": data_dir,
        "regime_source": ("SPY daily bars (20-session return: bull >+2%, bear <-2%)"
                          if by_date else "UNAVAILABLE — regime dimension omitted"),
        "desks": {},
    }
    for name, path in dbs.items():
        try:
            report["desks"][name] = _desk_report(name, path, by_date, args.min_trades)
        except Exception as e:  # noqa: BLE001 - one bad desk must not lose the rest
            report["desks"][name] = {"error": repr(e)}

    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=1, default=str)

    print(f"wrote {args.out}  ({os.path.getsize(args.out)/1024:.0f} KB)")
    print(f"regime source: {report['regime_source']}\n")
    print(f"{'desk':<10}{'return%':>9}{'P&L':>11}{'PF':>7}{'win%':>7}{'trades':>8}"
          f"{'maxDD%':>8}{'exp/trade':>11}")
    rows = [(n, d.get("headline", {})) for n, d in report["desks"].items() if "headline" in d]
    for n, h in sorted(rows, key=lambda r: r[1].get("return_pct", 0), reverse=True):
        pf = h.get("profit_factor")
        print(f"{n:<10}{h.get('return_pct', 0):>9.2f}{h.get('pnl_vs_base', 0):>11,.0f}"
              f"{(pf if pf is not None else float('inf')):>7.2f}{h.get('win_rate', 0):>7.1f}"
              f"{h.get('n_trades', 0):>8}{h.get('max_drawdown_pct', 0):>8.2f}"
              f"{h.get('expectancy', 0):>11,.2f}")

    # A compact text digest, sized to be COPIED out of a terminal. The JSON is
    # the complete record, but it lands inside a container on a NAS — a report
    # nobody can get at explains nothing, so the findings that matter print here.
    KEY_DIMS = ("strategy", "exit_reason", "with_trend", "market_regime", "tod_bucket")
    for n, d in sorted(report["desks"].items()):
        bd = d.get("breakdown") or {}
        if not bd:
            continue
        print(f"\n{'='*74}\n{n.upper()}")
        for dim in KEY_DIMS:
            groups = bd.get(dim) or []
            if not groups:
                continue
            # Best and worst by TOTAL P&L: the tails are where a decision lives.
            show = groups[:args.top]
            if len(groups) > args.top:
                show = show + [None] + groups[-min(args.top, len(groups) - args.top):]
            print(f"  {dim}:")
            for g in show:
                if g is None:
                    print(f"    {'...':<22}")
                    continue
                pf = g.get("profit_factor")
                pf_s = "inf" if pf is None else f"{pf:.2f}"
                print(f"    {g['group'][:22]:<22} n={g['n_trades']:<4} "
                      f"pnl={g['total_pnl']:>10,.0f}  pf={pf_s:<5} "
                      f"win={g['win_rate']:>5.1f}%  exp={g['expectancy']:>8,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
