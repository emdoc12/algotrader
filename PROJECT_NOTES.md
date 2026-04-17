# AlgoTrader — AI-Powered Crypto Trading Bot

## Overview
A 24/7 autonomous crypto trading bot that uses Claude AI as its decision engine. Trades 10 coins on Kraken (paper mode), runs in Docker on Unraid, and reports to Discord.

**Current Version:** v2.8.4

---

## Architecture

```
Unraid Server (Docker)
├── engine/bot.py              ← Main loop, scan scheduler, weekly digest trigger
├── engine/ai_strategy.py      ← Claude AI decision engine (the brain)
├── engine/dashboard.py        ← Web UI on port 3737 (live data + chat with Claude)
├── engine/database.py         ← SQLite persistence (trades, positions, journals, research)
├── engine/kraken_client.py    ← Kraken REST API client (public + authenticated)
├── engine/market_scanner.py   ← Multi-coin scanner (10 coins, full technicals, MTF, ATR)
├── engine/indicators.py       ← EMA, RSI, Bollinger Bands, ATR (Wilder's smoothing)
├── engine/sentiment.py        ← Fear & Greed Index, news headlines
├── engine/paper_trader.py     ← Paper trading engine with FIFO P&L
├── engine/web_research.py     ← DuckDuckGo + CryptoPanic search (no API keys)
├── engine/whale_monitor.py    ← Large BTC transaction monitoring
├── engine/discord_notifier.py ← Discord webhook notifications
├── engine/config.py           ← Environment-based configuration
├── engine/fix_bad_prices.py   ← Migration script for bad price data cleanup
├── Dockerfile                 ← Python 3.12-slim container
└── VERSION                    ← Semantic version tracking
```

---

## Tradeable Coins
BTC, ETH, SOL, DOGE, ADA, AVAX, LINK, DOT, POL, XRP (all vs USD on Kraken)

---

## Version History

### v2.6.2 — Performance Stats in AI Context
- Win rate, average win/loss, profit factor, max drawdown
- Per-coin and per-strategy performance breakdowns
- All stats fed to Claude every scan cycle so he can see what's working

### v2.7.0 — Persistent Memory + Web Research
- **Strategy Journal:** SQLite table that survives reboots/rebuilds. Claude writes lessons, observations, and insights that get fed back every cycle
- **Web Research:** DuckDuckGo HTML search + CryptoPanic news API. Claude can request research on any topic — results appear next cycle
- No API keys required for either feature

### v2.7.0-hotfix — Critical Price Bug Fix
- **Bug:** All non-BTC coins (DOT, ETH, etc.) were recording BTC's ~$74k price instead of their actual price
- **Root cause:** Symbol comparison used Kraken pair name ("DOTUSD") instead of friendly name ("DOT") — never matched, fell back to BTC price
- **Fix:** Three locations in ai_strategy.py corrected to compare against base coin symbol
- **Migration script** (`fix_bad_prices.py`) to clean up bad historical trade data

### v2.8.0 — Six Major Trading Features
1. **Multi-timeframe analysis** — 15m, 1h, 4h candle indicators (was BTC-only, now all coins as of v2.8.2)
2. **Order book depth** — Bid/ask walls, spread, imbalance from Kraken depth API
3. **Whale monitoring** — Large BTC transactions via Blockchain.com + Blockchair (>10 BTC)
4. **BTC dominance** — Real-time from CoinGecko free API, alt season detection
5. **ATR volatility sizing** — Wilder's smoothing ATR for all timeframes, volatility labels (low/medium/high/extreme)
6. **Drawdown circuit breaker** — When equity drops 5%+ from peak, position sizes are automatically halved

### v2.8.1 — Discord Webhook Notifications
- Trade alerts (buy/sell) with coin, price, quantity, value, fee, P&L, account balance, holdings
- Stop-loss trigger alerts
- Take-profit trigger alerts
- Hourly equity snapshots (throttled, includes open positions and drawdown)
- Drawdown circuit breaker activation alerts
- Bot startup notification (mode + version)
- Rate limit handling (429s logged, not crashed)

### v2.8.2 — All-Coin Multi-Timeframe + ATR
- **Before:** MTF (1h, 4h) and ATR only computed for BTC
- **After:** All 10 coins get full 1h and 4h indicators + ATR at every timeframe
- 20 concurrent Kraken API calls per scan (10 coins x 2 timeframes)
- Per-coin alignment check (bullish/bearish/conflicting across timeframes)
- Claude sees ATR volatility for every coin — uses it for smarter position sizing

### v2.8.3 — Research Notebook
- Dedicated `research_notebook` SQLite table, separate from the trade journal
- No limit on entries — Claude sees ALL active notes every cycle
- Topics: macro, technical, coin_analysis, strategy, risk, news, hypothesis
- Claude can mark notes as stale when they're outdated (soft delete by ID)
- System prompt encourages proactive thinking during HOLD cycles
- max_tokens bumped from 700 to 1500 so Claude can write real analysis

