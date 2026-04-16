"""
Short Put Strategy
------------------
Scans the option chain for each symbol in the watchlist.
Looks for puts within the DTE window and delta range, with a minimum premium.
Places a SELL_TO_OPEN limit order at the mid-price.

Default parameters (all overridable in the UI):
  minDTE:       30
  maxDTE:       60
  targetDelta:  0.30   (looks for puts with delta ~0.16-0.35 by default)
  minDelta:     0.16   (lower bound)
  maxDelta:     0.35   (upper bound — overrides targetDelta range if set)
  minPOP:       65     (min probability of profit %, derived from delta)
  minPremium:   0.50   (min credit per contract, in dollars)
  maxContracts: 1      (from maxPositionSize)
"""
import logging
from decimal import Decimal
from datetime import date

from tastytrade.instruments import get_option_chain, OptionType
from tastytrade.dxfeed import Greeks, Quote
from tastytrade import DXLinkStreamer

import api_client
import order_executor
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class ShortPutStrategy(BaseStrategy):

    async def scan(self):
        symbols = await api_client.get_watchlist(self.strategy_id)
        if not symbols:
            await api_client.post_log(
                "warn",
                f"[{self.name}] Watchlist is empty — add symbols in the UI.",
                strategy_id=self.strategy_id,
            )
            return

        min_dte: int = self.params.get("minDTE", 30)
        max_dte: int = self.params.get("maxDTE", 60)
        min_delta: float = self.params.get("minDelta", 0.16)
        max_delta: float = self.params.get("maxDelta", 0.35)
        min_premium: float = self.params.get("minPremium", 0.50)
        max_contracts: int = int(self.max_position_size)

        await api_client.post_log(
            "info",
            f"[{self.name}] Scanning {len(symbols)} symbol(s): {', '.join(symbols)}",
            strategy_id=self.strategy_id,
        )

        async with DXLinkStreamer(self.session) as streamer:
            for symbol in symbols:
                await self._scan_symbol(
                    symbol, streamer,
                    min_dte, max_dte, min_delta, max_delta,
                    min_premium, max_contracts,
                )

    async def _scan_symbol(
        self,
        symbol: str,
        streamer: DXLinkStreamer,
        min_dte: int,
        max_dte: int,
        min_delta: float,
        max_delta: float,
        min_premium: float,
        max_contracts: int,
    ):
        try:
            chain = get_option_chain(self.session, symbol)
        except Exception as e:
            logger.warning("Failed to get option chain for %s: %s", symbol, e)
            return

        # Filter expirations within DTE window
        today = date.today()
        valid_exps = [
            exp for exp in chain
            if min_dte <= (exp - today).days <= max_dte
        ]
        if not valid_exps:
            logger.info("%s: No expirations in %d-%d DTE window.", symbol, min_dte, max_dte)
            return

        # Collect puts from valid expirations
        candidates = []
        for exp in valid_exps:
            puts = [o for o in chain[exp] if o.option_type == OptionType.PUT]
            candidates.extend(puts)

        if not candidates:
            return

        # Stream Greeks for all puts
        streamer_symbols = [o.streamer_symbol for o in candidates]
        await streamer.subscribe(Greeks, streamer_symbols)

        greeks_map: dict[str, Greeks] = {}
        async for g in streamer.listen(Greeks):
            greeks_map[g.event_symbol] = g
            if len(greeks_map) >= len(streamer_symbols):
                break

        # Also get the underlying quote for context
        await streamer.subscribe(Quote, [symbol])
        underlying_quote = await streamer.get_event(Quote)
        underlying_price = underlying_quote.bid_price

        # Filter by delta range (puts have negative delta — use abs)
        matches = []
        for opt in candidates:
            g = greeks_map.get(opt.streamer_symbol)
            if not g:
                continue
            abs_delta = abs(float(g.delta))
            mid_price = float(g.price)  # theoretical mid
            if (
                min_delta <= abs_delta <= max_delta
                and mid_price >= min_premium
            ):
                dte = (opt.expiration_date - today).days
                pop = round((1 - abs_delta) * 100, 1)
                matches.append({
                    "option": opt,
                    "greeks": g,
                    "abs_delta": abs_delta,
                    "mid_price": mid_price,
                    "dte": dte,
                    "pop": pop,
                })

        if not matches:
            await api_client.post_log(
                "info",
                f"[{self.name}] {symbol}: No puts in delta {min_delta}-{max_delta} with premium ≥ ${min_premium}",
                strategy_id=self.strategy_id,
            )
            return

        # Pick the best: closest to target delta (0.30 default), highest premium as tiebreaker
        target_delta: float = self.params.get("targetDelta", 0.30)
        matches.sort(key=lambda x: (abs(x["abs_delta"] - target_delta), -x["mid_price"]))
        best = matches[0]

        opt = best["option"]
        mid = best["mid_price"]
        dte = best["dte"]
        pop = best["pop"]
        abs_delta = best["abs_delta"]

        await api_client.post_log(
            "info",
            (
                f"[{self.name}] {symbol}: Best put — {opt.symbol} "
                f"strike ${float(opt.strike_price):.0f}, DTE {dte}, "
                f"delta {abs_delta:.2f}, POP {pop}%, premium ${mid:.2f}, "
                f"underlying ${float(underlying_price):.2f}"
            ),
            strategy_id=self.strategy_id,
        )

        # Place the order
        await order_executor.place_option_order(
            session=self.session,
            account=self.account,
            option_symbol=opt.symbol,
            action="SELL_TO_OPEN",
            quantity=max_contracts,
            limit_price=Decimal(str(round(mid, 2))),
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            dry_run=self.dry_run,
            underlying_symbol=symbol,
        )
        self._increment_trades()
