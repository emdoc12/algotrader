# Changelog

All notable changes to AlgoTrader are documented here.
Format follows [Semantic Versioning](https://semver.org): MAJOR.MINOR.PATCH

- **MAJOR** — breaking changes (schema migrations, API redesigns)
- **MINOR** — new features, new strategies, new broker integrations
- **PATCH** — bug fixes, tweaks, performance improvements

---

## [6.8.0] — 2026-06-15

### Fixed
- **Dev requests now persist without a `GITHUB_TOKEN` — and say so.** Filing a
  request always wrote to the local DB (and thus the dashboard), but
  `file_dev_request` returned `ok: False` when no token was set, so the desks
  reasonably concluded their request had vanished. It now returns a truthful
  `recorded` flag, and the `request_dev_help` tool replies with a clear note:
  "Saved to the dev-requests page … GitHub mirror skipped (no token) but your
  request IS persisted." **No token is required** for the dev-request workflow;
  a token only adds optional GitHub-issue mirroring.

### Added
- **Dev requests can be CLOSED now** (the missing half of the workflow). New
  `resolve_dev_request(id, status, resolution)` agent tool — added to the
  Reviewer, who is now instructed at EOD to close any open request whose
  tool/data/fix has actually shipped, with a one-line verification note.
  Backed by a new `LiveDB.update_dev_request` + `get_dev_request` and a
  forward-only migration that adds `resolution` / `resolved_ts` columns.
- **"Mark done" buttons on the dashboard** — both the per-team Dev requests
  card and the Health tab's open-requests list now show the request id and a
  one-click close (POST `/api/devrequest/close`), so the owner can clean up the
  page directly too.
- **Relative strength vs SPY baked into every snapshot** (Team Claude's dev
  request #3). Each symbol's indicator block now carries `rs_vs_spy_pct`
  (symbol % change − SPY % change over the last ~30 min) and `rs_rank`
  (1 = strongest), computed from bars already loaded that cycle — no extra
  fetches. SPY is loaded as the benchmark even when it isn't on the watchlist.

---

## [6.7.0] — 2026-06-15

### Fixed
- **Feed-vs-broker price gap closed** (Claude's #1 escalation — was flipping
  winners into losers). The market snapshot and the paper broker now draw from
  ONE shared quote source (`daytrader/data/quotes.py`) backed by Yahoo's
  chart-meta `regularMarketPrice` (the official last trade, fresher than the
  last 1-minute bar close). The competition loop pins each cycle's quote map
  onto the broker for the duration of that cycle, so the price the agent
  reasoned over **is** the price the broker fills at — zero drift. Also fixes
  the BA-style "price-feed discrepancy" pattern on names whose 1m bar lagged
  the live tape.
- **Indicators for held positions outside the day's scan.** When a team holds
  a symbol that isn't on the day's scanned watchlist, `with_account` now
  fetches its bars + live quote and adds a full indicator block to the
  snapshot, so the trader is never flying blind on what it already owns.

### Added (agent capabilities)
- **`get_recent_trades`** — detailed round-trip trade blotter (entry/exit time
  + price, qty, commission, slippage, pnl, exit reason, rationale). Asked for
  by Team OpenAI for post-trade review. Also added to the Reviewer's allowed
  tools.
- **`get_opening_range(symbol, minutes=15)`** — today's first N minutes for
  trend-day detection: O/H/L/C, volume, range %, gap from prior close. Asked
  for by Team Qwen.
- **`get_relative_strength_vs_spy(symbols, lookback_minutes=30)`** — ranks a
  list of symbols by intraday RS vs SPY (sym% − SPY%). Asked for by Team Qwen.
- **Mission: fractional shares are explicit + risk floor stated.** The mission
  text now tells every desk that `qty` accepts fractional values (e.g. 0.05)
  and to size trades to ~0.2–0.5% of equity (~$50–$125 on $25k). This unblocks
  Team Grok, which was sitting on cash because its risk math couldn't justify
  a whole share of expensive names. The `place_trade` schema description was
  also updated to advertise fractional support.

### Notes
- Unusual Whales tools (`uw_flow_alerts`, `uw_ticker_flow`, `uw_dark_pool`,
  `uw_market_overview`) have been available since v6.5.0 — they appear in each
  desk's tool inventory automatically when `UNUSUAL_WHALES_API_KEY` is set.
  Team Qwen's dev request for "real-time unusual options flow + dark pool"
  should now be visible in its inventory.
- The recurring Anthropic `500 / Internal Server Error` was a transient
  upstream API failure (not a code bug); the existing trade loop tolerates it
  and retries on the next cycle.

---

## [6.6.2] — 2026-06-15

### Fixed
- **Dashboard is now mobile-friendly.** Added a responsive layout for phones /
  narrow screens (≤640px): the version badge stacks above the title instead of
  overlapping it, the tab bar scrolls horizontally rather than wrapping into a
  pile, padding/fonts tighten up, and — the big one — the wide data tables
  (especially the 10-column leaderboard) now scroll sideways *inside their card*
  instead of forcing the whole page to overflow. Inputs use 16px text so iOS
  Safari no longer zooms in when you tap the chat box. Pure CSS — no behavior
  change on desktop.

---

## [6.6.1] — 2026-06-14

### Fixed
- **Dashboard header is now dynamic.** The subtitle shows the real starting cash
  (so it reads **$25,000**, not a hardcoded $10k) and a **version badge** (e.g.
  `v6.6.1`) sits in the top-right so you can glance up and confirm you're on the
  latest build. Both are rendered server-side from the VERSION file + START_CASH,
  so they never go stale. `VERSION` is now copied into the container image.

---

## [6.6.0] — 2026-06-14

### Added
- **Web + YouTube research tools** (always on, no key) — `web_search`,
  `web_fetch`, `youtube_search`, `youtube_transcript`. The desks can browse the
  open web and read video transcripts to discover and learn ANY strategy,
  including ones traders/influencers teach. (YouTube transcript fetch is blocked
  from datacenter IPs but works from a residential IP like a home server.)
- **Explicit tool inventory** injected into each desk's prompt, so every team
  knows exactly which tools/data sources it has at its disposal (varies by which
  keys are set).
- **`python -m daytrader.agent reset`** — wipe per-team DBs for a clean restart.

### Changed
- **Starting cash per team: $10k → $25k** (more buffer for strategies). Run
  `reset` (or clear `team_*.db`) once so existing desks restart at $25k.
- Mission now grants full strategy freedom (invent/adopt any strategy, not just
  the built-ins) and explicitly invites the desks to file dev requests for any
  data/tool/strategy they think would give them an edge.

---

## [6.5.0] — 2026-06-13

### Added
- **External research-data feeds** the desks can query on demand to hunt for an
  edge — pluggable read-only adapters under `daytrader/data/feeds/`, each behind
  its own API key (Settings → Research data providers), merged into the desks'
  toolset only when configured:
  - **Polygon.io** — `polygon_quote`, `polygon_news`, `polygon_aggregates`, `polygon_movers`.
  - **Unusual Whales** — `uw_flow_alerts`, `uw_ticker_flow`, `uw_dark_pool`, `uw_market_overview`.
  - **BullFlow** — `bullflow_alerts`, `bullflow_ticker` (SSE-snapshot reader).
  - **Quiver Quant** — `quiver_congress`, `quiver_insiders`, `quiver_wsb`, `quiver_gov_contracts`.
  - **Finviz Elite** — `finviz_screener`, `finviz_news` (authenticated CSV export).
  Strategist + Trader can call them; the mission prompt nudges using flow/news/
  screeners for confluence. All adapters are stdlib-only, defensive (never raise,
  short-TTL cached), and READ-ONLY.

### Notes
- BullFlow field names and a couple of endpoints are inferred from limited public
  docs and may need a small tweak once tested with a live key.

---

## [6.4.2] — 2026-06-13

### Fixed
- **OpenAI GPT-5-family models** (e.g. `gpt-5.1`) reject `max_tokens` and require
  `max_completion_tokens`. The OpenAI-compatible provider now detects this and
  switches automatically (caching the choice), so OpenAI works while Grok/Qwen
  keep using `max_tokens`.

---

## [6.4.1] — 2026-06-13

### Fixed
- **OpenAI-compatible provider** no longer sends `tool_choice` when there are no
  tools — xAI Grok (and others) reject that, which made the Grok connectivity
  test and the chat-with-leader feature fail with a 400. Tools/`tool_choice` are
  now only sent when tools are present.

---

## [6.4.0] — 2026-06-13

### Changed
- **tastytrade auth switched to OAuth** so 2FA-protected accounts work headless
  (no rolling/one-time code to enter). Settings now takes
  `TASTYTRADE_CLIENT_SECRET` + `TASTYTRADE_REFRESH_TOKEN` (generate once on
  tastytrade.com → API → OAuth Applications → Create Grant; the refresh token
  never expires) instead of username/password. Unpinned to `tastytrade>=12`
  (latest SDK is OAuth-only) and migrated the option-chain call to the 12.x
  `get_option_chain` API. Still strictly READ-ONLY — no order code path.

---

## [6.3.0] — 2026-06-13

### Added
- **Health tab** in the dashboard — at-a-glance monitoring: market/data-feed
  status, per-team status (key configured, equity, errors today, halted,
  open positions, last activity), a recent-errors/refusals feed, and the agents'
  open dev requests. Auto-refreshes (DB-only, no API cost).
- **Live API connectivity test** — `GET /api/check`, a "Test APIs now" button on
  the Health tab (and Settings), and a CLI `python -m daytrader.agent check`.
  Pings each team's model with its current key and reports ✓/✗ + latency +
  error detail (surfaces dead keys *and* wrong model IDs).
- **Discord breakage alerts** — when a team's cycle errors, a daily-loss circuit
  breaker trips, or the competition starts, an alert is pushed to
  `DISCORD_WEBHOOK_URL` (throttled). New module `daytrader/live/healthcheck.py`.

### How failures surface
Agent errors/refusals are logged per team (visible in the Health tab and team
thinking feed); the agents file GitHub issues via `request_dev_help` for things
needing a developer; and with a Discord webhook set, breakages are pushed to you.

---

## [6.2.1] — 2026-06-13

### Fixed
- **Dashboard default port reverted to 3737** to match the legacy container.
  v6.x had changed it to 8787, which broke existing Unraid port mappings
  (host:8787 → container:3737). The default is 3737 again so existing mappings
  work unchanged; override with `DASHBOARD_PORT` if desired.

---

## [6.2.0] — 2026-06-13

### Added
- **tastytrade live data feed (READ-ONLY)** (`daytrader/live/tastytrade_data.py`)
  — real-time stock + option quotes and Greeks (delta/gamma/theta/vega/rho/iv)
  via DXLink, plus near-the-money option chains. Enriches the teams' market
  snapshot when tastytrade credentials are set; degrades to the Yahoo feed
  otherwise. **Strictly data/read endpoints — there is no code path that can
  place, modify, or cancel an order on the tastytrade account.** All execution
  stays in the internal paper books.
- tastytrade username/password fields on the dashboard Settings page.

### Notes
- Pinned `tastytrade<10` because the latest SDK (12.x) is OAuth-only; 9.13 keeps
  simple username/password login. (OAuth can be added later if preferred.)

---

## [6.1.0] — 2026-06-13

### Added
- **Settings page** in the dashboard — enter API keys (Claude/OpenAI/Grok/Qwen,
  plus Alpaca) and model/endpoint overrides from the browser. Stored in a
  gitignored `settings.json` in the data volume (chmod 600), masked in the UI,
  never logged. New keys **activate their team within the next cycle, no restart**
  (`Competition._sync_teams`). New module `daytrader/live/settings.py`.

### Fixed
- **Dashboard port.** The new service listens on 8787 (the old crypto dashboard
  used 3737). Added `DASHBOARD_PORT` env support so an existing container/port
  mapping keeps working — set `DASHBOARD_PORT=3737` to reuse the old mapping.

---

## [6.0.0] — 2026-06-13

**Crypto removed. Multi-model competition + web dashboard. $10k per team.**
A breaking, ground-clearing release: the legacy crypto bot and its data are gone;
the project is now purely an equity day-trading backtester plus a live competition
between AI trading desks.

### Removed
- The entire legacy crypto bot (`engine/`), its old databases (`data.db*`,
  `sqlite.db*`), and the crypto Dockerfile. Not coming back.

### Added
- **Model competition** (`daytrader/live/competition.py`) — four desks (Claude,
  OpenAI, Grok, Qwen), each a full multi-agent team running entirely on its own
  model, each with an identical **$10,000** paper account, same tools, same data.
  Per-team daily-loss circuit breaker; teams without an API key are skipped.
- **Provider abstraction** (`daytrader/live/providers.py`) — `AnthropicProvider`
  + `OpenAICompatibleProvider` (covers OpenAI, xAI Grok, and Qwen, including
  local OpenAI-compatible servers via env-overridable base URL).
- **Broadened universe** (`daytrader/data/universe.py`) — 148 liquid US stocks +
  ETFs with a daily liquidity/volatility/momentum scanner that picks each day's
  watchlist (replaces the fixed SPY+Mag7 list).
- **Web dashboard** (`daytrader/live/dashboard.py`) — overview leaderboard +
  equity-curve comparison chart, per-team tabs (positions, trades, full thinking
  feed, dev requests), and chat-with-team-leader. Stdlib-only, offline-capable.
  `python -m daytrader.agent serve` runs the dashboard + competition together.
- **Brokerage recommendation** (PROJECT_NOTES) — Alpaca (#1), tastytrade,
  IBKR for an options-capable automated bot; note that the PDT $25k rule was
  eliminated 2026-06-04.

### Changed
- Starting equity default 100k → **10k**. Agent is now a team of members on a
  per-team model. The top-level `Dockerfile` builds the competition+dashboard
  service (port 8787); CLI is `python -m daytrader.agent {serve,compete,leaderboard,status}`.

---

## [5.1.0] — 2026-06-13

**Autonomous, Claude-powered agent desk for paper trading.** A team of agents
that day-trades SPY + Mag7 during market hours, self-directs, and asks the
developer for help via GitHub issues when blocked. All paper mode.

### Added
- **Agent team** (`daytrader/live/agents.py`) — Strategist (sets the day's plan),
  Trader (runs each intraday cycle and places trades), Reviewer (journals
  lessons, files dev requests). All share one persistent journal as memory.
- **LLM client** (`daytrader/live/llm_client.py`) — official Anthropic SDK,
  manual tool-use loop, adaptive thinking, refusal handling. Default model
  `claude-opus-4-8` (configurable via `AGENT_MODEL`).
- **Audited tool surface** (`daytrader/live/tools.py`) — the only ways an agent
  can act: place_trade, close_position, flatten_all, get_positions,
  get_performance, journal_write, request_dev_help.
- **Paper broker + SQLite persistence** (`daytrader/live/paper_broker.py`,
  `db.py`) — simulated market fills at live prices with realistic slippage,
  long/short accounting, restart-safe state (positions, cash, journal, equity).
- **Market-state snapshot** (`daytrader/live/market_state.py`) — live prices,
  indicators, regime, fresh signals from the validated book, account state.
- **Dev-request channel** (`daytrader/live/dev_requests.py`) — files GitHub
  issues (`GITHUB_TOKEN`/`GITHUB_REPO`), with a DB fallback.
- **Market-hours runner** (`daytrader/live/runner.py`) — open→plan,
  interval→trade, close→flatten+review; hard daily-loss circuit breaker and
  forced EOD flat enforced in code. CLI: `python -m daytrader.agent {run,once,plan,review,status}`.
- **`Dockerfile.agent`** — container for the agent service (separate from the
  legacy crypto image). Requires `ANTHROPIC_API_KEY` at runtime.

### Notes
- `status` runs with no API key (shows what the agents see). The trading
  commands require `ANTHROPIC_API_KEY` and degrade gracefully without it.

---

## [5.0.0] — 2026-06-13

**Ground-up rewrite: SPY / Mag7 intraday day-trading system.** A new, independent
engine that day-trades SPY and the Mag7 (AAPL, MSFT, GOOGL, AMZN, NVDA, META,
TSLA) with a backtester built to be honest rather than flattering. The legacy
crypto bot is untouched and still lives under `engine/`; the new system lives
entirely under `daytrader/`. Major version bump because this is a new product
surface, not an iteration on the crypto bot.

### Added
- **Realistic backtest engine** (`daytrader/backtest/engine.py`) — next-bar
  execution (no look-ahead), slippage + half-spread, gap-through-stop fills,
  forced end-of-day flat, daily loss limit, optional breakeven/trailing stops.
- **Nine intraday strategies** (`daytrader/strategies/`) — Opening Range
  Breakout, VWAP reversion, VWAP-trend pullback, Connors RSI(2), Bollinger fade,
  EMA pullback, MACD continuation, pivot reversal, gap-and-go. All causal and
  lookahead-verified.
- **Regime-gated ensemble + SPY market-direction filter** (`daytrader/portfolio/`)
  — strategies fire only in their suited ADX regime and only with SPY's trend.
- **Validation** (`daytrader/backtest/validate.py`) — walk-forward in-sample /
  out-of-sample split, Monte-Carlo drawdown distribution, strategy correlation.
- **Risk-based position sizing, full metric suite, HTML report** with an inline
  equity-vs-SPY chart and a reality score, plus a CLI (`python -m daytrader …`).
- **Free data loader** (Yahoo Finance) with on-disk caching: 5m/15m (~60d),
  1h (~2y), daily (full history).

### Results (honest)
- Validated `trend` book (5m, market filter): out-of-sample profit factor 1.60,
  max drawdown ~1.7% (Monte-Carlo p95 1.5%), beat SPY out-of-sample by +4.5 pts.
- The 2:1 profit-factor target was **not** robustly met; the sub-10% drawdown
  target was met by a wide margin. Full scorecard and reasoning in
  `daytrader/RESULTS.md`.

### Notes
- The new day trader is CLI-only for now; the Docker image (`docker.yml`) still
  builds and runs the legacy crypto `engine/`.

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
