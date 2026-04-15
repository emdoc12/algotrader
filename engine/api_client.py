"""
API Client — talks to the AlgoTrader Node.js REST API.
Used by strategies to read config and write trades/logs.
"""
import logging
import httpx
from config import API_BASE_URL

logger = logging.getLogger(__name__)

_client = httpx.AsyncClient(base_url=API_BASE_URL, timeout=10.0)


async def get_strategies() -> list[dict]:
    """Fetch all enabled strategies from the web app."""
    r = await _client.get("/api/strategies")
    r.raise_for_status()
    return [s for s in r.json() if s.get("isEnabled")]


async def get_accounts() -> list[dict]:
    r = await _client.get("/api/accounts")
    r.raise_for_status()
    return r.json()


async def get_watchlist(strategy_id: int) -> list[str]:
    """Return list of symbols on the watchlist for a strategy."""
    r = await _client.get(f"/api/watchlist/{strategy_id}")
    r.raise_for_status()
    return [item["symbol"] for item in r.json()]


async def post_trade(trade: dict) -> dict:
    """Record a trade execution in the web app."""
    r = await _client.post("/api/trades", json=trade)
    r.raise_for_status()
    return r.json()


async def post_log(level: str, message: str, strategy_id: int | None = None, details: dict | None = None):
    """Post a bot log entry to the web app."""
    payload: dict = {"level": level, "message": message}
    if strategy_id is not None:
        payload["strategyId"] = strategy_id
    if details:
        import json
        payload["details"] = json.dumps(details)
    try:
        r = await _client.post("/api/logs", json=payload)
        r.raise_for_status()
    except Exception as e:
        logger.warning("Failed to post log to API: %s", e)


async def update_strategy(strategy_id: int, data: dict):
    r = await _client.patch(f"/api/strategies/{strategy_id}", json=data)
    r.raise_for_status()
    return r.json()


async def sync_positions(account_id: int, positions: list[dict]):
    """Upsert current positions into the web app."""
    r = await _client.post(f"/api/positions", json={"accountId": account_id, "positions": positions})
    # gracefully ignore if endpoint doesn't exist yet
    if r.status_code not in (200, 201, 404):
        r.raise_for_status()
