"""
Crypto Mean Reversion Strategy
--------------------------------
Buys when price dips X% below the N-day EMA (oversold),
takes profit when price returns to the EMA or hits take-profit target.

Works on both Tastytrade (Tasty Crypto) and Kraken (24/7 spot).
When platform='kraken', uses KrakenSessionManager for pricing and orders.
When platform='tasty_crypto', uses DXLinkStreamer + Tastytrade order executor.

Parameters:
  maPeriod:          50
  deviationPercent:  5.0    (% below EMA to trigger a buy)
  stopLossPercent:   3.0    (% further below entry to cut losses)
  takeProfitPercent: 4.0    (% above entry to close — ideally at or above EMA)
"""
import logging
from decimal import Decimal

import api_client
import order_executor
from strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class CryptoMeanReversionStrategy(BaseStrategy):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ema: dict[str, float] = {}
        self._entry_price: dict[str, float] = {}

    async def scan(self):
        symbols = await api_client.get_watchlist(self.strategy_id)
        if not symbols:
            await api_client.post_log("warn", f"[{self.name}] Watchlist empty.", strategy_id=self.strategy_id)
            return

        ma_period: int = self.params.get("maPeriod", 50)
        deviation_pct: float = self.params.get("deviationPercent", 5.0) / 100
        stop_loss_pct: float = self.params.get("stopLossPercent", 3.0) / 100
        take_profit_pct: float = self.params.get("takeProfitPercent", 4.0) / 100
        quantity: float = self.max_position_size
        k = 2 / (ma_period + 1)

        if self.platform == "kraken":
            await self._scan_kraken(symbols, k, ma_period, deviation_pct, stop_loss_pct, take_profit_pct, quantity)
        else:
            await self._scan_tastytrade(symbols, k, ma_period, deviation_pct, stop_loss_pct, take_profit_pct, quantity)

    # ── Kraken path ─────────────────────────────────────────

    async def _scan_kraken(self, symbols, k, ma_period, deviation_pct, stop_loss_pct, take_profit_pct, quantity):
        import kraken_order_executor
        kraken = self.kraken

        for symbol in symbols:
            try:
                ticker = await kraken.get_ticker(symbol)
                mid = ticker["mid"]
            except Exception as e:
                logger.warning("[%s] Kraken ticker failed for %s: %s", self.name, symbol, e)
                continue

            await self._process_signal(
                symbol=symbol, mid=mid, k=k, ma_period=ma_period,
                deviation_pct=deviation_pct, stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct, quantity=quantity,
                executor=lambda sym, action, qty, price: kraken_order_executor.place_kraken_order(
                    kraken=kraken, symbol=sym, action=action,
                    quantity=Decimal(str(qty)), limit_price=Decimal(str(round(price, 4))),
                    strategy_id=self.strategy_id, account_id=self.account_id,
                    dry_run=self.dry_run,
                ),
            )

    # ── Tastytrade path ─────────────────────────────────────

    async def _scan_tastytrade(self, symbols, k, ma_period, deviation_pct, stop_loss_pct, take_profit_pct, quantity):
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
                deviation_pct=deviation_pct, stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct, quantity=quantity,
                executor=lambda sym, action, qty, price: order_executor.place_crypto_order(
                    session=self.session, account=self.account,
                    symbol=sym, action=action,
                    quantity=Decimal(str(qty)), limit_price=Decimal(str(round(price, 2))),
                    strategy_id=self.strategy_id, account_id=self.account_id,
                    dry_run=self.dry_run,
                ),
            )

    # ── Shared signal logic ─────────────────────────────────

    async def _process_signal(self, symbol, mid, k, ma_period, deviation_pct,
                               stop_loss_pct, take_profit_pct, quantity, executor):
        if symbol not in self._ema:
            self._ema[symbol] = mid
        else:
            self._ema[symbol] = mid * k + self._ema[symbol] * (1 - k)

        ema = self._ema[symbol]
        dip_threshold = ema * (1 - deviation_pct)
        entry = self._entry_price.get(symbol)

        await api_client.post_log(
            "info",
            f"[{self.name}] {symbol}: price=${mid:.4f}, EMA({ma_period})=${ema:.4f}, dip threshold=${dip_threshold:.4f}",
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
            elif mid >= target or mid >= ema:
                await api_client.post_log("trade", f"[{self.name}] {symbol}: MEAN REVERSION exit @ ${mid:.4f} (EMA ${ema:.4f})", strategy_id=self.strategy_id)
                await executor(symbol, "SELL_TO_CLOSE", quantity, mid)
                self._entry_price.pop(symbol, None)
                self._increment_trades()
                return

        # Entry logic
        if not entry and mid <= dip_threshold:
            await api_client.post_log("info", f"[{self.name}] {symbol}: DIP entry — ${mid:.4f} below EMA threshold ${dip_threshold:.4f}", strategy_id=self.strategy_id)
            await executor(symbol, "BUY_TO_OPEN", quantity, mid)
            self._entry_price[symbol] = mid
            self._increment_trades()
