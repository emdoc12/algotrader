# SAFETY: data/read endpoints only — no order placement, ever.
"""READ-ONLY tastytrade margin profile.

Mirrors the owner's real broker terms onto the paper accounts: contract
multipliers and tick sizes straight from tastytrade's futures instruments, and
the account's actual leverage terms (Reg-T vs portfolio margin, equity buying
power, day-trading buying power, futures intraday vs overnight margin) derived
from its balances.

Leverage is applied as a **ratio**, never as a dollar figure. The owner's real
account and a $25k paper account differ in size, so copying absolute buying
power would be meaningless — copying "your broker gives you 4x intraday" is the
part that transfers.

STRICTLY READ-ONLY, same contract as :mod:`tastytrade_data`. It touches only:

    * ``Account.get(session)``                 -- list accounts
    * ``account.get_balances(session)``        -- buying power / margin fields
    * ``account.get_margin_requirements(...)`` -- Reg-T vs portfolio margin
    * ``Future.get(session, symbols)``         -- contract reference data

There is NO import or use of ``Order``, ``place_order``, ``cancel``, or
``get_order_buying_power_effect`` (which requires constructing an Order and is
deliberately avoided). Every function is defensive: any failure degrades to the
static exchange-minimum table in :mod:`daytrader.core.contracts`.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("daytrader.tastytrade.margin")

# Refresh at most this often — margin terms change slowly (daily at most).
_TTL_SEC = float(os.environ.get("TT_MARGIN_TTL_SEC", "3600"))
_cache: dict = {}
_cache_ts: float = 0.0
_lock = threading.Lock()


def _enabled() -> bool:
    """Opt-in. Off by default so the competition's terms never change silently."""
    return os.environ.get("USE_TASTYTRADE_MARGIN", "").strip().lower() in ("1", "true", "yes")


def _f(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fetch() -> dict:
    """Pull the live margin profile. Returns {} on any failure."""
    from daytrader.live.tastytrade_data import _get_session
    session = _get_session()
    if session is None:
        return {}
    out: dict = {}
    try:
        from tastytrade.account import Account
        accounts = Account.get(session)
        if not accounts:
            return {}
        acct = accounts[0]
        bal = acct.get_balances(session)

        equity = _f(getattr(bal, "margin_equity", None)) or _f(getattr(bal, "net_liquidating_value", None))
        ebp = _f(getattr(bal, "equity_buying_power", None))
        dtbp = _f(getattr(bal, "day_trading_buying_power", None))
        fut_intra = _f(getattr(bal, "futures_intraday_margin_requirement", None))
        fut_over = _f(getattr(bal, "futures_overnight_margin_requirement", None))

        if equity and equity > 0:
            # Ratios transfer across account sizes; dollar figures do not.
            if ebp and ebp > 0:
                out["equity_bp_multiple"] = round(ebp / equity, 3)
            if dtbp and dtbp > 0:
                out["day_trade_bp_multiple"] = round(dtbp / equity, 3)
        # Futures intraday relief, when the account is actually carrying futures.
        if fut_intra and fut_over and fut_over > 0:
            out["futures_intraday_ratio"] = round(fut_intra / fut_over, 3)

        try:
            rep = acct.get_margin_requirements(session)
            mct = getattr(rep, "margin_calculation_type", None)
            if mct:
                out["margin_calculation_type"] = str(mct)
        except Exception as e:  # noqa: BLE001
            log.info("tastytrade margin: requirements unavailable (%s)", e)

        out["account_equity"] = equity
        out["source"] = "tastytrade"
    except Exception as e:  # noqa: BLE001
        log.info("tastytrade margin: balances unavailable (%s)", e)
        return {}

    # Authoritative contract reference data (multiplier / tick size).
    try:
        from daytrader.core.contracts import supported_symbols
        from tastytrade.instruments import Future
        roots = [s.replace("=F", "") for s in supported_symbols()]
        futs = Future.get(session, product_codes=roots) or []
        specs: dict = {}
        for f in futs:
            code = str(getattr(f, "product_code", "") or "").upper()
            mult = _f(getattr(f, "notional_multiplier", None))
            tick = _f(getattr(f, "tick_size", None))
            if not code or not mult:
                continue
            # Front month wins; the list is per expiry.
            if code not in specs:
                specs[code] = {"multiplier": mult, "tick_size": tick}
        if specs:
            out["contracts"] = specs
    except Exception as e:  # noqa: BLE001
        log.info("tastytrade margin: futures reference data unavailable (%s)", e)
    return out


def profile(force: bool = False) -> dict:
    """Cached margin profile, or {} when disabled/unavailable."""
    global _cache, _cache_ts
    if not _enabled():
        return {}
    with _lock:
        if not force and _cache and (time.time() - _cache_ts) < _TTL_SEC:
            return dict(_cache)
        data = _fetch()
        if data:
            _cache, _cache_ts = data, time.time()
        return dict(_cache)


def equity_buying_power_multiple(default: float = 1.0) -> float:
    """How many times equity may be deployed in EQUITIES.

    Default 1.0 is the cash model the paper broker has always used. With the
    owner's Reg-T account mirrored this becomes ~2.0 (or ~4.0 intraday).
    """
    p = profile()
    v = p.get("day_trade_bp_multiple") or p.get("equity_bp_multiple")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    # Sanity-clamp: never hand the desks more leverage than Reg-T day-trading.
    return max(1.0, min(v, 4.0)) if v > 0 else default


def futures_margin_scale(default: float = 1.0) -> float:
    """Multiplier on exchange initial margin (``<1`` = intraday relief)."""
    p = profile()
    v = p.get("futures_intraday_ratio")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.25, min(v, 1.0)) if v > 0 else default


def describe() -> dict:
    """Human/agent-readable summary of the terms actually in force."""
    p = profile()
    if not p:
        return {
            "source": "static",
            "equity_bp_multiple": 1.0,
            "futures_margin": "exchange minimums (daytrader.core.contracts)",
            "note": ("Paper terms: equities are cash-funded and futures use exchange "
                     "minimum margin. Set USE_TASTYTRADE_MARGIN=1 to mirror the owner's "
                     "real tastytrade terms instead."),
        }
    return {
        "source": "tastytrade",
        "margin_calculation_type": p.get("margin_calculation_type"),
        "equity_bp_multiple": equity_buying_power_multiple(),
        "futures_margin_scale": futures_margin_scale(),
        "contracts_from_broker": sorted(p.get("contracts", {})),
        "note": ("Mirroring the owner's live tastytrade margin terms as RATIOS "
                 "(not dollar amounts), applied to this paper account's own equity."),
    }
