# SAFETY: data/read endpoints only — no order placement, ever.
"""READ-ONLY tastytrade market-data integration.

This module enriches the shared market snapshot with live data pulled from the
owner's tastytrade account: real-time stock quotes (bid/ask/last/mid) and, for a
few near-the-money option strikes, streaming Greeks (delta/gamma/theta/vega/rho/
iv) plus bid/ask.

IT IS DELIBERATELY READ-ONLY. The autonomous trading teams use this data to
reason, but the system NEVER trades on the tastytrade account: all execution
stays in the internal paper simulator. Accordingly this module touches ONLY the
tastytrade SDK's data/read surface:

    * tastytrade.Session(login, password)  -- auth + token refresh (read context)
    * tastytrade.instruments.NestedOptionChain.get_chain  -- read option chains
    * tastytrade.DXLinkStreamer with Quote and Greeks events  -- read quotes/Greeks

There is NO import or call of anything order/trade/account-mutation related
(no Order, no place_order, no cancel, no Account trading methods). Every public
function is synchronous-callable and fully defensive: any error (missing creds,
auth failure, network hiccup, SDK change) is swallowed and the system degrades
to the existing Yahoo-bars/indicators view. These functions must NEVER raise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

log = logging.getLogger("daytrader.tastytrade")

# How many symbols get a full option-chain enrichment per snapshot (latency bound).
_MAX_OPTION_SYMBOLS = 3

# Cached session, guarded by a lock so concurrent cycles share one login.
_session = None
_session_lock = threading.Lock()


def is_configured() -> bool:
    """True iff both tastytrade credentials are present in the environment."""
    try:
        return bool(os.environ.get("TASTYTRADE_USERNAME")) and bool(
            os.environ.get("TASTYTRADE_PASSWORD")
        )
    except Exception:  # noqa: BLE001 - never raise out of here
        return False


def _get_session():
    """Return a cached, validated read-only Session, creating it lazily.

    Returns None on any failure (missing creds, bad password, SDK issue).
    """
    global _session
    if not is_configured():
        return None
    with _session_lock:
        if _session is not None:
            # Best-effort token refresh; if it fails we rebuild below.
            try:
                if _session.validate():
                    return _session
            except Exception:  # noqa: BLE001
                pass
            _session = None
        try:
            from tastytrade import Session  # read/auth only
            user = os.environ.get("TASTYTRADE_USERNAME") or ""
            pwd = os.environ.get("TASTYTRADE_PASSWORD") or ""
            _session = Session(user, pwd)
            return _session
        except Exception as e:  # noqa: BLE001
            log.info("tastytrade: session unavailable (%s); using Yahoo only", e)
            _session = None
            return None


def _run(coro, timeout: float):
    """Run an async coroutine to completion from sync code, bounded by timeout.

    Uses a private event loop so it works even if called off the main thread.
    Returns None on timeout or any error.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
    except Exception as e:  # noqa: BLE001 - timeout, cancellation, network, etc.
        log.info("tastytrade: async op failed/timed out (%s)", e)
        return None
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass


def _q_num(v):
    """Coerce a possibly-Decimal/None quote value to float or None."""
    try:
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Live stock quotes
# --------------------------------------------------------------------------- #
async def _collect_quotes(session, symbols: list[str], timeout: float) -> dict:
    from tastytrade import DXLinkStreamer  # read streamer only
    from tastytrade.dxfeed import Quote

    out: dict[str, dict] = {}
    want = set(symbols)
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, list(want))
        # Collect one quote per symbol, or until we run out of time.
        while want:
            quote = await streamer.get_event(Quote)
            sym = getattr(quote, "event_symbol", None)
            if sym is None:
                continue
            bid = _q_num(getattr(quote, "bid_price", None))
            ask = _q_num(getattr(quote, "ask_price", None))
            mid = None
            try:
                mid = _q_num(quote.mid_price)
            except Exception:  # noqa: BLE001
                pass
            if mid is None and bid is not None and ask is not None:
                mid = round((bid + ask) / 2, 4)
            out[sym] = {"bid": bid, "ask": ask, "last": mid, "mid": mid}
            want.discard(sym)
    return out


