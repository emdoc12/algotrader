"""
Order Executor — wraps the Tastytrade SDK order placement.
Supports dry-run mode (validate only) and live execution.
"""
import logging
from decimal import Decimal
from tastytrade import Account, Session
from tastytrade.instruments import Equity, Option, Cryptocurrency
from tastytrade.order import (
    NewOrder,
    OrderAction,
    OrderTimeInForce,
    OrderType,
)
import api_client
from config import DRY_RUN

logger = logging.getLogger(__name__)


def _action(action_str: str) -> OrderAction:
    return {
        "BUY_TO_OPEN": OrderAction.BUY_TO_OPEN,
        "SELL_TO_OPEN": OrderAction.SELL_TO_OPEN,
        "BUY_TO_CLOSE": OrderAction.BUY_TO_CLOSE,
        "SELL_TO_CLOSE": OrderAction.SELL_TO_CLOSE,
    }[action_str]


async def place_option_order(
    session: Session,
    account: Account,
    option_symbol: str,       # e.g. 'SNDK 260620P00500000'
    action: str,              # 'SELL_TO_OPEN' etc.
    quantity: int,
    limit_price: Decimal,
    strategy_id: int,
    account_id: int,
    underlying_symbol: str,
) -> dict | None:
    """
    Place a single-leg option order.
    Returns the trade dict written to the API, or None on failure.
    """
    try:
        option = Option.get(session, option_symbol)
        leg = option.build_leg(Decimal(quantity), _action(action))

        # Options: negative price = credit received
        price = limit_price if action.startswith("BUY") else -limit_price

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[leg],
            price=price,
        )

        logger.info(
            "[%s] %s %dx %s @ $%.2f (dry_run=%s)",
            "DRY" if DRY_RUN else "LIVE",
            action,
            quantity,
            option_symbol,
            float(limit_price),
            DRY_RUN,
        )

        response = account.place_order(session, order, dry_run=DRY_RUN)

        if response.errors:
            err_msgs = [e.message for e in response.errors]
            logger.error("Order rejected: %s", err_msgs)
            await api_client.post_log(
                "error",
                f"Order rejected for {option_symbol}: {err_msgs}",
                strategy_id=strategy_id,
            )
            return None

        status = "pending" if DRY_RUN else "filled"
        order_id = None if DRY_RUN else str(response.order.id)

        trade = {
            "strategyId": strategy_id,
            "accountId": account_id,
            "platform": "tastytrade",
            "symbol": option_symbol,
            "action": action,
            "instrumentType": "option",
            "quantity": quantity,
            "price": float(limit_price),
            "status": status,
            "orderId": order_id,
            "optionDetails": None,
        }
        await api_client.post_trade(trade)
        await api_client.post_log(
            "trade",
            f"{'[DRY RUN] ' if DRY_RUN else ''}Placed {action} {quantity}x {option_symbol} @ ${float(limit_price):.2f}",
            strategy_id=strategy_id,
        )
        return trade

    except Exception as e:
        logger.exception("Failed to place order for %s: %s", option_symbol, e)
        await api_client.post_log(
            "error",
            f"Order exception for {option_symbol}: {e}",
            strategy_id=strategy_id,
        )
        return None


async def place_spread_order(
    session: Session,
    account: Account,
    short_symbol: str,
    long_symbol: str,
    quantity: int,
    net_credit: Decimal,
    strategy_id: int,
    account_id: int,
) -> dict | None:
    """
    Place a two-leg credit spread (short + long).
    net_credit should be the credit you expect to receive (positive = credit).
    """
    try:
        short_opt = Option.get(session, short_symbol)
        long_opt = Option.get(session, long_symbol)

        short_leg = short_opt.build_leg(Decimal(quantity), OrderAction.SELL_TO_OPEN)
        long_leg = long_opt.build_leg(Decimal(quantity), OrderAction.BUY_TO_OPEN)

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[short_leg, long_leg],
            price=-net_credit,  # negative = net credit
        )

        logger.info(
            "[%s] SPREAD %dx %s / %s @ $%.2f credit",
            "DRY" if DRY_RUN else "LIVE",
            quantity,
            short_symbol,
            long_symbol,
            float(net_credit),
        )

        response = account.place_order(session, order, dry_run=DRY_RUN)

        if response.errors:
            err_msgs = [e.message for e in response.errors]
            logger.error("Spread order rejected: %s", err_msgs)
            await api_client.post_log(
                "error",
                f"Spread rejected {short_symbol}/{long_symbol}: {err_msgs}",
                strategy_id=strategy_id,
            )
            return None

        label = f"{short_symbol} / {long_symbol}"
        trade = {
            "strategyId": strategy_id,
            "accountId": account_id,
            "platform": "tastytrade",
            "symbol": label,
            "action": "SELL_TO_OPEN",
            "instrumentType": "option",
            "quantity": quantity,
            "price": float(net_credit),
            "status": "pending" if DRY_RUN else "filled",
        }
        await api_client.post_trade(trade)
        await api_client.post_log(
            "trade",
            f"{'[DRY RUN] ' if DRY_RUN else ''}Spread {quantity}x {label} @ ${float(net_credit):.2f} credit",
            strategy_id=strategy_id,
        )
        return trade

    except Exception as e:
        logger.exception("Spread order failed: %s", e)
        await api_client.post_log("error", f"Spread order exception: {e}", strategy_id=strategy_id)
        return None


async def place_crypto_order(
    session: Session,
    account: Account,
    symbol: str,          # e.g. 'BTC/USD'
    action: str,
    quantity: Decimal,
    limit_price: Decimal,
    strategy_id: int,
    account_id: int,
) -> dict | None:
    """Place a crypto market/limit order."""
    try:
        crypto = Cryptocurrency.get(session, symbol)
        leg = crypto.build_leg(quantity, _action(action))

        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[leg],
            price=limit_price if action.startswith("BUY") else -limit_price,
        )

        logger.info(
            "[%s] CRYPTO %s %.4f %s @ $%.2f",
            "DRY" if DRY_RUN else "LIVE",
            action,
            float(quantity),
            symbol,
            float(limit_price),
        )

        response = account.place_order(session, order, dry_run=DRY_RUN)

        if response.errors:
            err_msgs = [e.message for e in response.errors]
            logger.error("Crypto order rejected: %s", err_msgs)
            await api_client.post_log("error", f"Crypto order rejected {symbol}: {err_msgs}", strategy_id=strategy_id)
            return None

        trade = {
            "strategyId": strategy_id,
            "accountId": account_id,
            "platform": "tasty_crypto",
            "symbol": symbol,
            "action": action,
            "instrumentType": "crypto",
            "quantity": float(quantity),
            "price": float(limit_price),
            "status": "pending" if DRY_RUN else "filled",
        }
        await api_client.post_trade(trade)
        await api_client.post_log(
            "trade",
            f"{'[DRY RUN] ' if DRY_RUN else ''}Crypto {action} {float(quantity):.4f} {symbol} @ ${float(limit_price):.2f}",
            strategy_id=strategy_id,
        )
        return trade

    except Exception as e:
        logger.exception("Crypto order failed: %s", e)
        await api_client.post_log("error", f"Crypto order exception: {e}", strategy_id=strategy_id)
        return None
