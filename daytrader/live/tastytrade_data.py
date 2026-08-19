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
import time

log = logging.getLogger("daytrader.tastytrade")

# How many symbols get a full option-chain enrichment per snapshot (latency bound).
_MAX_OPTION_SYMBOLS = 3
# The full-chain HTTP fetch is the slow phase for a big underlying (SPY has
# thousands of listed contracts), so it gets its own budget instead of sharing
# the streaming one.
_CHAIN_FETCH_TIMEOUT = float(os.environ.get("OPTION_CHAIN_FETCH_TIMEOUT", "40"))
# The listed chain (strikes x expirations) changes about once a day, yet every
# call was re-downloading SPY's ~10k contracts from scratch. Cache it.
_CHAIN_CACHE_TTL = float(os.environ.get("OPTION_CHAIN_CACHE_TTL", "21600"))
_CHAIN_CACHE: dict[str, tuple[float, object]] = {}
# symbol -> download start ts. asyncio.wait_for cannot cancel a thread, so a
# download that blows its budget KEEPS RUNNING — which we turn from a bug into
# the mechanism: it completes into the cache and the next call is instant. The
# in-flight registry stops a retry from launching a SECOND download of the same
# chain alongside it, which is how five desks retrying every cycle turned one
# slow fetch into a pile-up that never finished at all.
_CHAIN_INFLIGHT: dict[str, float] = {}
_chain_lock = threading.Lock()
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
# The exact exception from the last failed session build. _get_session returns
# a bare None on failure, which left "why" trapped in a log.info nobody reads —
# the owner rotated credentials and had NO way to test them until the next
# trading cycle. The health check surfaces this verbatim instead.
_last_session_error: str | None = None


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
                if _sync(_session.validate()):
                    return _session
            except Exception:  # noqa: BLE001
                pass
            _session = None
        global _last_session_error
        try:
            from tastytrade import Session  # read/auth only (OAuth)
            client_secret = os.environ.get("TASTYTRADE_CLIENT_SECRET") or ""
            refresh_token = os.environ.get("TASTYTRADE_REFRESH_TOKEN") or ""
            # OAuth session: provider_secret + refresh_token (refresh handled
            # automatically by the SDK). No password / no 2FA prompt.
            candidate = Session(client_secret, refresh_token)
            # tastytrade 13.x's Session.__init__ does NOT talk to the network —
            # it just stores the two strings and sets a placeholder token, so
            # construction "succeeds" for ANY non-empty credential, garbage
            # included. The real OAuth exchange (POST /oauth/token) only fires
            # inside refresh(), lazily, on the first actual API call. That made
            # this function's "validated" docstring false: a bad client_secret
            # or a refresh_token from a different grant sailed through here as
            # a live session and only blew up minutes later, deep in a chain
            # download, as an unexplained invalid_grant. Dev request #28 caught
            # this directly — the Test Connections row (which forces exactly
            # this call) went green seconds after a credential rotation, then
            # the next real chain fetch still hit "Invalid JWT". Force the
            # exchange now, so a rejected credential fails HERE with the real
            # reason instead of surfacing as a mystery three calls downstream.
            _sync(candidate.refresh(force=True))
            _session = candidate
            _last_session_error = None
            return _session
        except Exception as e:  # noqa: BLE001
            log.info("tastytrade: session unavailable (%s); using Yahoo only", e)
            _last_session_error = repr(e)[:300]
            _session = None
            return None


