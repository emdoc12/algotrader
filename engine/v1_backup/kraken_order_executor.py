"""
Kraken Order Executor
----------------------
Handles spot crypto order placement via Kraken.
Mirrors the interface of order_executor.py so strategies can swap
between Tastytrade and Kraken with a single parameter.

Supports dry_run mode — Kraken's own `validate=True` flag is used,
which means the order is fully validated server-side but never submitted.
"""
import logging
from decimal import Decimal

import api_client
from kraken_session_manager import KrakenSessionManager

logger = logging.getLogger(__name__)


async def place_kraken_order(
    kraken: KrakenSessionManager,
    symbol: str,          # e.g. "BTC/USD"
    action: str,          # "BUY_TO_OPEN" | "SELL_TO_CLOSE" | "BUY_TO_CLOSE" | "SELL_TO_OPEN"
    quantity: Decimal,
    limit_price: Decimal | None,   # None = market order
    strategy_id: int,
    account_id: int,
    dry_run: bool = True,
) -> dict | None:
    """
    Place a spot crypto order on Kraken.

    action is mapped to Kraken's side:
      BUY_TO_OPEN  / BUY_TO_CLOSE  -> "buy"
      SELL_TO_OPEN / SELL_TO_CLOSE -> "sell"

    Returns the trade dict written to the API, or None on failure.
    """
    side = "buy" if action.startswith("BUY") else "sell"

    try:
        if limit_price is not None:
            result = await kraken.place_limit_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=limit_price,
                validate=dry_run,
            )
        else:
            result = await kraken.place_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                validate=dry_run,
            )

        # Kraken returns {"txid": [...], "descr": {...}} on success
        # In validate (dry_run) mode it returns {} with no error
        if "error" in result and result["error"]:
            logger.error("Kraken order rejected: %s", result["error"])
            await api_client.post_log(
                "error",
                f"Kraken order rejected for {symbol}: {result['error']}",
                strategy_id=strategy_id,
            )
            return None

        txids = result.get("txid", [])
        order_id = txids[0] if txids else None
        price_val = float(limit_price) if limit_price else 0.0

        trade = {
            "strategyId": strategy_id,
            "accountId": account_id,
            "platform": "kraken",
            "symbol": symbol,
            "action": action,
            "instrumentType": "crypto",
            "quantity": float(quantity),
            "price": price_val,
            "status": "pending" if dry_run else "filled",
            "orderId": order_id,
            "notes": "dry_run" if dry_run else None,
        }

        await api_client.post_trade(trade)
        await api_client.post_log(
            "trade",
            f"{'[DRY RUN] ' if dry_run else ''}Kraken {action} {float(quantity):.6f} {symbol}"
            + (f" @ ${price_val:.2f}" if limit_price else " (market)"),
            strategy_id=strategy_id,
        )
        return trade

    except Exception as e:
        logger.exception("Kraken order failed for %s: %s", symbol, e)
        await api_client.post_log(
            "error",
            f"Kraken order exception for {symbol}: {e}",
            strategy_id=strategy_id,
        )
        return None
