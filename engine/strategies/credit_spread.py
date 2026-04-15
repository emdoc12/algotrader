"""
Credit Spread Strategy (Put or Call)
-------------------------------------
Sells the short strike near target delta, buys the long strike `width` points
further OTM for defined-risk protection.

Parameters:
  minDTE:      30
  maxDTE:      60
  shortDelta:  0.30   (target delta for the short leg)
  width:       5      (spread width in dollars)
  minCredit:   0.80   (minimum net credit to collect, in dollars)
  spreadType:  'put'  (or 'call' — default put for bullish bias)
  maxContracts: 1
"""
import logging
from decimal import Decimal
from datetime import date

from tastytrade.instruments import get_option_chain, OptionType
from tastytrade.dxfeed import Greeks
from tastytrade import DXLinkStreamer

import api_client
import order_executor
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class CreditSpreadStrategy(BaseStrategy):

    async def scan(self):
        symbols = await api_client.get_watchlist(self.strategy_id)
        if not symbols:
            await api_client.post_log("warn", f"[{self.name}] Watchlist empty.", strategy_id=self.strategy_id)
            return

        min_dte: int = self.params.get("minDTE", 30)
        max_dte: int = self.params.get("maxDTE", 60)
        short_delta: float = self.params.get("shortDelta", 0.30)
        width: float = self.params.get("width", 5)
        min_credit: float = self.params.get("minCredit", 0.80)
        spread_type: str = self.params.get("spreadType", "put")
        max_contracts: int = int(self.max_position_size)
        opt_type = OptionType.PUT if spread_type == "put" else OptionType.CALL

        await api_client.post_log(
            "info",
            f"[{self.name}] Scanning {len(symbols)} symbol(s) for {spread_type} credit spreads.",
            strategy_id=self.strategy_id,
        )

        async with DXLinkStreamer(self.session) as streamer:
            for symbol in symbols:
                await self._scan_symbol(
                    symbol, streamer, min_dte, max_dte,
                    short_delta, width, min_credit, opt_type, max_contracts,
                )

    async def _scan_symbol(
        self, symbol, streamer, min_dte, max_dte,
        short_delta, width, min_credit, opt_type, max_contracts,
    ):
        try:
            chain = get_option_chain(self.session, symbol)
        except Exception as e:
            logger.warning("Failed to get chain for %s: %s", symbol, e)
            return

        today = date.today()
        valid_exps = [e for e in chain if min_dte <= (e - today).days <= max_dte]
        if not valid_exps:
            return

        # Use the expiration with DTE closest to the midpoint of our window
        mid_dte = (min_dte + max_dte) / 2
        best_exp = min(valid_exps, key=lambda e: abs((e - today).days - mid_dte))

        options = [o for o in chain[best_exp] if o.option_type == opt_type]
        streamer_symbols = [o.streamer_symbol for o in options]

        await streamer.subscribe(Greeks, streamer_symbols)
        greeks_map: dict[str, Greeks] = {}
        async for g in streamer.listen(Greeks):
            greeks_map[g.event_symbol] = g
            if len(greeks_map) >= len(streamer_symbols):
                break

        # Find the short strike closest to target delta
        best_short = None
        best_diff = float("inf")
        for opt in options:
            g = greeks_map.get(opt.streamer_symbol)
            if not g:
                continue
            diff = abs(abs(float(g.delta)) - short_delta)
            if diff < best_diff:
                best_diff = diff
                best_short = (opt, g)

        if not best_short:
            return

        short_opt, short_g = best_short
        short_strike = float(short_opt.strike_price)
        short_price = float(short_g.price)

        # Long strike is `width` points further OTM
        if opt_type == OptionType.PUT:
            long_strike_target = short_strike - width
        else:
            long_strike_target = short_strike + width

        # Find closest long strike
        best_long = None
        best_long_diff = float("inf")
        for opt in options:
            diff = abs(float(opt.strike_price) - long_strike_target)
            if diff < best_long_diff:
                best_long_diff = diff
                best_long = opt

        if not best_long:
            return

        long_g = greeks_map.get(best_long.streamer_symbol)
        long_price = float(long_g.price) if long_g else 0.0
        net_credit = round(short_price - long_price, 2)

        dte = (best_exp - today).days
        pop = round((1 - abs(float(short_g.delta))) * 100, 1)

        await api_client.post_log(
            "info",
            (
                f"[{self.name}] {symbol}: {opt_type.value} spread "
                f"{short_strike:.0f}/{float(best_long.strike_price):.0f} "
                f"DTE {dte}, POP {pop}%, net credit ${net_credit:.2f}"
            ),
            strategy_id=self.strategy_id,
        )

        if net_credit < min_credit:
            await api_client.post_log(
                "info",
                f"[{self.name}] {symbol}: Credit ${net_credit:.2f} below minimum ${min_credit:.2f} — skipping.",
                strategy_id=self.strategy_id,
            )
            return

        await order_executor.place_spread_order(
            session=self.session,
            account=self.account,
            short_symbol=short_opt.symbol,
            long_symbol=best_long.symbol,
            quantity=max_contracts,
            net_credit=Decimal(str(net_credit)),
            strategy_id=self.strategy_id,
            account_id=self.account_id,
        )
        self._increment_trades()
