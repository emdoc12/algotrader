"""Market-state snapshot for the agent team.

Each decision cycle, the agents need a compact, current view of the world:
where each name is trading, what the indicators and regime say, which of the
backtested strategies are firing right now, the account's live P&L, and the
team's own memory (journal) and outstanding dev requests. This module assembles
that into a plain dict the LLM tools can serialize.

It reuses the same causal indicators and strategies the backtester validated —
so the agents reason over the exact signals that were tested, not a parallel
re-implementation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

ET_ZONE = ZoneInfo("America/New_York")
import os as _os
_STOP_POLL_SEC = int(_os.environ.get("STOP_POLL_SECONDS", "120"))

from daytrader.core import indicators as ind
from daytrader.data import loader, quotes
from daytrader.portfolio.book import _SPEC, _load
from daytrader.portfolio.ensemble import Ensemble, Regime, classify_regime


def _latest_indicators(df: pd.DataFrame, live_price: float | None = None) -> dict:
    """Per-symbol indicator snapshot.

    ``price`` is the live quote (same one the broker fills at) when supplied,
    falling back to the last bar's close. ``bar_close`` always carries the
    underlying bar close for transparency.
    """
    if len(df) < 30:
        return {}
    close = df["close"]
    ema9 = ind.ema(close, 9).iloc[-1]
    ema21 = ind.ema(close, 21).iloc[-1]
    rsi = ind.rsi(close, 14).iloc[-1]
    atr = ind.atr(df, 14).iloc[-1]
    adx_series = ind.adx(df, 14)
    adx = adx_series.iloc[-1]
    adx_prev = adx_series.iloc[-4] if len(adx_series) >= 4 else adx
    adx_slope = float(adx) - float(adx_prev) if pd.notna(adx) and pd.notna(adx_prev) else 0.0
    _mline, _msig, _mhist = ind.macd(close)
    macd_hist = float(_mhist.iloc[-1]) if pd.notna(_mhist.iloc[-1]) else None
    macd_hist_prev = float(_mhist.iloc[-2]) if len(_mhist) >= 2 and pd.notna(_mhist.iloc[-2]) else None
    today_mask = df.index.normalize() == df.index[-1].normalize()
    today_df = df[today_mask]
    vwap_raw = ind.vwap_session(df).iloc[-1]
    vwap_ok = pd.notna(vwap_raw) and float(vwap_raw) > 0
    vwap = float(vwap_raw) if vwap_ok else None
    vwap_status = "ok" if vwap_ok else "undefined"
    if not vwap_ok:
        # Right-edge fallback. Near the open the session's only bar is the
        # still-forming 09:30 bar, whose volume the feed has not published yet —
        # cumulative session volume is 0, so a volume-WEIGHTED average is
        # undefined and SPY/ETF VWAP came back null in the 09:32 snapshot. Fall
        # back to the unweighted mean of today's typical prices: with a single
        # bar that IS the VWAP (weighting is irrelevant with one observation),
        # and over a handful of bars it is a close proxy. Always flagged, so the
        # desk knows it is gating off an approximation.
        tp = ((today_df["high"] + today_df["low"] + today_df["close"]) / 3.0).dropna()
        if len(tp) and float(tp.mean()) > 0:
            vwap = round(float(tp.mean()), 4)
            vwap_ok = True
            vwap_status = f"fallback_typical_mean_{len(tp)}bar"
        else:
            vwap_status = "no_intraday_bars"
    bar_close = float(close.iloc[-1])
    price = float(live_price) if live_price is not None else bar_close
    day_open = float(today_df["open"].iloc[0])
    # Data-quality guard: flag a stale/mismatched quote and unavailable VWAP so
    # the desk doesn't size/stop off bad marks. Tradeable mark = price (live
    # quote); indicators (EMA/ATR/RSI/VWAP) are derived from bar_close.
    dq = []
    dev = abs(price - bar_close) / bar_close if bar_close else 0.0
    if dev > 0.015:
        dq.append(f"quote_vs_bar_{dev * 100:.1f}pct")
    if not vwap_ok:
        dq.append(f"vwap_unavailable_{vwap_status}")
    elif vwap_status != "ok":
        dq.append(f"vwap_{vwap_status}")  # approximated — see vwap_status
    return {
        "price": round(price, 2),
        "bar_close": round(bar_close, 2),
        "day_change_pct": round((price / day_open - 1) * 100, 2) if day_open else 0.0,
        "ema9": round(float(ema9), 2),
        "ema21": round(float(ema21), 2),
        "ema_trend": "up" if ema9 > ema21 else "down",
        "rsi14": round(float(rsi), 1),
        "atr14": round(float(atr), 2),
        "atr_pct": round(float(atr) / price * 100, 2) if price else 0.0,
        "adx14": round(float(adx), 1),
        "adx_slope": round(adx_slope, 1),
        "adx_rising": adx_slope > 0,
        "macd_hist": round(macd_hist, 4) if macd_hist is not None else None,
        "macd_hist_prev": round(macd_hist_prev, 4) if macd_hist_prev is not None else None,
        "vwap": round(vwap, 2) if vwap_ok else None,
        "vwap_status": vwap_status,
        "vs_vwap_pct": round((price / vwap - 1) * 100, 2) if vwap_ok else None,
        "regime": Regime.TREND.value if adx >= 25 else Regime.RANGE.value,
        "data_quality": dq or None,
        "tradeable_mark": "price (live quote)",
        "indicator_source": "bar_close",
    }


def _fresh_signals(data: dict[str, pd.DataFrame], lookback_bars: int = 2) -> list[dict]:
    """Run every strategy and return signals stamped on the last few bars.

    Mirrors the production ensemble (regime gating + SPY market filter) so the
    agents see exactly what the automated book would act on right now.
    """
    allocs = _load([(m, c, r, w) for m, c, r, w in _SPEC])
    ens = Ensemble(allocs, market_filter=True)
    all_sigs = ens.generate(data)
    if not all_sigs:
        return []
    # Keep only signals whose decision bar is among the most recent bars.
    cutoffs = {}
    for sym, df in data.items():
        if len(df) > lookback_bars:
            cutoffs[sym] = df.index[-lookback_bars]
    fresh = []
    for s in all_sigs:
        cut = cutoffs.get(s.symbol)
        if cut is not None and s.ts >= cut:
            fresh.append({
                "symbol": s.symbol,
                "side": s.side.value,
                "strategy": s.strategy,
                "stop": round(s.stop, 2) if s.stop else None,
                "target": round(s.target, 2) if s.target else None,
                "ts": s.ts.isoformat(),
                "reason": s.reason,
            })
    return fresh


def _add_relative_strength(
    per_symbol: dict, data: dict, lookback_bars: int = 6, benchmark: str = "SPY"
) -> None:
    """Annotate each symbol's indicator block with relative strength vs SPY.

    RS = (symbol % change over the lookback) − (SPY % change over the same span).
    Computed from bars already loaded this cycle (no extra fetches). With 5m
    bars, the default 6 bars ≈ a 30-minute window. Adds ``rs_vs_spy_pct`` and a
    ``rs_rank`` (1 = strongest) to every symbol that has enough data.
    """
    def _pct(sym: str):
        df = data.get(sym)
        if df is None or len(df) <= lookback_bars:
            return None
        try:
            past = float(df["close"].iloc[-(lookback_bars + 1)])
            now = float(df["close"].iloc[-1])
            return ((now / past) - 1) * 100 if past else None
        except Exception:  # noqa: BLE001
            return None

    spy_pct = _pct(benchmark)
    if spy_pct is None:
        return
    scored = []
    for sym, inds in per_symbol.items():
        if not inds:
            continue
        p = _pct(sym)
        if p is None:
            continue
        rs = round(p - spy_pct, 2)
        inds["rs_vs_spy_pct"] = rs
        scored.append((sym, rs))
    scored.sort(key=lambda x: x[1], reverse=True)
    for rank, (sym, _rs) in enumerate(scored, 1):
        per_symbol[sym]["rs_rank"] = rank


# Coarse sector map for clustering the watchlist. Unmapped names are skipped.
_SECTORS = {
    "semis": {"NVDA", "AMD", "INTC", "MU", "AVGO", "QCOM", "TXN", "AMAT", "LRCX",
              "ASML", "TSM", "MRVL", "ON", "ADI", "KLAC", "MCHP", "NXPI",
              "SOXL", "SOXS", "SMH", "SOXX", "NVDL"},
    "mega_tech": {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NFLX",
                  "QQQ", "TQQQ", "SQQQ", "XLK"},
    "ev_auto": {"TSLA", "RIVN", "LCID", "F", "GM", "NIO"},
    "financials": {"JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "XLF"},
    "energy": {"XOM", "CVX", "OXY", "SLB", "COP", "XLE", "USO"},
    "china": {"BABA", "PDD", "JD", "BIDU", "FXI", "KWEB"},
    "crypto_proxy": {"COIN", "MSTR", "MARA", "RIOT", "BITO", "IBIT", "CLSK"},
    "index": {"SPY", "VOO", "IWM", "DIA", "VTI"},
}


def _sector_clusters(per_symbol: dict) -> list[dict]:
    """Per-sector RSI/ADX/breadth aggregates with overbought/oversold flags, so
    an exhaustion cluster (e.g. 9 semis all RSI>85) is visible on cycle 1 instead
    of requiring the trader to scan every name."""
    out = []
    for sector, members in _SECTORS.items():
        rows = [(s, v) for s, v in per_symbol.items() if s in members and v]
        if len(rows) < 2:
            continue
        rsis = [v.get("rsi14") for _, v in rows if v.get("rsi14") is not None]
        adxs = [v.get("adx14") for _, v in rows if v.get("adx14") is not None]
        slopes = [v.get("adx_slope") for _, v in rows if v.get("adx_slope") is not None]
        n = len(rows)
        ob70 = sum(1 for r in rsis if r >= 70)
        ob80 = sum(1 for r in rsis if r >= 80)
        os30 = sum(1 for r in rsis if r <= 30)
        os20 = sum(1 for r in rsis if r <= 20)
        avg_adx = round(sum(adxs) / len(adxs), 1) if adxs else None
        avg_slope = round(sum(slopes) / len(slopes), 2) if slopes else 0.0
        flag = None
        if ob70 >= 5 or ob80 >= 3:
            flag = "overbought_cluster"
        elif os30 >= 5 or os20 >= 3:
            flag = "oversold_cluster"
        out.append({
            "sector": sector, "n": n,
            "rsi_gt70": ob70, "rsi_gt80": ob80, "rsi_lt30": os30, "rsi_lt20": os20,
            "avg_adx": avg_adx, "adx_rising": avg_slope > 0, "avg_adx_slope": avg_slope,
            "flag": flag,
        })
    # Flagged clusters first, then by size.
    out.sort(key=lambda c: (c["flag"] is None, -c["n"]))
    return out


def _market_summary(per_symbol: dict) -> dict:
    """Top-level read of the tape for fast regime/trend-day detection.

    ``trend_day`` reflects whether the BROAD TAPE (SPY) is actually trending —
    SPY's own ADX is up AND its EMA trend agrees with its direction — NOT merely
    whether some single name is running. Big movers and breadth are reported as
    SEPARATE signals so a lone laggard with high ADX can't fake a trend day.
    Built from values already computed, so it's free.
    """
    rows = [(s, v) for s, v in per_symbol.items() if v]
    if not rows:
        return {}
    spy = per_symbol.get("SPY", {})
    ups = sum(1 for _, v in rows if v.get("day_change_pct", 0) > 0)
    downs = sum(1 for _, v in rows if v.get("day_change_pct", 0) < 0)
    big_movers = [
        {"symbol": s, "day_change_pct": v.get("day_change_pct"),
         "adx14": v.get("adx14"), "rs_vs_spy_pct": v.get("rs_vs_spy_pct")}
        for s, v in rows
        if abs(v.get("day_change_pct", 0)) >= 2.0 and (v.get("adx14") or 0) >= 30
    ]
    big_movers.sort(key=lambda r: abs(r.get("day_change_pct") or 0), reverse=True)

    spy_chg = spy.get("day_change_pct", 0) or 0
    spy_adx = spy.get("adx14", 0) or 0
    spy_adx_slope = spy.get("adx_slope", 0) or 0
    spy_ema_trend = spy.get("ema_trend")
    direction = "up" if spy_chg > 0 else ("down" if spy_chg < 0 else "flat")
    # Trend day = the INDEX itself is trending: real ADX, and EMA trend aligned
    # with the day's direction. A single big mover does NOT make it a trend day.
    spy_trending = (
        spy_adx >= 22
        and ((spy_ema_trend == "up" and spy_chg > 0)
             or (spy_ema_trend == "down" and spy_chg < 0))
    )
    # leaders / laggers by relative strength
    ranked = sorted((r for r in rows if r[1].get("rs_rank") is not None),
                    key=lambda r: r[1]["rs_rank"])
    leaders = [r[0] for r in ranked[:3]]
    laggers = [r[0] for r in ranked[-3:]][::-1]

    if spy_trending:
        adx_state = "rising" if spy_adx_slope > 0 else ("decaying" if spy_adx_slope < 0 else "flat")
        note = (f"TREND DAY — SPY trending {direction} (ADX {spy_adx:.0f} {adx_state}, "
                f"EMA {spy_ema_trend}). Favor with-trend entries early"
                + (" while ADX is still rising." if spy_adx_slope > 0 else "; ADX is decaying, the window may be closing."))
    else:
        note = (f"RANGE/CHOP — SPY not trending (ADX {spy_adx:.0f}, EMA {spy_ema_trend}, "
                f"{direction}). Be selective; treat single big movers as isolated, "
                "not a market-wide trend.")
        if big_movers:
            note += f" Note {len(big_movers)} isolated mover(s) running on their own."
    return {
        "trend_day": spy_trending,
        "spy_trending": spy_trending,
        "spy_day_change_pct": round(float(spy_chg), 2),
        "spy_adx14": round(float(spy_adx), 1),
        "spy_adx_slope": round(float(spy_adx_slope), 1),
        "spy_adx_rising": spy_adx_slope > 0,
        "spy_ema_trend": spy_ema_trend,
        "spy_direction": direction,
        "breadth": {"advancers": ups, "decliners": downs, "total": len(rows)},
        "big_movers": big_movers[:8],
        "rs_leaders": leaders,
        "rs_laggers": laggers,
        "sector_clusters": _sector_clusters(per_symbol),
        "note": note,
    }


def _add_rs_persistence(per_symbol: dict, data: dict, benchmark: str = "SPY") -> None:
    """Annotate each name with how STABLE its relative strength has been this
    session — not just who leads right now. Targets the recurring loss pattern of
    entering a one-bar 'RS leader' that flips to laggard in 15-20 min.

    Adds per symbol:
      rs_persistence   — fraction of the session's bars the name's RS line was
                         positive vs SPY (0-1); high = durable leadership.
      rs_slope_20m/60m — change in the RS line (pct pts) over ~20m / ~60m;
                         positive = leadership accelerating, negative = decaying.
      rs_rank_change_20m — cross-sectional rank moved over ~20m (+ = climbed).
      rs_stable        — bool: durable leader (persistence>=0.7 & slope_20m>=0)
                         or durable laggard (persistence<=0.3 & slope_20m<=0).
    """
    B20, B60 = 4, 12  # 5m bars ≈ 20 / 60 minutes

    def _today_close(df):
        d = df.index[-1].normalize()
        c = df[df.index.normalize() == d]["close"]
        return c

    spy = data.get(benchmark)
    if spy is None or len(spy) == 0:
        return
    spy_c = _today_close(spy)
    if len(spy_c) < 2:
        return
    spy_ret = spy_c / float(spy_c.iloc[0]) - 1.0

    rs_lines: dict[str, "pd.Series"] = {}
    for sym in list(per_symbol):
        if not per_symbol[sym]:
            continue
        df = data.get(sym)
        if df is None or len(df) == 0:
            continue
        c = _today_close(df)
        if len(c) < 2:
            continue
        sym_ret = c / float(c.iloc[0]) - 1.0
        aligned = pd.concat([sym_ret, spy_ret], axis=1, join="inner").dropna()
        if len(aligned) < 2:
            continue
        rs_lines[sym] = aligned.iloc[:, 0] - aligned.iloc[:, 1]  # RS line (fraction)

    if not rs_lines:
        return
    rs_df = pd.DataFrame(rs_lines)
    ranks = rs_df.rank(axis=1, ascending=False)  # 1 = strongest RS

    for sym, rs in rs_lines.items():
        n = len(rs)
        last = float(rs.iloc[-1])
        persistence = round(float((rs.tail(B60) > 0).mean()), 2)
        slope20 = round((last - float(rs.iloc[-1 - B20])) * 100, 2) if n > B20 else None
        slope60 = round((last - float(rs.iloc[-1 - B60])) * 100, 2) if n > B60 else None
        rank_chg = None
        if len(ranks) > B20:
            rn, rt = ranks[sym].iloc[-1], ranks[sym].iloc[-1 - B20]
            if pd.notna(rn) and pd.notna(rt):
                rank_chg = int(rt - rn)  # positive = climbed the rankings
        stable = ((persistence >= 0.7 and (slope20 or 0) >= 0)
                  or (persistence <= 0.3 and (slope20 or 0) <= 0))
        per_symbol[sym]["rs_persistence"] = persistence
        per_symbol[sym]["rs_slope_20m"] = slope20
        per_symbol[sym]["rs_slope_60m"] = slope60
        per_symbol[sym]["rs_rank_change_20m"] = rank_chg
        per_symbol[sym]["rs_stable"] = bool(stable)


def _macd_trigger(per_symbol: dict, spy_direction: str | None, now_t) -> dict:
    """The desk's one proven A+ setup, mechanized: a FRESH MACD with-trend cross
    on a non-extended, rising-ADX>=25 name aligned with SPY, in the 10:00-14:00
    window. Removes the per-cycle manual reconstruction of the only edge that pays."""
    from datetime import time as _dtime
    in_window = _dtime(10, 0) <= now_t <= _dtime(14, 0) if now_t is not None else True
    hits = []
    for sym, v in per_symbol.items():
        if not v:
            continue
        h, hp = v.get("macd_hist"), v.get("macd_hist_prev")
        ema_trend, adx, rising = v.get("ema_trend"), v.get("adx14") or 0, v.get("adx_rising")
        price, ema9, atr = v.get("price"), v.get("ema9"), v.get("atr14")
        if h is None or hp is None or not atr or price is None or ema9 is None:
            continue
        dist = abs(price - ema9) / atr
        # Fresh sign flip this bar, in the direction of the name's EMA trend.
        cross_up = h > 0 >= hp
        cross_dn = h < 0 <= hp
        side = None
        if cross_up and ema_trend == "up" and spy_direction == "up":
            side = "long"
        elif cross_dn and ema_trend == "down" and spy_direction == "down":
            side = "short"
        if side is None or adx < 25 or not rising or dist > 1.5:
            continue
        hits.append({
            "symbol": sym, "side": side, "macd_hist": h, "macd_hist_prev": hp,
            "adx14": adx, "adx_slope": v.get("adx_slope"),
            "dist_from_ema9_atr": round(dist, 2), "vs_vwap_pct": v.get("vs_vwap_pct"),
            "rs_rank": v.get("rs_rank"),
        })
    hits.sort(key=lambda r: (r.get("rs_rank") or 99))
    note = ("A+ fresh MACD with-trend cross candidates (ADX>=25 rising, within 1.5xATR of "
            "EMA9, SPY-aligned).")
    if not in_window:
        note += " NOTE: outside the 10:00-14:00 edge window — watch-only."
    return {"in_window": bool(in_window), "count": len(hits), "note": note, "triggers": hits}


def _rollover_short_trigger(per_symbol: dict, summary: dict, now_t) -> dict:
    """The desk's second validated co-primary setup, mechanized: the saved
    'trend_day_ema9_rollover_short' (backtested PF 2.15 / 60% win / +12.3 alpha,
    in-window 10:00-14:00).

    Flags names where the down-trend is RE-EXPANDING rather than merely present:
    EMA stack down, ADX >= 25 and rising, MACD histogram making a new low below
    zero (hist < hist_prev < 0), RSI > 35 (skip names already flushed into an
    oversold hole), price just under VWAP (-1.5% .. 0%), and SPY itself trending
    down with rising ADX. Mirrors ``macd_trigger`` so the desk can fire the
    instant it prints instead of hand-checking hist vs hist_prev on every
    EMA-down name and arriving after the 14:00 gate.
    """
    from datetime import time as _dtime
    in_window = _dtime(10, 0) <= now_t <= _dtime(14, 0) if now_t is not None else True
    spy_dir = (summary or {}).get("spy_direction")
    spy_rising = bool((summary or {}).get("spy_adx_rising"))
    spy_aligned = (spy_dir == "down") and spy_rising

    hits, near_miss = [], 0
    for sym, v in per_symbol.items():
        if not v:
            continue
        h, hp = v.get("macd_hist"), v.get("macd_hist_prev")
        adx, slope = v.get("adx14"), v.get("adx_slope")
        rsi, vs_vwap = v.get("rsi14"), v.get("vs_vwap_pct")
        price, ema9, atr = v.get("price"), v.get("ema9"), v.get("atr14")
        if h is None or hp is None or adx is None or rsi is None or vs_vwap is None:
            continue
        if price is None or ema9 is None or not atr:
            continue
        checks = (
            v.get("ema_trend") == "down",          # EMA9 < EMA21
            adx >= 25 and (slope or 0) > 0,        # trending AND strengthening
            h < hp < 0,                            # hist re-expanding DOWN
            rsi > 35,                              # not already flushed
            -1.5 <= vs_vwap <= 0,                  # just under VWAP
        )
        if not all(checks):
            if sum(checks) == len(checks) - 1:
                near_miss += 1
            continue
        # Name-level conditions all pass; SPY alignment is the shared gate.
        if not spy_aligned:
            near_miss += 1
            continue
        hits.append({
            "symbol": sym,
            "adx14": adx,
            "adx_slope": slope,
            "macd_hist": h,
            "macd_hist_prev": hp,
            "rsi14": rsi,
            "vs_vwap_pct": vs_vwap,
            "dist_from_ema9_atr": round(abs(price - ema9) / atr, 2),
            "rs_rank": v.get("rs_rank"),
        })
    # Weakest relative strength first — the best short candidates.
    hits.sort(key=lambda r: -(r.get("rs_rank") or 0))

    note = ("trend_day_ema9_rollover_short candidates: EMA-down, ADX>=25 rising, "
            "MACD hist re-expanding down (hist<hist_prev<0), RSI>35, price 0 to "
            "-1.5% vs VWAP, SPY down with rising ADX.")
    if not spy_aligned:
        note += (f" BLOCKED: SPY gate not met (direction={spy_dir!r}, "
                 f"adx_rising={spy_rising}) — no short qualifies this cycle.")
    if not in_window:
        note += " NOTE: outside the 10:00-14:00 edge window — watch-only."
    return {
        "in_window": bool(in_window),
        "spy_aligned": bool(spy_aligned),
        "count": len(hits),
        "near_miss_count": near_miss,
        "note": note,
        "triggers": hits,
    }


def _ema_scan(per_symbol: dict) -> dict:
    """Pre-stage EMA-pullback structure for the 9:30-10:00 window so the desk has
    entry zones ready at the bell instead of doing manual analysis after ADX has
    already decayed. Classifies each name as a long/short candidate by EMA stack
    and reports distance-from-EMA9 (in ATR), ADX + slope, VWAP position, gap."""
    longs, shorts = [], []
    for sym, v in per_symbol.items():
        if not v:
            continue
        price, ema9, ema21, atr = v.get("price"), v.get("ema9"), v.get("ema21"), v.get("atr14")
        if price is None or ema9 is None or ema21 is None or not atr:
            continue
        row = {
            "symbol": sym, "price": price, "ema9": ema9, "ema21": ema21,
            "dist_from_ema9_atr": round((price - ema9) / atr, 2),
            "adx14": v.get("adx14"), "adx_slope": v.get("adx_slope"),
            "adx_rising": v.get("adx_rising"), "vs_vwap_pct": v.get("vs_vwap_pct"),
            "day_change_pct": v.get("day_change_pct"), "gap_pct": v.get("gap_pct"),
            "rs_rank": v.get("rs_rank"),
        }
        if price > ema21 and ema9 > ema21:
            longs.append(row)
        elif price < ema21 and ema9 < ema21:
            shorts.append(row)
    # Rank by real trend strength (high ADX) then shallow pullback (small |dist|).
    _key = lambda r: (-(r.get("adx14") or 0), abs(r.get("dist_from_ema9_atr") or 99))
    longs.sort(key=_key)
    shorts.sort(key=_key)
    return {
        "note": ("EMA-pullback candidates pre-staged for the open. Prefer names with "
                 "ADX>=25 and RISING slope (adx_rising) and a shallow pullback "
                 "(|dist_from_ema9_atr| small); skip decaying ADX — that's the "
                 "post-open trap the desk keeps hitting."),
        "long_candidates": longs[:8],
        "short_candidates": shorts[:8],
    }


def _default_symbols(top_n: int | None = None) -> list[str]:
    """The day's watchlist from the scanner; falls back to the core universe.
    Size honors the WATCHLIST_SIZE env var (default 18)."""
    import os
    if top_n is None:
        try:
            top_n = int(os.environ.get("WATCHLIST_SIZE", "18"))
        except (TypeError, ValueError):
            top_n = 18
    try:
        from daytrader.data.universe import watchlist
        syms = watchlist(top_n=top_n)
    except Exception:  # noqa: BLE001 - universe module optional / scan hiccup
        syms = list(loader.DEFAULT_UNIVERSE)
    # Micro futures, so the desks can actually SEE the contracts they're allowed
    # to trade (indicators, VWAP, regime) instead of trading them blind. Off the
    # scanner's radar by construction — it ranks equities. Set FUTURES_SYMBOLS=""
    # to disable, or to a comma-list to choose your own.
    raw = os.environ.get("FUTURES_SYMBOLS", "MES=F,MNQ=F")
    fut = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if fut:
        from daytrader.core.contracts import spec_for
        syms = syms + [s for s in fut if s not in syms and spec_for(s) is not None]
    return syms


def market_only(symbols: list[str] | None = None, interval: str = "5m") -> dict:
    """The shared market view: prices, indicators, regime, fresh signals.

    Account/memory state is NOT included so this can be computed ONCE per cycle
    and reused across all competing teams (one data fetch, not N). The price
    inside each market[sym] entry is the live quote from
    :mod:`daytrader.data.quotes` — the same number the broker uses for fills,
    so there is no feed-vs-broker gap within a cycle.
    """
    symbols = symbols or _default_symbols()
    data = loader.load_many(symbols, interval=interval, max_age_hours=0.1)
    # Ensure SPY bars are available as the relative-strength benchmark even if
    # it isn't on the day's watchlist.
    if "SPY" not in data:
        try:
            data["SPY"] = loader.load("SPY", interval=interval, max_age_hours=0.1)
        except Exception:  # noqa: BLE001
            pass
    quote_map = quotes.get_quotes(symbols)
    per_symbol = {sym: _latest_indicators(df, live_price=quote_map.get(sym))
                  for sym, df in data.items() if sym in symbols}
    _add_relative_strength(per_symbol, data)
    _add_rs_persistence(per_symbol, data)
    fresh = _fresh_signals(data)
    now_et = datetime.now(timezone.utc).astimezone()
    summary = _market_summary(per_symbol)
    now_et_t = datetime.now(ET_ZONE).time()
    out = {
        "timestamp": now_et.isoformat(),
        "universe": symbols,
        "interval": interval,
        "market": per_symbol,
        "market_summary": summary,
        "macd_trigger": _macd_trigger(per_symbol, summary.get("spy_direction"), now_et_t),
        "rollover_short_trigger": _rollover_short_trigger(per_symbol, summary, now_et_t),
        "ema_scan": _ema_scan(per_symbol),
        "fresh_signals": fresh,
        "quotes": quote_map,
    }
    # Optional enrichment: if the owner has configured tastytrade, overlay live
    # READ-ONLY quotes + option chains/Greeks. Degrades to Yahoo-only otherwise.
    try:
        from daytrader.live import tastytrade_data
        if tastytrade_data.is_configured():
            out = tastytrade_data.enrich_snapshot(out)
    except Exception:  # noqa: BLE001 - enrichment is best-effort, never fatal
        pass
    return out


def with_account(market_snap: dict, broker) -> dict:
    """Overlay one team's account state + memory onto a shared market snapshot.

    Also fetches indicators + a live quote for any HELD position whose symbol
    is not on the day's scanned universe, so the trader never has to manage a
    position blind.
    """
    out = dict(market_snap)
    if broker is None:
        return out
    try:
        out["account"] = broker.snapshot()
        out["performance"] = broker.performance()
    except Exception as e:  # noqa: BLE001
        out["account_error"] = str(e)

    # Signals from THIS desk's deployed (out-of-sample-validated) strategies.
    # Per-team, so it belongs here rather than in the shared market snapshot.
    try:
        from daytrader.research.deploy import live_signals
        interval = market_snap.get("interval", "5m")
        syms = list(market_snap.get("universe") or [])
        data = loader.load_many(syms, interval=interval, max_age_hours=0.1) if syms else {}
        sigs = live_signals(broker.db, data)
        if sigs:
            out["deployed_signals"] = {
                "count": len(sigs),
                "signals": sigs,
                "note": ("Fired by YOUR deployed strategies — rules that already cleared "
                         "hard out-of-sample validation and the corrected significance "
                         "bar. Each carries its validation record. Execute them unless "
                         "you have a concrete, stateable reason not to; overriding a "
                         "validated rule on a hunch is the discretionary call the "
                         "research loop exists to remove."),
            }
    except Exception:  # noqa: BLE001 - deployment must never break a trade cycle
        pass

    # Held positions outside the day's scan need live indicators too, or the
    # trader is flying blind on what it already owns.
    try:
        positions = (out.get("account") or {}).get("positions") or []
        market = dict(out.get("market") or {})
        held_extra = sorted({(p.get("symbol") or "").upper()
                             for p in positions
                             if p.get("symbol") and p["symbol"].upper() not in market})
        if held_extra:
            interval = market_snap.get("interval", "5m")
            extra_data = loader.load_many(list(held_extra), interval=interval, max_age_hours=0.1)
            extra_quotes = quotes.get_quotes(held_extra)
            for sym, df in extra_data.items():
                inds = _latest_indicators(df, live_price=extra_quotes.get(sym))
                if inds:
                    market[sym] = inds
            out["market"] = market
            extra_quote_map = dict(out.get("quotes") or {})
            extra_quote_map.update(extra_quotes)
            out["quotes"] = extra_quote_map
            out["held_positions_added"] = list(held_extra)
    except Exception as e:  # noqa: BLE001 - never break the snapshot
        out["held_indicator_error"] = str(e)

    db = getattr(broker, "db", None)
    if db is not None:
        try:
            out["journal"] = db.recent_journal(limit=40)
        except Exception:  # noqa: BLE001
            pass
        # Cross-session memory: surface the newest lessons/plans/risk notes even
        # when buried past the recency window — so EOD Reviewer findings reliably
        # reach the next day's planner (fixes "review lessons never carry forward").
        try:
            out["recent_lessons"] = db.recent_journal_by_topics(
                ("lesson", "plan", "risk", "review"), limit=15)
        except Exception:  # noqa: BLE001
            pass
        try:
            out["open_dev_requests"] = db.open_dev_requests()
        except Exception:  # noqa: BLE001
            pass
        # This session's exits + realized P&L, so the on-cycle trader SEES when a
        # server-side stop/target fired (a flat book alone can't tell a banked
        # target from a stopped-out loss). Addresses the "traded on a stale mental
        # model of the book" P&L leak.
        try:
            from daytrader.live import analytics as _an
            today_et = datetime.now(ET_ZONE).date()
            exits, realized = [], 0.0
            for tr in db.recent_trades(limit=100):
                ex = _an._to_et(tr.get("exit_ts"))
                if ex is None or ex.date() != today_et:
                    continue
                pnl = tr.get("pnl")
                if pnl is not None:
                    realized += float(pnl)
                if len(exits) < 12:
                    exits.append({
                        "symbol": tr.get("symbol"),
                        "side": tr.get("side"),
                        "exit_reason": tr.get("exit_reason"),
                        "pnl": round(float(pnl), 2) if pnl is not None else None,
                        "exit_ts": tr.get("exit_ts"),
                    })
            out["recent_exits"] = exits            # newest first, this session
            out["session_realized_pnl"] = round(realized, 2)
        except Exception:  # noqa: BLE001
            pass
        # Risk audit: planned vs realized, flagging stop-throughs (realized loss
        # materially exceeding the planned entry→stop risk). Grounds the "stop
        # filled far past my planned risk" complaint in data.
        try:
            audit = []
            for tr in db.recent_trades(limit=60):
                if not tr.get("risk_overrun"):
                    continue
                audit.append({
                    "symbol": tr.get("symbol"), "side": tr.get("side"),
                    "planned_risk": tr.get("planned_risk"), "realized_pnl": tr.get("pnl"),
                    "exit_reason": tr.get("exit_reason"), "exit_ts": tr.get("exit_ts"),
                })
            out["risk_audit"] = {
                "stop_throughs": audit[:10],
                "note": ("Trades where realized loss exceeded planned entry→stop risk "
                         "by >25% — usually stop-through between polls on a fast move."),
            }
        except Exception:  # noqa: BLE001
            pass
    # Stop-execution transparency: stops are POLLED (server-side each cycle + a
    # faster between-cycle poll), NOT continuous native exchange stops. Size for
    # gap risk — a fast move can fill worse than the stop between polls.
    out["stop_execution"] = {
        "mode": "cycle_polled",
        "poll_interval_sec": _STOP_POLL_SEC,
        "note": ("Stops/targets/trailing/auto-scale are enforced server-side on each "
                 "trade cycle AND on a faster between-cycle poll — but not tick-by-tick. "
                 "A fast move can fill past your stop between polls (stop-through); size "
                 "for that gap risk, especially on 2x/3x levered ETFs."),
    }
    return out


def snapshot(broker=None, symbols: list[str] | None = None, interval: str = "5m") -> dict:
    """Full market + account snapshot for a single team (convenience wrapper)."""
    return with_account(market_only(symbols, interval), broker)