def invalidate_session() -> None:
    """Drop the cached Session so the very next call rebuilds from current env.

    A rotated TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN does not, by
    itself, make ``_get_session()`` rebuild: the cached ``_session`` from
    BEFORE the rotation keeps being handed back to every caller for as long as
    its ``validate()`` keeps returning true, which checks the cached access
    token's local state, not whether tastytrade would still honor the
    credential pair it came from. Dev request #33: the dashboard's Test
    Connections row forced its OWN fresh rebuild (see healthcheck.py) and went
    green, but a desk's very next ``get_option_chain`` call — same process,
    same os.environ, same freshly-saved credentials — kept hitting
    invalid_grant, because it was still reusing the pre-rotation session
    object rather than rebuilding. Call this the moment credentials might have
    changed (a Settings save, a Test Connections click) so EVERY caller after
    that point rebuilds against the current environment instead of racing to
    be the one whose stale cache survives.
    """
    global _session
    with _session_lock:
        _session = None


def _sync(value):
    """Bridge SDK calls that went async in tastytrade v13 back into sync code.

    The chain download got this treatment in v6.38.1 (the auto-fix pipeline's
    own commit, from a desk's coroutine-bug report); this helper covers the
    REMAINING v13 async surfaces — ``Session.validate`` and the margin module's
    ``Account.get`` / ``get_balances`` / ``get_margin_requirements`` /
    ``Future.get`` — which were failing the same silent way: a coroutine is
    truthy, so the un-awaited call "succeeded" and downstream access crashed
    into a swallowing except. Plain values pass through, so v12-style sync
    SDKs keep working unchanged.
    """
    import inspect
    if inspect.iscoroutine(value):
        return asyncio.run(value)
    return value


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
    except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError) as e:
        log.info("tastytrade: async op timed out (%s)", e)
        return None
    except Exception:
        # Every other exception is REAL INFORMATION and must reach the caller.
        # This except used to swallow all of them into None, which the caller
        # then labelled "stream_timeout" — six desks spent seven days reporting
        # a timeout that may never have been one, because the true exception
        # (an SDK raise inside the chain download or streamer) died right here.
        raise
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
def _cached_chain(symbol: str):
    with _chain_lock:
        hit = _CHAIN_CACHE.get(symbol)
    if hit and (time.time() - hit[0]) < _CHAIN_CACHE_TTL:
        return hit[1]
    return None


