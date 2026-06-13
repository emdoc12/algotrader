# daytrader — intraday backtester + competing AI trading desks

An honest intraday **backtester** for liquid US stocks + ETFs, plus a live
**competition** in which four AI desks (Claude, OpenAI, Grok, Qwen) each trade an
identical **$10,000** paper account and a web dashboard shows who's winning.

The backtester targets a strategy *book* with **profit factor ≥ 2.0**, **max
drawdown < 10%**, and positive alpha versus buy-and-hold SPY.

> The legacy crypto bot has been removed; everything lives under `daytrader/`.

Jump to the live competition: [Competing AI trading desks](#competing-ai-trading-desks-paper).

## Why you can trust the numbers

The fastest way to a fake 3:1 profit factor is look-ahead bias, ignored costs,
and reporting only the data you fit on. This backtester is built to avoid all
three:

- **No look-ahead.** A signal decided on bar *t* (using its close) is filled at
  the **open of bar t+1**. Every indicator is causal.
- **Realistic fills.** Each fill pays half-spread + slippage; stops are market
  orders and suffer **gap-through-stop** fills; targets fill at the limit. A
  `--pessimistic` preset stress-tests at the example dashboard's 0.4% slippage.
- **True day trading.** Positions are force-flattened at the session close and a
  daily loss limit halts trading after a bad day.
- **Out-of-sample.** `walkforward` scores the *same* book on in-sample and
  untouched out-of-sample windows. `monte_carlo_dd` reshuffles the trade
  sequence to judge drawdown against a distribution, not one lucky ordering.
- **No survivorship bias.** The universe is a fixed set of large caps that were
  all listed across the whole window.

### Honest limitations (free data)

Yahoo only serves ~60 days of 5-minute history and ~2 years of hourly bars, so
long backtests use coarser bars. Short borrow/locate costs, per-name market
impact, and intraday corporate actions are **not** modeled. Absolute returns are
optimistic; use them for direction, not promises. The report prints a
**reality score** stating exactly what is and isn't accounted for.

## Layout

```
daytrader/
  core/        types (Bar/Signal/Trade/Position) + causal indicators
  data/        Yahoo loader with on-disk cache (5m/15m/1h/1d)
  strategies/  one file per strategy, all subclassing Strategy
  backtest/    engine (realistic fills), metrics, runner, validate, tune
  risk/        position sizing + risk budget
  portfolio/   regime classifier, ensemble, the production "book"
  report/      HTML report with inline SVG equity-vs-SPY chart
  cli.py       command-line entry point
```

## Strategies

| Name      | Type            | Edge regime | Idea |
|-----------|-----------------|-------------|------|
| ORB       | momentum        | any         | first breakout of the opening range |
| VWAP-Trend| trend pullback  | trend       | buy pullbacks to a rising session VWAP |
| EMA-Pull  | trend pullback  | trend       | pullback to fast EMA in an EMA-stacked trend |
| MACD      | trend continuation | trend    | MACD zero-line cross with ADX + trend filter |
| VWAP-MR   | mean reversion  | range       | fade stretched moves back to VWAP |
| RSI2      | mean reversion  | range       | Connors RSI(2) dip-buy above a trend filter |
| BB-Fade   | mean reversion  | range       | fade Bollinger band touches to the midline |
| Pivot     | mean reversion  | range       | fade prior-day floor-trader pivots (S/R) |
| Gap       | gap fill / go   | any         | fade or follow the opening gap vs prior close |

The **ensemble** gates each strategy by ADX regime so mean-reverters don't fire
in trends and trend-followers don't fire in chop.

## Usage

The default book is the validated `trend` profile with the SPY market-direction
filter on. See `RESULTS.md` for the honest scorecard and what the targets
actually achieved.

```bash
# full honest scorecard: 5m walk-forward + 1h multi-regime check
python -m daytrader.evaluate

# headline backtest on 60 days of 5-minute bars, with HTML report
python -m daytrader backtest --interval 5m --html report.html

# out-of-sample validation + Monte-Carlo drawdown
python -m daytrader walkforward --interval 5m --oos 0.35 --html report.html

# include every strategy (adds mean-reversion — drags in a trend), or go long-only
python -m daytrader backtest --interval 5m --profile all
python -m daytrader backtest --interval 5m --long-only

# stress test at 0.4% slippage; disable the market filter
python -m daytrader backtest --interval 5m --pessimistic --no-market-filter

# diagnostics
python -m daytrader strategies
python -m daytrader correlation --interval 5m --profile all
```

## Competing AI trading desks (paper)

Four AI desks compete on an even field. Each is a **full multi-agent team** —
**Strategist** (plans the day), **Trader** (runs each cycle and places trades),
**Reviewer** (journals lessons, files dev requests) — and **every member runs on
that team's own model**:

| Team | Model (default) | Endpoint | API key env |
|------|-----------------|----------|-------------|
| claude | `claude-opus-4-8` | Anthropic | `ANTHROPIC_API_KEY` |
| openai | `gpt-5.1` | OpenAI | `OPENAI_API_KEY` |
| grok | `grok-4` | xAI (`https://api.x.ai/v1`) | `XAI_API_KEY` |
| qwen | `qwen3.6` | DashScope (OpenAI-compatible) | `DASHSCOPE_API_KEY` |

Every team starts with an identical **$10,000**, the same tools, and the same
data — the only variable is the model. Teams whose API key isn't set are skipped,
so you can run any subset. They trade the day's **scanned watchlist** of liquid
US stocks + ETFs (148-name universe; options come online once a brokerage is
connected). Model, endpoint, and key are all env-overridable (`OPENAI_MODEL`,
`XAI_MODEL`, `QWEN_MODEL`, `OPENAI_BASE_URL`, `XAI_BASE_URL`, `QWEN_BASE_URL`, …).

Hard risk limits live in code, not the model's discretion: a per-team daily-loss
circuit breaker, one position per symbol, and a forced flat at 15:50 ET. All
state persists to per-team SQLite DBs, so a restart resumes mid-day with positions
and memory intact. When a team is blocked by something only a developer can fix,
it **files a GitHub issue** via `request_dev_help`.

### Web dashboard

`python -m daytrader.agent serve` starts the trading loop **and** a dashboard at
`http://localhost:3737`:
- **Overview** — live leaderboard plus an equity-curve chart overlaying all four
  teams against the $10k line.
- **Per-team tabs** — positions, trades, the team's full thinking feed
  (journal + agent log), and dev requests.
- **Chat with the team leader** — message any desk's lead (runs on that team's
  model with its trading context) to ask about trades or suggest changes.

