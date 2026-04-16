"""
Iron Condor Strategy
---------------------
Sells an OTM put spread + OTM call spread simultaneously for a net credit.
Best in low-volatility / range-bound environments.

Parameters:
  minDTE:      30
  maxDTE:      55
  shortDelta:  0.16   (target delta for both short legs)
  width:       5      (spread width in dollars for each wing)
  minCredit:   1.50   (minimum total net credit)
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


class IronCondorStrategy(BaseStrategy):

    async def scan(self):
        symbols = await api_client.get_watchlist(self.strategy_id)
        if not symbols:
            await api_client.post_log("warn", f"[{self.name}] Watchlist empty.", strategy_id=self.strategy_id)
            return

        min_dte: int = self.params.get("minDTE", 30)
        max_dte: int = self.params.get("maxDTE", 55)
        short_delta: float = self.params.get("shortDelta", 0.16)
        width: float = self.params.get("width", 5)
        min_credit: float = self.params.get("minCredit", 1.50)
        max_contracts: int = int(self.max_position_size)

        await api_client.post_log(
            "info",
            f"[{self.name}] Scanning {len(symbols)} symbol(s) for iron condors.",
            strategy_id=self.strategy_id,
        )

        async with DXLinkStreamer(self.session) as streamer:
            for symbol in symbols:
                await self._scan_symbol(
                    symbol, streamer, min_dte, max_dte,
                    short_delta, width, min_credit, max_contracts,
                )

    async def _scan_symbol(
        self, symbol, streamer, min_dte, max_dte,
        short_delta, width, min_credit, max_contracts,
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

        mid_dte = (min_dte + max_dte) / 2
        exp = min(valid_exps, key=lambda e: abs((e - today).days - mid_dte))
        dte = (exp - today).days

        all_options = chain[exp]
        puts = [o for o in all_options if o.option_type == OptionType.PUT]
        calls = [o for o in all_options if o.option_type == OptionType.CALL]

        streamer_syms = [o.streamer_symbol for o in puts + calls]
        await streamer.subscribe(Greeks, streamer_syms)

        greeks_map: dict[str, Greeks] = {}
        async for g in streamer.listen(Greeks):
            greeks_map[g.event_symbol] = g
            if len(greeks_map) >= len(streamer_syms):
                break

        def find_short_and_long(options, delta_target, wing: str):
            """Find short strike closest to delta_target, then long strike `width` further OTM."""
            best_short = min(
                ((o, abs(abs(float(greeks_map[o.streamer_symbol].delta)) - delta_target))
                 for o in options if o.streamer_symbol in greeks_map),
                key=lambda x: x[1],
                default=None,
            )
            if not best_short:
                return None, None

            short_opt = best_short[0]
            short_strike = float(short_opt.strike_price)
            long_target = short_strike - width if wing == "put" else short_strike + width

            best_long = min(
                options,
                key=lambda o: abs(float(o.strike_price) - long_target),
                default=None,
            )
            return short_opt, best_long

        short_put, long_put = find_short_and_long(puts, short_delta, "put")
        short_call, long_call = find_short_and_long(calls, short_delta, "call")

        if not all([short_put, long_put, short_call, long_call]):
            await api_client.post_log(
                "warn",
                f"[{self.name}] {symbol}: Could not build full condor for {exp}",
                strategy_id=self.strategy_id,
            )
            return

        def price(opt):
            g = greeks_map.get(opt.streamer_symbol)
            return float(g.price) if g else 0.0

        put_credit = price(short_put) - price(long_put)
        call_credit = price(short_call) - price(long_call)
        total_credit = round(put_credit + call_credit, 2)

        await api_client.post_log(
            "info",
            (
                f"[{self.name}] {symbol}: Iron condor "
                f"P{float(short_put.strike_price):.0f}/{float(long_put.strike_price):.0f} "
                f"C{float(short_call.strike_price):.0f}/{float(long_call.strike_price):.0f} "
                f"DTE {dte}, total credit ${total_credit:.2f}"
            ),
            strategy_id=self.strategy_id,
        )

        if total_credit < min_credit:
            await api_client.post_log(
                "info",
                f"[{self.name}] {symbol}: Credit ${total_credit:.2f} below minimum ${min_credit:.2f} — skipping.",
                strategy_id=self.strategy_id,
            )
            return

        # Place put spread
        await order_executor.place_spread_order(
            session=self.session,
            account=self.account,
            short_symbol=short_put.symbol,
            long_symbol=long_put.symbol,
            quantity=max_contracts,
            net_credit=Decimal(str(round(put_credit, 2))),
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            dry_run=self.dry_run,
        )
        # Place call spread
        await order_executor.place_spread_order(
            session=self.session,
            account=self.account,
            short_symbol=short_call.symbol,
            long_symbol=long_call.symbol,
            quantity=max_contracts,
            net_credit=Decimal(str(round(call_credit, 2))),
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            dry_run=self.dry_run,
        )
        self._increment_trades()
