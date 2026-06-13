# AlgoTrader

An autonomous, AI-driven **equity day-trading** system. The legacy crypto bot has
been removed; everything lives under `daytrader/`.

Two halves:

1. **Backtester** — a realistic, look-ahead-free intraday backtester for liquid US
   stocks + ETFs, with nine strategies, a regime-gated ensemble, walk-forward and
   Monte-Carlo validation, and an HTML report. See `daytrader/README.md` and
   `daytrader/RESULTS.md`.

2. **Competing agent desks** — four AI trading desks (Claude, OpenAI, Grok, Qwen),
   each a full multi-agent team (Strategist / Trader / Reviewer) running on its own
   model with an identical **$10,000** paper account, the same tools, and the same
   data. They day-trade the scanned liquid-stock/ETF watchlist (options once a
   brokerage is connected), build persistent memory, and file GitHub issues when
   they need developer help. A web dashboard shows the standings, comparison graphs,
   per-team thinking/trades, and a chat with each team leader.

## Quick start

```bash
# Backtest
python -m daytrader backtest --interval 5m --html report.html
python -m daytrader walkforward --interval 5m

# Competing agent desks (set the API keys for whichever teams you want)
python -m daytrader.agent serve        # web dashboard + run all teams (http://localhost:8787)
python -m daytrader.agent leaderboard  # print standings
python -m daytrader.agent status       # what the agents see (no API key needed)
```

## Brokerage (for going live)

Recommended for an options-capable automated bot: **Alpaca** (#1 — API-first, free
paper that mirrors live, native multi-leg options, $0 commissions), **tastytrade**
(options-native runner-up), **IBKR** (serious-money alternative). Note: the US PDT
$25k day-trading minimum was eliminated on 2026-06-04, so a small automated account
is no longer frozen for day trading — though a margin account funded above $25k is
the safest structure during the broker rollout transition.

## Deployment

`Dockerfile` builds the competing-desks service (web dashboard on port 8787 +
the trading loop). State persists in per-team SQLite DBs under `/app/data`.
