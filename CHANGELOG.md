# Changelog

All notable changes to AlgoTrader are documented here.
Format follows [Semantic Versioning](https://semver.org): MAJOR.MINOR.PATCH

- **MAJOR** — breaking changes (schema migrations, API redesigns)
- **MINOR** — new features, new strategies, new broker integrations
- **PATCH** — bug fixes, tweaks, performance improvements

---

## [4.3.0] — 2026-05-01

Joint Codex + Claude code review pass. Closes four real money-affecting bugs,
adds a deterministic risk-sizing layer, and gives Claude (the PM) more
expressive trade tags.

### Fixed (Track A — stop the bleeding)
- **`database.py:get_period_pnl`** — was computing realized P&L as
  `sells - buys` over the time window, which double-counted gross transaction
  values and badly misled the weekly digest, monthly stats, and Claude's own
  performance feedback. Now sums FIFO sell P&L from `get_trades_with_pnl`.
- **`ai_strategy.py` agent loop** — typo `kraken.get_ohlc(...)` should have
  been `get_ohlcv(...)`. Every Haiku agent cycle was silently AttributeError-ing
  on BTC candle data between PM sessions.
- **Drawdown circuit breaker survives restarts** — `_peak_equity` no longer
  resets to starting capital on init; it loads `MAX(equity)` from the
  performance snapshot table. Previously a restart silently disabled the
  breaker until a new peak was hit.
- **Paper trader applies slippage** — docstring claimed slippage but no
  slippage was applied; default is now 0.05% per side (configurable via
  `PAPER_SLIPPAGE_PCT`). Paper P&L now resembles what live execution would
  deliver.
- **`bot.py` startup status** — `for/else` clause always logged "No open
  position" because the loop never broke. Restructured.

### Added (Track B — risk hardening)
- **`risk_manager.py`** — central deterministic sizing layer. Every BUY,
  SCALE-IN, and LIMIT_BUY runs through `clamp_buy_size()` which enforces:
  - `max_position_pct` (single position vs equity, default 25%)
  - `max_per_coin_pct` (combined exposure to one coin, default 35%)
  - `max_risk_per_trade_pct` (stop-distance dollars, default 1.5%)
  - `max_total_exposure_pct` (total holdings, default 80% — leaves dry powder)
  - drawdown breaker multiplier (halves size when drawdown active)
  - cash cap (always last; never overspend)
  Each clamp records a reason; the operator sees what bound the size.
- **Daily loss cooldown** — tracks day-start equity at UTC midnight. If
  `daily_loss_limit_pct` (default 4%) is breached, all new buys are blocked
  until midnight. Protective exits still execute.
- **Pending-buy cash reservation** — open buy limit orders subtract from
  "available cash" before sizing. Claude can no longer overcommit by
  stacking GTCs.
- **Pending-buy fills merge into existing positions** — previously a filled
  pending buy always inserted a new `Position` row, so a coin with both a
  market and a limit fill produced split records that broke `get_open_position`,
  scale-in math, and stop placement. Now merges via weighted average.

### Added (Track C — profit upside)
- **USD / risk-dollar trade sizing** — system prompt now teaches Claude to
  express size as `usd=N` (notional dollars) or `risk_usd=N` (stop-distance
  dollars) instead of coin units. Code converts to qty after risk clamps.
  Legacy `qty=` still accepted.
- **Multi-trade per PM session** — Claude can now place up to 3 trade tags
  per response (configurable via `MAX_TRADES_PER_PM_SESSION`). Risk clamps
  apply per-trade and the per-coin / total-exposure caps naturally
  distribute the budget. Previously only the first tag was acted on.
- **Multi-symbol order book depth** — instead of fetching only BTC depth,
  the scanner now grabs concurrent depth for BTC + every open position +
  the top 3 candidates by composite score. Claude sees real spread/wall
  data on thinly-traded alts (POL, DOT, DOGE) before trading them.

### Changed
- `StrategyConfig` defaults tightened: `risk_per_trade_pct` 2.0 → 1.5,
  `max_position_pct` 30 → 25; new `max_per_coin_pct`, `max_total_exposure_pct`,
  `daily_loss_limit_pct`, `max_trades_per_pm_session` fields.
- System prompt expanded with explicit hard sizing caps and daily-loss-limit
  description so Claude reasons inside the same rules the code enforces.

---

## [1.1.0] — 2026-04-15

### Added
- **Kraken integration** — 24/7 spot crypto trading via `python-kraken-sdk`
  - `kraken_session_manager.py` — REST client with ticker, OHLC, balance, and order placement
  - `kraken_order_executor.py` — limit/market order execution with dry-run support
  - Kraken `platform` type in Accounts UI — add your API key/secret from the web dashboard
- **Dual-broker crypto strategies** — `crypto_momentum` and `crypto_mean_reversion` now
  automatically route to Kraken when `platform=kraken`, Tastytrade when `platform=tasty_crypto`
- **`KRAKEN_API_KEY` / `KRAKEN_API_SECRET`** added to `.env.example` and `config.py`
- **Semantic versioning** — `VERSION` file + `CHANGELOG.md` added to repo root
- Engine gracefully skips a broker if its credentials are missing (warns in logs instead of crashing)

### Changed
- `strategies/base.py` — `BaseStrategy.__init__` now accepts optional `kraken` kwarg
- `engine.py` — routes strategies to Kraken or Tastytrade based on `platform` field;
  each broker connects independently on startup
- `requirements.txt` — added `python-kraken-sdk>=3.0.0`, removed `apscheduler` (unused)

---

## [1.0.0] — 2026-04-15

### Added
- Initial release — full AlgoTrader system
- Node.js/React web dashboard (Express + Vite + shadcn/ui + Drizzle/SQLite)
- Python strategy engine sidecar with 6 strategies:
  - **Short Put** — delta/DTE/POP filtering, DXLinkStreamer Greeks
  - **Credit Spread** — put or call spreads, configurable width
  - **Iron Condor** — simultaneous put + call spread
  - **Covered Call** — OTM call against existing long position
  - **Crypto Momentum** — EMA breakout buy with stop/target exit
  - **Crypto Mean Reversion** — EMA dip buy, exits at EMA recovery
- Tastytrade SDK integration (Session auth, Account, DXLinkStreamer)
- REST API sync — strategies, trades, positions, logs all flow through Node.js API
- `DRY_RUN=true` default — no live orders without explicit opt-in
- `run.sh` launch script with auto-dependency install and mode banner

## [1.2.0] - 2026-04-15

### Added
- **Bullflow Options Flow Scanner** (`options_flow_scanner` strategy type)
  - Real-time SSE stream from `api.bullflow.io/v1/streaming/alerts`
  - OCC symbol parser (extracts ticker, expiry, strike, option type, DTE)
  - Composite scoring model (premium size + Repeater pattern weight)
  - Configurable filters: minPremium, minScore, callsOnly, excludeEtfs, minDTE, maxDTE
  - Auto-executes calls or stock via Tastytrade on score threshold
  - All params tunable in the web UI
  - Auto-reconnects on stream drop
  - Daily trade limit + midnight reset
- `BULLFLOW_API_KEY` added to `config.py` and `.env.example`
- Scanner strategy type visible in Strategies page with default params pre-filled
- Account field optional for scanner type (only needed for live execution)

## [1.2.2] - 2026-04-15

### Fixed
- GitHub Actions: add `setup-buildx-action` so GHA cache backend works correctly
- Repo visibility set to public so `ghcr.io/emdoc12/algotrader:latest` is pullable without auth

## [1.2.3] - 2026-04-15

### Fixed
- Dockerfile: run `npm ci` with scripts enabled so `better-sqlite3` native addon compiles correctly
- supervisord: increase engine `startsecs` to 15s so web server is fully up before Python engine connects

## [1.2.4] - 2026-04-15

### Fixed
- supervisord + entrypoint: API_BASE_URL was pointing to port 3000 but Express listens on 5000 — corrected to http://localhost:5000
- Dockerfile: EXPOSE updated to 5000

## [1.2.5] - 2026-04-15

### Fixed
- server/db.ts: auto-create all tables on first boot using CREATE TABLE IF NOT EXISTS — no drizzle-kit push needed in Docker
- Fixes "no such table: bot_logs / strategies" errors on fresh container start