def _download_chain(session, symbol: str):
    """Blocking REST download, run in a worker thread.

    13.x's ``get_option_chain`` is a coroutine function (the 12.x version this
    was written against was plain sync HTTP) — calling it without awaiting
    just builds a coroutine object and never sends the request. That object is
    truthy, so it sailed past ``if chain:`` and got cached, then blew up two
    frames later as ``AttributeError: 'coroutine' object has no attribute
    'keys'`` on the first ``chain.keys()``. This runs it to completion with its
    own event loop, since this function executes off-loop in a worker thread.

    Writes the cache even when the awaiting coroutine has already given up and
    returned an error to the desk: the expensive work still pays off, because
    the desk's next attempt (they retry every cycle) hits the cache. Clears the
    in-flight flag on every exit so a failure never wedges the symbol.
    """
    from tastytrade.instruments import get_option_chain
    try:
        chain = asyncio.run(get_option_chain(session, symbol))
        if chain:
            with _chain_lock:
                _CHAIN_CACHE[symbol] = (time.time(), chain)
        return chain
    finally:
        with _chain_lock:
            _CHAIN_INFLIGHT.pop(symbol, None)


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
    # This blocking HTTP download of the symbol's ENTIRE chain is the phase that
    # was silently eating the whole budget in production — ~10k contracts for
    # SPY on a container already running seven desks. Serve it from cache when
    # fresh; otherwise download in a thread with its own budget, and if that
    # budget blows, keep the thread alive to finish INTO the cache so the next
    # call succeeds instantly instead of starting over.
    chain = _cached_chain(symbol)
    if chain is None:
        now = time.time()
        with _chain_lock:
            started = _CHAIN_INFLIGHT.get(symbol)
            # A download stuck far past any sane budget is presumed dead
            # (hung socket); allow a fresh attempt rather than pointing at it
            # forever.
            stale = started is not None and (now - started) > 10 * _CHAIN_FETCH_TIMEOUT
            if started is None or stale:
                _CHAIN_INFLIGHT[symbol] = now
                owner = True
            else:
                owner = False
        if not owner:
            raise _ChainError(
                "chain_downloading",
                f"the {symbol} chain download started {now - started:.0f}s ago by an "
                "earlier call and is still running; it is cached the moment it "
                "completes — retry next cycle instead of stacking another download")
        try:
            chain = await asyncio.wait_for(
                asyncio.to_thread(_download_chain, session, symbol),
                timeout=_CHAIN_FETCH_TIMEOUT)
        except _ChainError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise _ChainError(
                "rest_chain_timeout",
                f"downloading the full {symbol} chain exceeded its "
                f"{_CHAIN_FETCH_TIMEOUT:.0f}s budget. The download CONTINUES in the "
                "background and lands in the cache when done — retry in a minute "
                "and it should return instantly") from None
        except Exception as e:  # noqa: BLE001
            raise _ChainError(
                "chain_download_failed",
                f"the {symbol} chain download RAISED rather than timing out: "
                f"{repr(e)[:300]}. This is the true error the old code masked as "
                "stream_timeout.") from e
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
                    # _get_session() now exchanges the refresh token for real
                    # (see its docstring) instead of only constructing the SDK
                    # object, so an invalid_grant/mismatched-grant credential
                    # is caught HERE rather than three phases deeper inside
                    # _download_chain. Carry the real rejection text along —
                    # without it this collapses back to the same generic
                    # "check your credentials" a desk cannot act on.
                    reason = _last_session_error or (
                        "check TASTYTRADE_CLIENT_SECRET / TASTYTRADE_REFRESH_TOKEN")
                    last = _chain_err("no_session",
                                      f"tastytrade session could not be established: {reason}",
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
                if e.code in ("empty_chain", "no_contracts",
                              "rest_chain_timeout", "chain_downloading"):
                    break        # retrying inside this call changes nothing
            except Exception as e:  # noqa: BLE001
                last = _chain_err("fetch_failed", repr(e)[:300], symbol)
    else:
        last = _chain_err("not_configured",
                          "tastytrade is not configured, so live chains are unavailable",
                          symbol)

    if allow_historical_fallback:
        # Two independent fallbacks, tried in order of data quality. Polygon
        # first: a live snapshot (current bid/ask/greeks/OI, not a dated
        # chain) that depends on neither tastytrade's OAuth session nor
        # Alpha Vantage's paywalled endpoint (dev request #28 — the desk had
        # already traced both dead paths and asked for exactly this). Alpha
        # Vantage's genuinely-historical chain stays as the last resort for
        # when Polygon isn't configured either.
        poly, poly_why = _polygon_chain_fallback(
            symbol, min_dte, max_dte, strike_pct_window,
            max_expirations, strikes_around_atr)
        if poly:
            poly["degraded_reason"] = (last or {}).get("error")
            poly["live_error_code"] = (last or {}).get("error_code")
            return poly

        hist, why_not = _historical_chain_fallback(
            symbol, min_dte, max_dte, strike_pct_window,
            max_expirations, strikes_around_atr)
        if hist:
            hist["degraded_reason"] = (last or {}).get("error")
            hist["live_error_code"] = (last or {}).get("error_code")
            hist["polygon_fallback_reason"] = poly_why
            return hist
        # The desks spent a day concluding the fallback "is not wired in"
        # because a live failure returned with no word about WHY the fallback
        # did not step in. Say it, in the error and on the dashboard.
        combined_reason = f"polygon: {poly_why} | alphavantage: {why_not}"
        if last is not None:
            last["fallback_reason"] = combined_reason
            last["error"] = f"{last['error']} | fallbacks unavailable: {combined_reason}"
        try:
            from daytrader.data.feeds.base import record_named_error
            record_named_error("chain_fallbacks", "unavailable", combined_reason,
                               hint=("With the live chain down, these fallbacks are the only "
                                     "options data path — fix whichever this names."))
        except Exception:  # noqa: BLE001
            pass
    return last or _chain_err("unknown", "chain unavailable", symbol)


def _chain_err(code: str, message: str, symbol: str) -> dict:
    return {"ok": False, "chain": {}, "symbol": symbol, "source": None,
            "stale": False, "as_of": None, "n_contracts": 0,
            "error_code": code, "error": message}


def _polygon_chain_fallback(symbol, min_dte, max_dte, strike_pct_window,
                            max_expirations, max_strikes) -> tuple[dict | None, str]:
    """Live chain from Polygon's options snapshot — the SECOND-tier fallback.

    Tried before Alpha Vantage's historical chain because this is a current
    snapshot (bid/ask/greeks/OI as of right now), not a dated one, and it
    depends on neither tastytrade's OAuth session nor Alpha Vantage's paywall
    — the exact independence dev request #28 asked for. Still labelled
    ``stale`` in the envelope: this module cannot confirm the owner's Polygon
    plan carries real-time (vs 15-minute delayed) options quotes, and
    overclaiming freshness the code cannot verify is worse than a
    conservative warning a desk can choose to act on anyway.
    """
    try:
        from daytrader.data.feeds import polygon as poly
        if not poly.is_configured():
            return None, "POLYGON_API_KEY is not configured"
        res = poly.chain(symbol, min_dte, max_dte, strike_pct_window,
                         max_expirations, max_strikes)
        if res.get("error"):
            return None, f"polygon error: {str(res['error'])[:200]}"
        if not res.get("chain"):
            return None, f"polygon returned no usable contracts for {symbol}"
        from datetime import datetime as _dt, timezone as _tz
        as_of = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"ok": True, "chain": res["chain"], "source": "polygon",
                "stale": True, "as_of": as_of, "n_contracts": res.get("n_contracts"),
                "symbol": symbol}, ""
    except Exception as e:  # noqa: BLE001
        log.info("options: polygon fallback for %s failed (%s)", symbol, e)
        return None, f"polygon fallback crashed: {repr(e)[:200]}"


