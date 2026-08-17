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

    * tastytrade.Session(provider_secret, refresh_token)  -- OAuth auth + token
      refresh (read context). OAuth is used so a 2FA-protected account can run
      headless: the refresh token is generated once and never expires.
    * tastytrade.instruments.get_option_chain  -- read option chains
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
# The full-chain HTTP fetch is the slow phase for a big underlying (SPY has
# thousands of listed contracts), so it gets its own budget instead of sharing
# the streaming one.
_CHAIN_FETCH_TIMEOUT = float(os.environ.get("OPTION_CHAIN_FETCH_TIMEOUT", "25"))
# Give up on a quiet stream after this long with no new event.
_STREAM_QUIET_SEC = float(os.environ.get("OPTION_STREAM_QUIET_SEC", "4"))
# Hard cap on contracts subscribed for Greeks/quotes in one call.
_MAX_STREAM_SYMBOLS = int(os.environ.get("OPTION_MAX_STREAM_SYMBOLS", "80"))


class _ChainError(Exception):
    """A chain fetch that failed for a REASON worth reporting.

    Every failure path used to return an empty dict, which is why a desk saw
    "no chain returned — the symbol may not have listed options" for SPY, and
    why data_providers.degraded stayed empty: the reason was thrown away at the
    point it was discovered.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

# Cached session, guarded by a lock so concurrent cycles share one login.
_session = None
_session_lock = threading.Lock()


def is_configured() -> bool:
    """True iff the tastytrade OAuth credentials are present in the environment."""
    try:
        return bool(os.environ.get("TASTYTRADE_CLIENT_SECRET")) and bool(
            os.environ.get("TASTYTRADE_REFRESH_TOKEN")
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
            from tastytrade import Session  # read/auth only (OAuth)
            client_secret = os.environ.get("TASTYTRADE_CLIENT_SECRET") or ""
            refresh_token = os.environ.get("TASTYTRADE_REFRESH_TOKEN") or ""
            # OAuth session: provider_secret + refresh_token (refresh handled
            # automatically by the SDK). No password / no 2FA prompt.
            _session = Session(client_secret, refresh_token)
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
    except _ChainError:
        # A diagnosed failure must reach the caller. Folding it into None here
        # would relabel "this symbol has no listed options" as a timeout — the
        # exact loss of information that had a desk chasing a phantom SPY bug.
        raise
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
    session, symbol: str, max_expirations: int, strikes_around_atr: int, timeout: float,
    min_dte: int | None = None, max_dte: int | None = None,
    strike_pct_window: float | None = None,
) -> dict:
    from datetime import date as _date

    from tastytrade import DXLinkStreamer  # read streamer only
    from tastytrade.dxfeed import Greeks, Quote
    from tastytrade.instruments import OptionType, get_option_chain  # read chain only

    # 12.x: get_option_chain(session, symbol) -> {expiration_date: [Option, ...]}
    # This is a BLOCKING HTTP call that downloads and parses the symbol's entire
    # option chain — thousands of contracts for an index ETF. Run it in a thread
    # so it cannot stall the event loop the streamer also needs, and give it its
    # own budget rather than letting it eat the streaming allowance.
    chain = await asyncio.wait_for(
        asyncio.to_thread(get_option_chain, session, symbol),
        timeout=_CHAIN_FETCH_TIMEOUT)
    if not chain:
        raise _ChainError("empty_chain", f"{symbol} returned no option chain")
    expirations = sorted(chain.keys())
    # Strategy work is expressed in DTE ("30-45 DTE, 20-30 delta"), not in
    # "the next N expirations" — a desk asking for a 35-DTE spread must not be
    # handed weeklies. Filter to the requested window before slicing.
    if min_dte is not None or max_dte is not None:
        today = _date.today()
        lo = int(min_dte) if min_dte is not None else -1
        hi = int(max_dte) if max_dte is not None else 10_000
        windowed = [e for e in expirations
                    if isinstance(e, _date) and lo <= (e - today).days <= hi]
        # Nothing in the window (thin chain / holiday week): fall back to the
        # nearest expiration BEYOND it rather than silently returning weeklies.
        if not windowed:
            beyond = [e for e in expirations if isinstance(e, _date) and (e - today).days >= lo]
            windowed = beyond[:1] or expirations[:1]
        expirations = windowed
    expirations = expirations[: max(1, max_expirations)]

    # Find an at-the-money anchor from a quick underlying quote so we can keep
    # the subscription small (near-the-money strikes only).
    spot = None
    try:
        uq = await asyncio.wait_for(_collect_quotes(session, [symbol], 4.0), timeout=5.0)
        if uq.get(symbol):
            spot = uq[symbol].get("mid") or uq[symbol].get("last")
    except Exception:  # noqa: BLE001 - a missing spot only widens strike selection
        spot = None

    result: dict = {}
    streamer_symbols: list[str] = []
    # streamer_symbol -> (expiration_key, "call"/"put", strike_price)
    sym_index: dict[str, tuple] = {}

    for exp_date in expirations:
        options = chain.get(exp_date) or []
        if not options:
            continue
        # Unique strikes, then pick the N closest to spot (fallback: lowest N).
        strikes = sorted({_q_num(o.strike_price) for o in options if o.strike_price is not None})
        if spot is not None and strike_pct_window:
            # A 20-30 delta short strike sits well outside the money, so a
            # count-based window centred on spot misses exactly the strikes the
            # premium-selling strategies need. Select by DISTANCE first, then cap.
            span = float(strike_pct_window) / 100.0 * spot
            strikes = [k for k in strikes if k is not None and abs(k - spot) <= span] or strikes
        if spot is not None:
            strikes.sort(key=lambda s: abs((s or 0.0) - spot))
        chosen = set(strikes[: max(1, strikes_around_atr)])

        exp_key = str(exp_date)
        try:
            dte = (exp_date - _date.today()).days if isinstance(exp_date, _date) else None
        except Exception:  # noqa: BLE001
            dte = None
        result[exp_key] = {"expiration": exp_key, "days_to_expiration": dte, "strikes": {}}

        for o in options:
            strike_px = _q_num(o.strike_price)
            if strike_px not in chosen:
                continue
            streamer_sym = getattr(o, "streamer_symbol", None)
            if not streamer_sym:
                continue
            side = "call" if getattr(o, "option_type", None) == OptionType.CALL else "put"
            streamer_symbols.append(streamer_sym)
            sym_index[streamer_sym] = (exp_key, side, strike_px)

    if not streamer_symbols:
        raise _ChainError("no_contracts",
                          f"{symbol} chain had no contracts in the requested "
                          "expiration/strike window")
    # Every subscribed contract is one more event to wait for. Cap it so a wide
    # request cannot turn into a slow one.
    if len(streamer_symbols) > _MAX_STREAM_SYMBOLS:
        streamer_symbols = streamer_symbols[:_MAX_STREAM_SYMBOLS]
        keep = set(streamer_symbols)
        sym_index = {k: v for k, v in sym_index.items() if k in keep}

    # Stream Greeks + quotes for just those near-the-money contracts.
    greeks_by_sym: dict[str, dict] = {}
    quotes_by_sym: dict[str, dict] = {}

    async def _pump():
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, streamer_symbols)
            await streamer.subscribe(Quote, streamer_symbols)
            need_greeks = set(streamer_symbols)
            need_quotes = set(streamer_symbols)

            # Return as soon as the flow goes quiet rather than holding out for
            # every contract: an illiquid strike may never publish an event, and
            # waiting for it burned the whole budget while usable data for the
            # liquid strikes sat already collected.
            async def _greeks():
                while need_greeks:
                    g = await asyncio.wait_for(streamer.get_event(Greeks),
                                               timeout=_STREAM_QUIET_SEC)
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
                    q = await asyncio.wait_for(streamer.get_event(Quote),
                                               timeout=_STREAM_QUIET_SEC)
                    sym = getattr(q, "event_symbol", None)
                    if sym is None:
                        continue
                    bid = _q_num(getattr(q, "bid_price", None))
                    ask = _q_num(getattr(q, "ask_price", None))
                    quotes_by_sym[sym] = {"bid": bid, "ask": ask}
                    need_quotes.discard(sym)

            # Partial data is a success, so a quiet stream on one leg must not
            # discard what the other collected.
            await asyncio.gather(_greeks(), _quotes(), return_exceptions=True)

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


def fetch_option_chain(
    symbol: str, max_expirations: int = 2, strikes_around_atr: int = 16,
    min_dte: int | None = None, max_dte: int | None = None,
    strike_pct_window: float | None = None, timeout: float = 10.0,
    attempts: int = 2, allow_historical_fallback: bool = True,
) -> dict:
    """Fetch a chain and REPORT what happened.

    Returns an envelope::

        {"ok", "chain", "source", "stale", "as_of", "error_code", "error",
         "n_contracts", "partial"}

    The plain-dict version of this function swallowed every failure into ``{}``,
    so a desk could not tell "this symbol has no options" from "the market-data
    stream timed out" from "the session expired" — and the snapshot's degraded
    list had nothing to report. Each phase now fails with a code.

    Live quotes are tried first, twice (the streamer is genuinely flaky). If
    live is unavailable and ``allow_historical_fallback`` is set, the most recent
    HISTORICAL chain is returned instead, flagged ``stale`` with its ``as_of``
    date — usable for structure selection and strike/delta research, and clearly
    marked as not tradeable pricing.
    """
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return _chain_err("bad_symbol", "symbol required", symbol)

    last: dict | None = None
    if is_configured():
        for attempt in range(max(1, int(attempts))):
            try:
                session = _get_session()
                if session is None:
                    last = _chain_err("no_session",
                                      "tastytrade session could not be established — check "
                                      "TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN",
                                      symbol)
                    break
                # Budget must COVER its phases, not equal one of them: the old
                # 12s cap had to fit a full-chain download plus a 4s spot quote
                # plus an 8s stream, which is why a big chain could never finish.
                budget = _CHAIN_FETCH_TIMEOUT + 5.0 + timeout + 5.0
                result = _run(
                    _collect_option_chain(
                        session, symbol, max_expirations, strikes_around_atr,
                        timeout, min_dte, max_dte, strike_pct_window,
                    ),
                    budget,
                )
                if result:
                    n = sum(len(b.get("strikes") or {}) for b in result.values())
                    if n:
                        return {"ok": True, "chain": result, "source": "tastytrade",
                                "stale": False, "as_of": None, "n_contracts": n,
                                "symbol": symbol, "attempt": attempt + 1}
                    last = _chain_err("no_quotes",
                                      "the chain was retrieved but no strike received a "
                                      "quote or Greeks event before the deadline (quiet "
                                      "market-data stream)", symbol)
                else:
                    last = _chain_err("stream_timeout",
                                      f"chain fetch exceeded its {budget:.0f}s budget",
                                      symbol)
            except _ChainError as e:
                last = _chain_err(e.code, e.message, symbol)
                if e.code in ("empty_chain", "no_contracts"):
                    break        # deterministic: retrying changes nothing
            except Exception as e:  # noqa: BLE001
                last = _chain_err("fetch_failed", repr(e)[:300], symbol)
    else:
        last = _chain_err("not_configured",
                          "tastytrade is not configured, so live chains are unavailable",
                          symbol)

    if allow_historical_fallback:
        hist = _historical_chain_fallback(symbol, min_dte, max_dte, strike_pct_window,
                                         max_expirations, strikes_around_atr)
        if hist:
            hist["degraded_reason"] = (last or {}).get("error")
            hist["live_error_code"] = (last or {}).get("error_code")
            return hist
    return last or _chain_err("unknown", "chain unavailable", symbol)


def _chain_err(code: str, message: str, symbol: str) -> dict:
    return {"ok": False, "chain": {}, "symbol": symbol, "source": None,
            "stale": False, "as_of": None, "n_contracts": 0,
            "error_code": code, "error": message}


def _historical_chain_fallback(symbol, min_dte, max_dte, strike_pct_window,
                               max_expirations, max_strikes) -> dict | None:
    """Build a chain from the most recent HISTORICAL data, shaped like a live one.

    Explicitly stale, and labelled so. A desk choosing strikes by delta for a
    30-45 DTE credit spread can do that off yesterday's chain; what it must not
    do is believe those premiums are executable prices today.
    """
    try:
        from daytrader.data.feeds import alphavantage as av
        if not av.is_configured():
            return None
        cached = av.cached_dates(symbol)
        res = av.historical_chain(symbol, cached[-1] if cached else None)
        rows = (res or {}).get("contracts") or []
        if not rows:
            return None
        from datetime import date as _d
        today = _d.today()

        def _dte(exp):
            try:
                y, m, dd = (int(x) for x in str(exp)[:10].split("-"))
                return (_d(y, m, dd) - today).days
            except Exception:  # noqa: BLE001
                return None

        spot = None
        try:
            from daytrader.data import quotes as _q
            spot = _q.get_quote(symbol)
        except Exception:  # noqa: BLE001
            spot = None

        exps: dict[str, list] = {}
        for r in rows:
            d = _dte(r.get("expiration"))
            if d is None or d < 0:
                continue
            if min_dte is not None and d < int(min_dte):
                continue
            if max_dte is not None and d > int(max_dte):
                continue
            if spot and strike_pct_window and r.get("strike"):
                if abs(float(r["strike"]) - spot) > float(strike_pct_window) / 100.0 * spot:
                    continue
            exps.setdefault(str(r["expiration"]), []).append(r)
        if not exps:
            return None

        chain: dict = {}
        for exp in sorted(exps)[: max(1, int(max_expirations))]:
            rows_e = exps[exp]
            strikes = sorted({float(r["strike"]) for r in rows_e if r.get("strike")})
            if spot:
                strikes.sort(key=lambda k: abs(k - spot))
            keep = set(strikes[: max(1, int(max_strikes))])
            block = {"expiration": exp, "days_to_expiration": _dte(exp), "strikes": {}}
            for r in rows_e:
                k = float(r.get("strike") or 0)
                if k not in keep:
                    continue
                slot = block["strikes"].setdefault(str(k), {"strike": k})
                slot["call" if r.get("type") == "call" else "put"] = {
                    "bid": r.get("bid"), "ask": r.get("ask"), "mark": r.get("mark"),
                    "delta": r.get("delta"), "gamma": r.get("gamma"),
                    "theta": r.get("theta"), "vega": r.get("vega"),
                    "iv": r.get("implied_volatility"),
                    "open_interest": r.get("open_interest"), "volume": r.get("volume"),
                }
            if block["strikes"]:
                chain[exp] = block
        if not chain:
            return None
        n = sum(len(b["strikes"]) for b in chain.values())
        return {"ok": True, "chain": chain, "source": "alphavantage_historical",
                "stale": True, "as_of": res.get("date"), "n_contracts": n,
                "symbol": symbol}
    except Exception as e:  # noqa: BLE001
        log.info("options: historical fallback for %s failed (%s)", symbol, e)
        return None


def get_option_chain(
    symbol: str, max_expirations: int = 1, strikes_around_atr: int = 6,
    min_dte: int | None = None, max_dte: int | None = None,
    strike_pct_window: float | None = None, timeout: float = 8.0,
) -> dict:
    """Near-the-money option data with streaming Greeks. Returns {} on failure.

    Kept for callers that only want the chain; :func:`fetch_option_chain` is the
    one that explains a failure. Live-only — a caller wanting the plain shape is
    marking positions, and must never mark them off a stale chain.
    """
    env = fetch_option_chain(symbol, max_expirations, strikes_around_atr,
                             min_dte, max_dte, strike_pct_window, timeout,
                             attempts=1, allow_historical_fallback=False)
    return env.get("chain") or {}


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