def get_quotes(symbols: list[str], timeout: float = 8.0) -> dict:
    """Return {symbol: {bid, ask, last, mid}} from a short Quote subscription.

    Opens (cached) session, subscribes, collects one quote per symbol or until
    the timeout, then tears the streamer down. Fully defensive — returns {} on
    any problem and never raises.
    """
    try:
        if not symbols:
            return {}
        session = _get_session()
        if session is None:
            return {}
        result = _run(_collect_quotes(session, list(symbols), timeout), timeout + 1.0)
        return result or {}
    except Exception as e:  # noqa: BLE001
        log.info("tastytrade: get_quotes failed (%s)", e)
        return {}


# --------------------------------------------------------------------------- #
# Option chain with streaming Greeks (near-the-money only)
# --------------------------------------------------------------------------- #
async def _collect_option_chain(
    session, symbol: str, max_expirations: int, strikes_around_atr: int, timeout: float
) -> dict:
    from tastytrade import DXLinkStreamer  # read streamer only
    from tastytrade.dxfeed import Greeks, Quote
    from tastytrade.instruments import NestedOptionChain  # read chain only

    chains = NestedOptionChain.get_chain(session, symbol)
    if not chains:
        return {}
    chain = chains[0]
    expirations = sorted(
        chain.expirations, key=lambda e: getattr(e, "days_to_expiration", 9999)
    )[: max(1, max_expirations)]

    # Find an at-the-money anchor from a quick underlying quote so we can keep
    # the subscription small (near-the-money strikes only).
    spot = None
    uq = await _collect_quotes(session, [symbol], min(timeout, 4.0))
    if uq.get(symbol):
        spot = uq[symbol].get("mid") or uq[symbol].get("last")

    result: dict = {}
    streamer_symbols: list[str] = []
    # streamer_symbol -> (expiration_key, "call"/"put", strike_price)
    sym_index: dict[str, tuple] = {}

    for exp in expirations:
        strikes = list(getattr(exp, "strikes", []) or [])
        if not strikes:
            continue
        # Pick the N strikes closest to spot (fallback: middle of the ladder).
        if spot is not None:
            strikes.sort(key=lambda s: abs(_q_num(s.strike_price) or 0.0) - 0.0)
            strikes.sort(key=lambda s: abs((_q_num(s.strike_price) or 0.0) - spot))
        n = max(1, strikes_around_atr)
        chosen = strikes[:n]

        exp_key = str(getattr(exp, "expiration_date", ""))
        legs = []
        for s in chosen:
            strike_px = _q_num(s.strike_price)
            for side, streamer_sym in (
                ("call", getattr(s, "call_streamer_symbol", None)),
                ("put", getattr(s, "put_streamer_symbol", None)),
            ):
                if not streamer_sym:
                    continue
                streamer_symbols.append(streamer_sym)
                sym_index[streamer_sym] = (exp_key, side, strike_px)
                legs.append((streamer_sym, side, strike_px))
        result[exp_key] = {
            "expiration": exp_key,
            "days_to_expiration": getattr(exp, "days_to_expiration", None),
            "strikes": {},  # filled below keyed by strike price
        }

    if not streamer_symbols:
        return result

    # Stream Greeks + quotes for just those near-the-money contracts.
    greeks_by_sym: dict[str, dict] = {}
    quotes_by_sym: dict[str, dict] = {}

    async def _pump():
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, streamer_symbols)
            await streamer.subscribe(Quote, streamer_symbols)
            need_greeks = set(streamer_symbols)
            need_quotes = set(streamer_symbols)

            async def _greeks():
                while need_greeks:
                    g = await streamer.get_event(Greeks)
                    sym = getattr(g, "event_symbol", None)
                    if sym is None:
                        continue
                    greeks_by_sym[sym] = {
                        "delta": _q_num(getattr(g, "delta", None)),
                        "gamma": _q_num(getattr(g, "gamma", None)),
                        "theta": _q_num(getattr(g, "theta", None)),
                        "vega": _q_num(getattr(g, "vega", None)),
                        "rho": _q_num(getattr(g, "rho", None)),
                        "iv": _q_num(getattr(g, "volatility", None)),
                    }
                    need_greeks.discard(sym)

            async def _quotes():
                while need_quotes:
                    q = await streamer.get_event(Quote)
                    sym = getattr(q, "event_symbol", None)
                    if sym is None:
                        continue
                    bid = _q_num(getattr(q, "bid_price", None))
                    ask = _q_num(getattr(q, "ask_price", None))
                    quotes_by_sym[sym] = {"bid": bid, "ask": ask}
                    need_quotes.discard(sym)

            # Wait for both, but never beyond the budget.
            await asyncio.gather(_greeks(), _quotes())

    # Bound the pump; partial data is fine.
    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except Exception:  # noqa: BLE001 - timeout/partial is acceptable
        pass

    # Stitch streamed data back onto the chain structure.
    for streamer_sym, (exp_key, side, strike_px) in sym_index.items():
        leg = {}
        leg.update(quotes_by_sym.get(streamer_sym, {}))
        leg.update(greeks_by_sym.get(streamer_sym, {}))
        if not leg:
            continue
        exp_block = result.get(exp_key)
        if exp_block is None:
            continue
        key = str(strike_px)
        slot = exp_block["strikes"].setdefault(key, {"strike": strike_px})
        slot[side] = leg

    return result