def _historical_chain_fallback(symbol, min_dte, max_dte, strike_pct_window,
                               max_expirations, max_strikes) -> tuple[dict | None, str]:
    """Build a chain from the most recent HISTORICAL data, shaped like a live one.

    Explicitly stale, and labelled so. Returns ``(chain_env, reason)`` — the
    reason says why no chain came back, because "the fallback silently did
    nothing" cost the desks a day of guessing.
    """
    try:
        from daytrader.data.feeds import alphavantage as av
        if not av.is_configured():
            return None, ("ALPHAVANTAGE_API_KEY is not visible in this process — set it in "
                          "Settings (and note settings apply on save; a container update "
                          "re-applies them at startup)")
        cached = av.cached_dates(symbol)
        res = av.historical_chain(symbol, cached[-1] if cached else None)
        if res.get("error"):
            return None, f"alphavantage error: {str(res['error'])[:200]}"
        rows = (res or {}).get("contracts") or []
        if not rows:
            return None, f"alphavantage returned no contracts for {symbol}"
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
            return None, (f"no {symbol} contracts matched the requested window "
                          f"(dte {min_dte}-{max_dte}, strikes within "
                          f"{strike_pct_window}% of spot) in the historical chain")

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
            return None, f"historical rows for {symbol} produced no usable strikes"
        n = sum(len(b["strikes"]) for b in chain.values())
        return {"ok": True, "chain": chain, "source": "alphavantage_historical",
                "stale": True, "as_of": res.get("date"), "n_contracts": n,
                "symbol": symbol}, ""
    except Exception as e:  # noqa: BLE001
        log.info("options: historical fallback for %s failed (%s)", symbol, e)
        return None, f"fallback crashed: {repr(e)[:200]}"


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
