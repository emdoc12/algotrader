"""
Crypto Momentum Strategy
-------------------------
Buys when the price breaks above the N-day EMA by X%,
sells (closes) when price drops to stop-loss or take-profit.

Works on both Tastytrade (Tasty Crypto) and Kraken (24/7 spot).
When platform='kraken', uses KrakenSessionManager for pricing and orders.
When platform='tasty_crypto', uses DXLinkStreamer + Tastytrade order executor.

Parameters:
  maPeriod:          20     (EMA period)
  breakoutPercent:   2.0    (% above EMA to trigger a buy)
  stopLossPercent:   3.0    (% below entry to exit)
  takeProfitPercent: 6.0    (% above entry to take profits)
  symbols:           ['BTC/USD', 'ETH/USD']  (from watchlist)
"""
import logging
from decimal import Decimal

import api_client
import order_executor
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class CryptoMomentumStrategy(BaseStrategy):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        k = 2 / (ma_period + 1)

        use_kraken = self.platform == "kraken"

        if use_kraken:
            await self._scan_kraken(symbols, k, ma_period, breakout_pct, stop_loss_pct, take_profit_pct, quantity)
        else:
            await self._scan_tastytrade(symbols, k, ma_period, breakout_pct, stop_loss_pct, take_profit_pct, quantity)

    # ── Kraken path ─────────────────────────────────────────

    async def _scan_kraken(self, symbols, k, ma_period, breakout_pct, stop_loss_pct, take_profit_pct, quantity):
        import kraken_order_executor
        kraken = self.kraken  # injected by engine

        for symbol in symbols:
            try:
                ticker = await kraken.get_ticker(symbol)
                mid = ticker["mid"]
            except Exception as e:
                logger.warning("[%s] Failed to get Kraken ticker for %s: %s", self.name, symbol, e)
                continue

            await self._process_signal(
                symbol=symbol, mid=mid, k=k, ma_period=ma_period,
                breakout_pct=breakout_pct, stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct, quantity=quantity,
                executor=lambda sym, action, qty, price: kraken_order_executor.place_kraken_order(
                    kraken=kraken, symbol=sym, action=action,
                    quantity=Decimal(str(qty)), limit_price=Decimal(str(round(price, 4))),
                    strategy_id=self.strategy_id, account_id=self.account_id,
                ),
            )

    # ── Tastytrade path ─────────────────────────────────────

    async def _scan_tastytrade(self, symbols, k, ma_period, breakout_pct, stop_loss_pct, take_profit_pct, quantity):
        from tastytrade.dxfeed import Quote
        from tastytrade import DXLinkStreamer

        async with DXLinkStreamer(self.session) as streamer:
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
            await self._process_signal(
                symbol=symbol, mid=mid, k=k, ma_period=ma_period,
                breakout_pct=breakout_pct, stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct, quantity=quantity,
                executor=lambda sym, action, qty, price: order_executor.place_crypto_order(
                    session=self.session, account=self.account,
                    symbol=sym, action=action,
                    quantity=Decimal(str(qty)), limit_price=Decimal(str(round(price, 2))),
                    strategy_id=self.strategy_id, account_id=self.account_id,
                ),
            )

    # ── Shared signal logic ─────────────────────────────────

    async def _process_signal(self, symbol, mid, k, ma_period, breakout_pct,
                               stop_loss_pct, take_profit_pct, quantity, executor):
        if symbol not in self._ema:
            self._ema[symbol] = mid
        else:
            self._ema[symbol] = mid * k + self._ema[symbol] * (1 - k)

        ema = self._ema[symbol]
        entry = self._entry_price.get(symbol)
        breakout_threshold = ema * (1 + breakout_pct)

        await api_client.post_log(
            "info",
            f"[{self.name}] {symbol}: price=${mid:.4f}, EMA({ma_period})=${ema:.4f}, threshold=${breakout_threshold:.4f}",
            strategy_id=self.strategy_id,
        )

        # Exit logic
        if entry:
            stop = entry * (1 - stop_loss_pct)
            target = entry * (1 + take_profit_pct)
            if mid <= stop:
                await api_client.post_log("trade", f"[{self.name}] {symbol}: STOP LOSS @ ${mid:.4f}", strategy_id=self.strategy_id)
                await executor(symbol, "SELL_TO_CLOSE", quantity, mid)
                self._entry_price.pop(symbol, None)
                self._increment_trades()
                return
            elif mid >= target:
                await api_client.post_log("trade", f"[{self.name}] {symbol}: TAKE PROFIT @ ${mid:.4f}", strategy_id=self.strategy_id)
                await executor(symbol, "SELL_TO_CLOSE", quantity, mid)
                self._entry_price.pop(symbol, None)
                self._increment_trades()
                return

        # Entry logic
        if not entry and mid >= breakout_threshold:
            await api_client.post_log("info", f"[{self.name}] {symbol}: BREAKOUT — ${mid:.4f} > ${breakout_threshold:.4f}", strategy_id=self.strategy_id)
            await executor(symbol, "BUY_TO_OPEN", quantity, mid)
            self._entry_price[symbol] = mid
            self._increment_trades()
