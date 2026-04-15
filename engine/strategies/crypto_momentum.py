"""
Crypto Momentum Strategy
-------------------------
Buys when the price breaks above the N-day moving average by X%,
sells (closes) when price drops back below the MA by X%.

Requires a price history source. Uses the Tastytrade quote streamer
for current price and relies on the quote history endpoint for MA calc.
Falls back to a simple EMA approximation if history isn't available.

Parameters:
  maPeriod:          20     (MA period in days)
  breakoutPercent:   2.0    (% above MA to trigger a buy)
  stopLossPercent:   3.0    (% below entry to exit)
  takeProfitPercent: 6.0    (% above entry to take profits)
  quantity:          0.1    (from maxPositionSize — crypto units to buy)
  symbols:           ['BTC/USD', 'ETH/USD']  (from watchlist)
"""
import logging
from decimal import Decimal

from tastytrade.instruments import Cryptocurrency
from tastytrade.dxfeed import Quote
from tastytrade import DXLinkStreamer

import api_client
import order_executor
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class CryptoMomentumStrategy(BaseStrategy):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Track running EMA per symbol
        self._ema: dict[str, float] = {}
        self._entry_price: dict[str, float] = {}

    async def scan(self):
        symbols = await api_client.get_watchlist(self.strategy_id)
        if not symbols:
            await api_client.post_log("warn", f"[{self.name}] Watchlist empty.", strategy_id=self.strategy_id)
            return

        ma_period: int = self.params.get("maPeriod", 20)
        breakout_pct: float = self.params.get("breakoutPercent", 2.0) / 100
        stop_loss_pct: float = self.params.get("stopLossPercent", 3.0) / 100
        take_profit_pct: float = self.params.get("takeProfitPercent", 6.0) / 100
        quantity: float = self.max_position_size

        # EMA smoothing factor
        k = 2 / (ma_period + 1)

        async with DXLinkStreamer(self.session) as streamer:
            # Get current mid prices for all symbols
            await streamer.subscribe(Quote, symbols)
            quotes: dict[str, Quote] = {}
            async for q in streamer.listen(Quote):
                quotes[q.event_symbol] = q
                if len(quotes) >= len(symbols):
                    break

            for symbol in symbols:
                q = quotes.get(symbol)
                if not q:
                    continue

                mid = (q.bid_price + q.ask_price) / 2

                # Update EMA
                if symbol not in self._ema:
                    self._ema[symbol] = mid
                else:
                    self._ema[symbol] = mid * k + self._ema[symbol] * (1 - k)

                ema = self._ema[symbol]
                entry = self._entry_price.get(symbol)
                breakout_threshold = ema * (1 + breakout_pct)

                await api_client.post_log(
                    "info",
                    f"[{self.name}] {symbol}: price=${mid:.2f}, EMA({ma_period})=${ema:.2f}, breakout threshold=${breakout_threshold:.2f}",
                    strategy_id=self.strategy_id,
                )

                # --- Exit logic (if in position) ---
                if entry:
                    stop = entry * (1 - stop_loss_pct)
                    target = entry * (1 + take_profit_pct)
                    if mid <= stop:
                        await api_client.post_log(
                            "trade",
                            f"[{self.name}] {symbol}: STOP LOSS triggered @ ${mid:.2f} (entry ${entry:.2f})",
                            strategy_id=self.strategy_id,
                        )
                        await order_executor.place_crypto_order(
                            session=self.session, account=self.account,
                            symbol=symbol, action="SELL_TO_CLOSE",
                            quantity=Decimal(str(quantity)),
                            limit_price=Decimal(str(round(mid, 2))),
                            strategy_id=self.strategy_id,
                            account_id=self.account_id,
                        )
                        self._entry_price.pop(symbol, None)
                        self._increment_trades()
                        continue
                    elif mid >= target:
                        await api_client.post_log(
                            "trade",
                            f"[{self.name}] {symbol}: TAKE PROFIT @ ${mid:.2f} (entry ${entry:.2f})",
                            strategy_id=self.strategy_id,
                        )
                        await order_executor.place_crypto_order(
                            session=self.session, account=self.account,
                            symbol=symbol, action="SELL_TO_CLOSE",
                            quantity=Decimal(str(quantity)),
                            limit_price=Decimal(str(round(mid, 2))),
                            strategy_id=self.strategy_id,
                            account_id=self.account_id,
                        )
                        self._entry_price.pop(symbol, None)
                        self._increment_trades()
                        continue

                # --- Entry logic (if not in position) ---
                if not entry and mid >= breakout_threshold:
                    await api_client.post_log(
                        "info",
                        f"[{self.name}] {symbol}: BREAKOUT detected — ${mid:.2f} > EMA threshold ${breakout_threshold:.2f}",
                        strategy_id=self.strategy_id,
                    )
                    await order_executor.place_crypto_order(
                        session=self.session, account=self.account,
                        symbol=symbol, action="BUY_TO_OPEN",
                        quantity=Decimal(str(quantity)),
                        limit_price=Decimal(str(round(mid, 2))),
                        strategy_id=self.strategy_id,
                        account_id=self.account_id,
                    )
                    self._entry_price[symbol] = mid
                    self._increment_trades()
