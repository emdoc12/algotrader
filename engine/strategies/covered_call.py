"""
Covered Call Strategy
----------------------
Sells OTM calls against an existing long equity position.
Requires you to already own the shares (100 per contract).

Parameters:
  minDTE:      20
  maxDTE:      45
  targetDelta: 0.30
  minPremium:  0.30
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


class CoveredCallStrategy(BaseStrategy):

    async def scan(self):
        symbols = await api_client.get_watchlist(self.strategy_id)
        if not symbols:
            await api_client.post_log("warn", f"[{self.name}] Watchlist empty.", strategy_id=self.strategy_id)
            return

        min_dte: int = self.params.get("minDTE", 20)
        max_dte: int = self.params.get("maxDTE", 45)
        target_delta: float = self.params.get("targetDelta", 0.30)
        min_premium: float = self.params.get("minPremium", 0.30)
        max_contracts: int = int(self.max_position_size)

        await api_client.post_log(
            "info",
            f"[{self.name}] Scanning {len(symbols)} symbol(s) for covered calls.",
            strategy_id=self.strategy_id,
        )

        async with DXLinkStreamer(self.session) as streamer:
            for symbol in symbols:
                await self._scan_symbol(
                    symbol, streamer, min_dte, max_dte,
                    target_delta, min_premium, max_contracts,
                )

    async def _scan_symbol(
        self, symbol, streamer, min_dte, max_dte,
        target_delta, min_premium, max_contracts,
    ):
        try:
            chain = get_option_chain(self.session, symbol)
        except Exception as e:
            logger.warning("Chain fetch failed for %s: %s", symbol, e)
            return

        today = date.today()
        valid_exps = [e for e in chain if min_dte <= (e - today).days <= max_dte]
        if not valid_exps:
            return

        # Use the earliest valid expiration (shortest DTE, lowest premium decay risk)
        exp = min(valid_exps, key=lambda e: (e - today).days)
        calls = [o for o in chain[exp] if o.option_type == OptionType.CALL]
        streamer_syms = [o.streamer_symbol for o in calls]

        await streamer.subscribe(Greeks, streamer_syms)
        greeks_map: dict[str, Greeks] = {}
        async for g in streamer.listen(Greeks):
            greeks_map[g.event_symbol] = g
            if len(greeks_map) >= len(streamer_syms):
                break

        # Find call closest to target delta
        best = None
        best_diff = float("inf")
        for opt in calls:
            g = greeks_map.get(opt.streamer_symbol)
            if not g:
                continue
            diff = abs(float(g.delta) - target_delta)
            mid = float(g.price)
            if diff < best_diff and mid >= min_premium:
                best_diff = diff
                best = (opt, g)

        if not best:
            await api_client.post_log(
                "info",
                f"[{self.name}] {symbol}: No qualifying calls found.",
                strategy_id=self.strategy_id,
            )
            return

        opt, g = best
        dte = (exp - today).days
        mid = float(g.price)

        await api_client.post_log(
            "info",
            f"[{self.name}] {symbol}: Covered call {opt.symbol} DTE {dte}, delta {float(g.delta):.2f}, premium ${mid:.2f}",
            strategy_id=self.strategy_id,
        )

        await order_executor.place_option_order(
            session=self.session,
            account=self.account,
            option_symbol=opt.symbol,
            action="SELL_TO_OPEN",
            quantity=max_contracts,
            limit_price=Decimal(str(round(mid, 2))),
            strategy_id=self.strategy_id,
            account_id=self.account_id,
            underlying_symbol=symbol,
        )
        self._increment_trades()