```bash
python -m daytrader.agent serve         # dashboard + run all teams (the default service)
python -m daytrader.agent compete       # headless competition loop
python -m daytrader.agent leaderboard   # print standings and exit
python -m daytrader.agent status        # what a desk sees (no API key needed)
```

Deploy with the top-level `Dockerfile` (exposes 3737, persists per-team DBs under
`/app/data`). Set the API keys for whichever teams you want to run.

**Running Qwen locally:** point it at any OpenAI-compatible local server (vLLM,
Ollama, LM Studio) — e.g. `QWEN_BASE_URL=http://host:11434/v1`,
`QWEN_MODEL=qwen3.6`, and any non-empty placeholder in `DASHSCOPE_API_KEY`.

## Brokerage (going live)

For an options-capable automated bot, the researched recommendation is **Alpaca**
(#1 — API-first, free paper that mirrors live, native multi-leg options L3, $0
commissions, official MCP server), **tastytrade** (options-native runner-up), and
**IBKR** (serious-money alternative). The US Pattern-Day-Trader $25k minimum was
eliminated on 2026-06-04, so a small automated account is no longer frozen for day
trading — though a margin account above $25k is the safest structure during the
broker rollout. Full comparison in the project notes.

### Key knobs

- `--risk-per-trade` — % of equity risked entry→stop (default 0.4). The single
  biggest lever on drawdown.
- `--adx` — trend/range threshold for regime gating (default 25).
- `--daily-loss-limit` — halt trading for the day past this % loss (default 2).
- `--max-positions` — cap on simultaneous open positions (default 4).
