# Changelog

All notable changes to AlgoTrader are documented here.
Format follows [Semantic Versioning](https://semver.org): MAJOR.MINOR.PATCH

- **MAJOR** — breaking changes (schema migrations, API redesigns)
- **MINOR** — new features, new strategies, new broker integrations
- **PATCH** — bug fixes, tweaks, performance improvements

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
