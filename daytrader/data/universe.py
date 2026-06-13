"""Tradeable universe + daily liquidity/volatility scanner.

The system historically traded only SPY + the Mag7. This module widens the net
to the broad range of liquid US **stocks and ETFs** (options live elsewhere) and
then narrows it back down — every cycle — to a manageable, high-quality
watchlist via :func:`scan`. Fetching ~200 names every cycle is too slow and too
noisy; instead we keep a curated liquid universe and rank it once a day.

Design notes:
    * The curated lists are intentionally large-cap / very-liquid only, so that
      day-trading agents never have to worry about thin books or huge spreads.
    * Scoring blends liquidity (dollar volume), opportunity (ATR%, momentum)
      and crowd interest (relative volume) into a single rank.
    * Everything is resilient: a Yahoo hiccup on one symbol must not sink the
      whole scan, so each name is wrapped in try/except and simply skipped.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr
from daytrader.data import loader

# ---------------------------------------------------------------------------
# Curated universe
# ---------------------------------------------------------------------------

# ~80 highly liquid US large-cap STOCKS spread across sectors. These are the
# kind of names that always have tight spreads and deep books intraday.
LIQUID_STOCKS: list[str] = [
    # Mega-cap tech / communication
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO",
    "NFLX", "ADBE", "CRM", "ORCL", "AMD", "INTC", "QCOM", "CSCO", "TXN",
    "MU", "AMAT", "ADI", "LRCX", "INTU", "NOW", "PANW", "PLTR", "SNOW",
    "UBER", "SHOP", "ABNB", "XYZ",
    # Financials
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "V", "MA", "AXP",
    "BLK", "BRK-B", "PYPL", "COIN",
    # Health care
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "BMY",
    "AMGN", "GILD", "CVS", "MRNA",
    # Energy / materials
    "XOM", "CVX", "COP", "SLB", "OXY", "FCX", "MPC",
    # Consumer
    "WMT", "COST", "HD", "LOW", "TGT", "NKE", "MCD", "SBUX", "KO", "PEP",
    "PG", "DIS", "MDLZ", "CMCSA",
    # Industrials / transports / autos
    "BA", "CAT", "DE", "GE", "HON", "UPS", "LMT", "RTX", "F", "GM",
    "DAL", "NCLH",
    # Real estate / utilities / telecom
    "T", "VZ", "NEE",
]

# Major liquid ETFs: broad index, sector SPDRs, common leveraged/inverse,
# bonds/commodities, and volatility products.
LIQUID_ETFS: list[str] = [
    # Broad market / index
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EEM", "EFA",
    # Sector SPDRs and theme funds
    "XLF", "XLK", "XLE", "XLV", "XLY", "XLI", "XLP", "XLU", "XLB", "XLRE",
    "XLC", "SMH", "SOXX", "XBI", "KRE", "XOP", "XRT", "ITB", "IYR",
    "ARKK",
    # Leveraged / inverse (common, very liquid)
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "TNA", "LABU",
    # Bonds / rates / credit
    "TLT", "IEF", "SHY", "HYG", "LQD", "AGG", "BND",
    # Commodities / metals
    "GLD", "SLV", "GDX", "USO", "UNG", "SLX",
    # Volatility
    "VXX", "UVXY", "SVXY",
]


def _dedup_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# Full tradeable universe: stocks + ETFs, de-duplicated, with the benchmark
# (SPY) pinned first so callers can rely on a stable lead element.
ALL_SYMBOLS: list[str] = _dedup_keep_order(
    [loader.BENCHMARK] + LIQUID_STOCKS + LIQUID_ETFS
)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _score_symbol(symbol: str, interval: str) -> dict | None:
    """Compute liquidity/volatility/momentum metrics for one symbol.

    Returns a metrics dict, or ``None`` if the symbol can't be scored (no data,
    too few bars, etc.). Network/parse failures propagate to the caller, which
    wraps this in try/except.
    """
    df = loader.load(symbol, interval=interval)
    if df is None or len(df) < 20:
        return None

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    price = float(close.iloc[-1])
    if not np.isfinite(price) or price <= 0:
        return None

    # --- Liquidity: median per-bar dollar volume across the recent session(s).
    # We measure typical bar liquidity then scale to a full RTH session so the
    # number reads like a daily dollar-volume figure.
    day = df.index.normalize()
    bars_per_day = df.groupby(day).size()
    typical_bars = float(bars_per_day.median()) if len(bars_per_day) else float(len(df))
    typical_bars = max(typical_bars, 1.0)
    bar_dollar_vol = (close * volume)
    median_bar_dvol = float(np.nanmedian(bar_dollar_vol.tail(int(typical_bars) * 5)))
    dollar_volume = median_bar_dvol * typical_bars
    if not np.isfinite(dollar_volume) or dollar_volume <= 0:
        return None

    # --- Volatility / opportunity: ATR14 as a fraction of price.
    atr_series = atr(df, period=14)
    atr_val = float(atr_series.iloc[-1])
    atr_pct = atr_val / price if price else 0.0
    if not np.isfinite(atr_pct):
        atr_pct = 0.0

    # --- Relative volume: today's cumulative volume vs the typical full-day
    # volume of prior sessions, normalized by how far into the session we are.
    rel_volume = float("nan")
    try:
        vol_by_day = volume.groupby(day).sum()
        if len(vol_by_day) >= 3:
            today_key = day[-1]
            today_vol = float(vol_by_day.loc[today_key])
            prior = vol_by_day.iloc[:-1]
            typical_full = float(prior.tail(20).median())
            # Fraction of the session elapsed (bars today / typical bars/day).
            bars_today = float((day == today_key).sum())
            elapsed = min(max(bars_today / typical_bars, 0.05), 1.0)
            expected_so_far = typical_full * elapsed
            if expected_so_far > 0:
                rel_volume = today_vol / expected_so_far
    except Exception:  # noqa: BLE001 - rel_volume is best-effort
        rel_volume = float("nan")

    # --- Intraday momentum: signed return since today's open.
    day_change_pct = 0.0
    try:
        today_key = day[-1]
        today_df = df[day == today_key]
        if len(today_df):
            open_px = float(today_df["open"].iloc[0])
            if open_px > 0:
                day_change_pct = (price - open_px) / open_px
    except Exception:  # noqa: BLE001
        day_change_pct = 0.0
    if not np.isfinite(day_change_pct):
        day_change_pct = 0.0

    return {
        "symbol": symbol,
        "price": round(price, 4),
        "dollar_volume": dollar_volume,
        "atr_pct": round(atr_pct, 5),
        "rel_volume": (round(rel_volume, 3) if np.isfinite(rel_volume) else None),
        "day_change_pct": round(day_change_pct, 5),
    }


def _compute_scores(rows: list[dict]) -> None:
    """Assign a composite ``score`` to each metrics row, in place.

    Each component is rank-normalized to [0, 1] across the scanned set so the
    blend is robust to wildly different scales (a $50B-dollar-volume ETF vs a
    2%-ATR small-cap). Liquidity is weighted highest, then range, then the two
    "interest" signals.
    """
    if not rows:
        return

    def _pct_rank(values: list[float]) -> list[float]:
        s = pd.Series(values, dtype="float64")
        if s.notna().sum() <= 1:
            return [0.5] * len(values)
        # rank with NaNs treated as the middle of the pack
        r = s.rank(method="average", na_option="keep")
        filled = r.fillna(r.mean())
        lo, hi = filled.min(), filled.max()
        if hi <= lo:
            return [0.5] * len(values)
        return ((filled - lo) / (hi - lo)).tolist()

    liq = _pct_rank([np.log1p(r["dollar_volume"]) for r in rows])
    rng = _pct_rank([r["atr_pct"] for r in rows])
    relv = _pct_rank([
        (r["rel_volume"] if r["rel_volume"] is not None else np.nan) for r in rows
    ])
    mom = _pct_rank([abs(r["day_change_pct"]) for r in rows])

    for i, r in enumerate(rows):
        r["score"] = round(
            0.45 * liq[i] + 0.30 * rng[i] + 0.15 * relv[i] + 0.10 * mom[i], 6
        )


def scan(
    symbols: list[str] | None = None,
    interval: str = "5m",
    top_n: int = 18,
) -> list[dict]:
    """Rank candidates for the day's watchlist.

    For each symbol we load recent bars and compute a liquidity + volatility +
    momentum score:

        * ``dollar_volume`` — median bar dollar volume scaled to a session
          (liquidity).
        * ``atr_pct`` — ATR14 / price (intraday range / opportunity).
        * ``rel_volume`` — today's volume vs typical, session-elapsed adjusted
          (crowd interest; ``None`` if not derivable).
        * abs intraday momentum — ``|return since open|``.

    Symbols that fail to load are skipped (the scan never crashes on a single
    Yahoo hiccup). Returns the top ``top_n`` rows sorted by ``score`` desc:
    ``[{symbol, price, dollar_volume, atr_pct, rel_volume, day_change_pct,
    score}]``.

    Pass a smaller ``symbols`` subset to keep things fast (e.g. tests).
    """
    syms = list(symbols) if symbols is not None else ALL_SYMBOLS
    syms = _dedup_keep_order(syms)

    rows: list[dict] = []
    for sym in syms:
        try:
            metrics = _score_symbol(sym, interval)
            if metrics is not None:
                rows.append(metrics)
        except Exception as e:  # noqa: BLE001 - resilient: keep scanning
            print(f"[universe] WARNING: could not scan {sym}: {e}")
            continue

    _compute_scores(rows)
    rows.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return rows[:top_n]


def watchlist(top_n: int = 18, interval: str = "5m") -> list[str]:
    """Return just the ranked symbol list from :func:`scan`.

    Always includes the benchmark (SPY) even if it doesn't rank, and pins it
    first so downstream agents have a stable reference symbol.
    """
    ranked = scan(interval=interval, top_n=top_n)
    symbols = [r["symbol"] for r in ranked]

    bench = loader.BENCHMARK
    symbols = [s for s in symbols if s != bench]
    return [bench] + symbols


if __name__ == "__main__":  # smoke test
    print(
        f"universe: {len(ALL_SYMBOLS)} symbols "
        f"({len(LIQUID_STOCKS)} stocks + {len(LIQUID_ETFS)} etfs)"
    )
    res = scan(["SPY", "QQQ", "NVDA", "AAPL", "TSLA", "XLF"], top_n=4)
    for r in res:
        print(r["symbol"], "score", round(r["score"], 3), "atr%", r["atr_pct"])
    print("watchlist:", watchlist(top_n=4))