### v2.8.4 — Weekly Monday Digest
- Every Monday at 7 AM Eastern, Claude writes a weekly briefing to Discord
- Reviews all trades, journal entries, and research notes from the past 7 days
- Three sections: Week in Review, Key Lessons, Plan for This Week
- References actual numbers, actual trades — no generic filler
- Stats embed: trade count, win rate, week P&L, equity, all-time P&L, research note count
- Handles Discord's 2000-char message limit (auto-splits long digests)
- DST-aware: covers both EDT (UTC-4) and EST (UTC-5)

---

## What Claude Sees Every Scan Cycle

### Market Data (all 10 coins)
- Current price, 1h change, 24h change, volume
- EMA (9/21) crossover status
- RSI with signal (oversold/overbought/neutral)
- Bollinger Bands (position, bandwidth)
- ATR in USD and % of price, volatility label
- Composite score and recommendation
- Momentum score and relative strength vs BTC

### Multi-Timeframe (all 10 coins)
- 15-minute indicators (primary)
- 1-hour indicators + ATR
- 4-hour indicators + ATR
- Per-coin alignment verdict across all three timeframes

### Order Book (BTC/USD)
- Bid/ask spread and spread %
- Bid depth vs ask depth in USD
- Order imbalance (-1 to +1)
- Largest bid wall and ask wall (price + volume)

### Whale Activity
- Large BTC transactions (>10 BTC)
- Exchange inflow vs outflow
- Net flow direction
- Alert level (normal/elevated/high)

### Global Market
- BTC dominance % (from CoinGecko)
- ETH dominance %
- Total market cap and 24h volume
- Alt season warnings

### Sentiment
- Fear & Greed Index (current, yesterday, week ago)
- Price momentum (1h, 24h)
- Volume trend
- News headlines and sentiment summary

### Account State
- Cash balance, total equity, all-time P&L
- All open positions with unrealized P&L
- Holdings per coin
- Peak equity and current drawdown %
- Drawdown circuit breaker status

### Performance Stats
- Win rate, profit factor, max drawdown
- Per-coin breakdown (win rate, W/L count, P&L)
- Per-strategy breakdown

### Memory Systems
- **Strategy Journal** — Recent lessons and observations (top 15 by confidence)
- **Research Notebook** — All active research notes (no limit), with IDs for stale-marking
- **Web Research** — Results from last requested search query

---

## Infrastructure

### Docker Deployment
- Image: `emdoc12/algotrader:latest` on GHCR
- GitHub Actions auto-builds on push to main
- Runs on Unraid with `/app/data` volume for SQLite persistence
- Health check via dashboard on port 3737

### Environment Variables
| Variable | Purpose |
|----------|---------|
| `BOT_MODE` | `paper` or `live` |
| `ANTHROPIC_API_KEY` | Claude API access |
| `KRAKEN_API_KEY` | Kraken trading (live mode) |
| `KRAKEN_API_SECRET` | Kraken auth |
| `DISCORD_WEBHOOK_URL` | Discord notifications |
| `BOT_DB_PATH` | SQLite path (default: `/app/data/bot_data.db`) |
| `BOT_LOG_LEVEL` | Logging level (default: `INFO`) |
| `DASHBOARD_PORT` | Web UI port (default: `3737`) |

### Key Design Decisions
- **No paid APIs** — All market data from free sources (Kraken public, CoinGecko free, DuckDuckGo, CryptoPanic, Blockchain.com, Blockchair)
- **Paper mode first** — All trading logic works identically in paper and live mode
- **FIFO P&L** — Buy lots matched first-in-first-out for accurate cost basis
- **Fee awareness** — 0.26% taker fee baked into every decision. Sells under 0.6% profit are blocked (except stop-losses)
- **Async everything** — httpx + asyncio for concurrent API calls
- **Shared HTTP client** — Single httpx.AsyncClient shared across scanner, whale monitor, Discord, and research modules

---

## Discord Notifications

Claude sends to a private Discord server via webhook:
- **Trade alerts** — Every buy/sell with full details and account balance
- **Stop-loss / take-profit** — Special formatted alerts when triggered
- **Hourly equity snapshots** — Portfolio summary (throttled to once per hour)
- **Drawdown alerts** — When the 5% circuit breaker activates
- **Startup notification** — Mode and version when bot comes online
- **Weekly digest** — Monday 7 AM ET, AI-written summary of the past week + plan ahead

---

## Future Plans
- Polymarket trading bot (separate bot, same Discord server)
- Discord chat with Claude (phase 2 — two-way conversation via bot token)
- Live trading mode activation (when paper performance is proven)