def get_option_chain(
    symbol: str, max_expirations: int = 1, strikes_around_atr: int = 6
) -> dict:
    """Return near-the-money option data with streaming Greeks for ``symbol``.

    Shape: {expiration_date: {expiration, days_to_expiration,
            strikes: {strike: {strike, call:{...greeks/quote}, put:{...}}}}}.

    Kept intentionally small (a few strikes around the money, nearest
    expirations) to bound latency. Returns {} on any problem; never raises.
    """
    try:
        if not symbol:
            return {}
        session = _get_session()
        if session is None:
            return {}
        # Total budget for chain fetch + streaming.
        result = _run(
            _collect_option_chain(
                session, symbol, max_expirations, strikes_around_atr, 8.0
            ),
            12.0,
        )
        return result or {}
    except Exception as e:  # noqa: BLE001
        log.info("tastytrade: get_option_chain(%s) failed (%s)", symbol, e)
        return {}


# --------------------------------------------------------------------------- #
# Snapshot enrichment hook
# --------------------------------------------------------------------------- #
def enrich_snapshot(snapshot: dict, options_for: list[str] | None = None) -> dict:
    """Add live tastytrade quotes + option chains/Greeks onto a market snapshot.

    If tastytrade is not configured, the snapshot is returned UNCHANGED. On any
    failure we log a note and return the snapshot unmodified (degrade to the
    existing Yahoo-only view). This function never raises.
    """
    if not is_configured():
        return snapshot
    try:
        universe = list(snapshot.get("universe") or [])
        market = snapshot.get("market") or {}

        # Live bid/ask/last/mid for the whole universe.
        quote_symbols = universe or list(market.keys())
        if quote_symbols:
            quotes = get_quotes(quote_symbols)
            if quotes:
                for sym, q in quotes.items():
                    if sym in market and isinstance(market[sym], dict):
                        market[sym]["quote"] = q
                snapshot["market"] = market

        # Option chains + Greeks for a small set of symbols.
        opt_symbols = options_for if options_for is not None else universe
        opt_symbols = list(opt_symbols)[:_MAX_OPTION_SYMBOLS]
        options: dict[str, dict] = {}
        for sym in opt_symbols:
            chain = get_option_chain(sym)
            if chain:
                options[sym] = chain
        if options:
            snapshot["options"] = options

        snapshot["data_source"] = "tastytrade"
        return snapshot
    except Exception as e:  # noqa: BLE001 - degrade gracefully to Yahoo-only
        log.info("tastytrade: enrich_snapshot failed (%s); using Yahoo only", e)
        return snapshot
