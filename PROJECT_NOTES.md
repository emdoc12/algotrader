# AlgoTrader

An autonomous, AI-driven **equity, futures and options trading** system. The
legacy crypto bot has been removed; everything lives under `daytrader/`.

Two halves:

1. **Backtester** — a realistic, look-ahead-free intraday backtester for liquid US
   stocks + ETFs, with nine strategies, a regime-gated ensemble, walk-forward and
   Monte-Carlo validation, and an HTML report. See `daytrader/README.md` and
   `daytrader/RESULTS.md`.

2. **Competing agent desks** — seven AI trading desks (Claude, OpenAI, Grok, Qwen,
   DeepSeek, GLM, Kimi), each a full multi-agent team (Strategist / Trader /
   Reviewer) running on its own model with an identical paper account, the same
   tools, and the same data. A web dashboard shows the standings, a profit/loss
   comparison chart, per-team thinking/trades, and a chat with each team leader.

   Each desk started at **$25,000** and received a one-time **$25,000** owner
   deposit, for a **$50,000** capital base. That deposit is booked as a capital
   event, never as profit: return is measured against the capital base, and the
   chart plots P&L with contributed capital removed, so adding money does not
   look like earning it.

## What the desks can trade

The engine settles all of these correctly — an instrument it cannot price is
refused rather than silently mispriced as shares.

* **Stocks and ETFs**, long or short, including leveraged and inverse ETFs.
  Fractional quantities supported.
* **Listed futures**, sized in contracts with real multipliers and margin
  (`daytrader/core/contracts.py`). Margin is pledged, not spent. Only the micros
  (MES/MNQ/MGC/M2K/MYM/MCL) size sensibly against these accounts.
* **Options**, single-leg and multi-leg (`daytrader/core/options.py`,
  `daytrader/live/options_book.py`): cash-secured puts, the full wheel through
  assignment and call-away, credit spreads, iron condors, LEAPs. Real 100x
  multiplier, premium, collateral, assignment and exercise. **Defined risk only** —
  max loss is computed from the payoff, so a naked short call is rejected by
  arithmetic rather than by matching its name.

Horizons are a decision, not a default: `day` (flattened at the close), `swing`
(days), `long` (weeks+).

## Risk rails

Broker-enforced, not merely prompted. An order that breaches one is rejected with
an actionable message. All are editable in the dashboard Settings tab.

| Rail | Default | Env |
|---|---|---|
| Per-trade risk (shares/futures, entry→stop) | 1.5% of equity | `MAX_TRADE_RISK_PCT` |
| Per-trade risk (options) | 5% of equity | `MAX_OPTION_RISK_PCT` |
| Portfolio heat — Σ open risk, all instruments | 8% of equity | `MAX_PORTFOLIO_HEAT_PCT` |
| Daily realized loss before new entries stop | 3% of equity | `DAILY_LOSS_LIMIT_PCT` |
| Drawdown from peak triggering cooling-off | 8% | `COOLDOWN_DRAWDOWN_PCT` |
| Collateral one options structure may tie up | 25% of equity | `MAX_OPTION_COLLATERAL_PCT` |
| Averaging down (adding while underwater) | blocked | `ALLOW_AVERAGE_DOWN` |

Options risk is measured honestly: width-bounded structures (spreads, condors)
are sized on their true max loss, while structures exposed to the underlying
itself (cash-secured puts, covered calls) are sized on a stress loss at a 20%
adverse move — a cash-secured put's strike-to-zero worst case fits no per-trade
cap, and a rule nobody can follow gets bypassed rather than obeyed. The full
obligation is still held as collateral.

Each desk must also **declare a strategy** (`declare_strategy`) and keep it for
`STRATEGY_COMMIT_DAYS` before switching, so per-strategy records mean something.

## Data sources

* **Yahoo / loader** — bars and quotes for the scanned watchlist.
* **tastytrade** — the owner's account, strictly **READ-ONLY**: live option chains
  with streaming Greeks, quotes, and (optionally) mirrored margin terms. The desks
  never place an order against it.
* **Alpha Vantage** — historical option chains with full Greeks for research,
  cached to disk permanently since history never changes. Free key.
* **Optional research feeds** — Polygon, Unusual Whales, BullFlow, Quiver, Finviz.
  Each enables itself when its key is present.

## Research loop

Desks pre-register hypotheses (`propose_hypothesis`), which are judged later
against hard out-of-sample periods with a significance bar corrected across every
hypothesis all seven desks have tested. Accepted rules can be deployed
(`deploy_strategy`) to feed the Trader mechanical signals. Rejection is the
expected outcome. See `daytrader/research/`.

## Quick start

```bash
# Backtest
python -m daytrader backtest --interval 5m --html report.html
python -m daytrader walkforward --interval 5m

# Competing agent desks (set the API keys for whichever teams you want)
python -m daytrader.agent serve        # web dashboard + run all teams (http://localhost:3737)
python -m daytrader.agent leaderboard  # print standings
python -m daytrader.agent status       # what the agents see (no API key needed)
```

Keys and rails are editable at runtime from the dashboard **Settings** tab; they
apply on the next cycle without a restart.

## Brokerage (for going live)

Recommended for an options-capable automated bot: **Alpaca** (#1 — API-first, free
paper that mirrors live, native multi-leg options, $0 commissions), **tastytrade**
(options-native runner-up), **IBKR** (serious-money alternative). Note: the US PDT
$25k day-trading minimum was eliminated on 2026-06-04, so a small automated account
is no longer frozen for day trading — though a margin account funded above $25k is
the safest structure during the broker rollout transition.

## Deployment

`Dockerfile` builds the competing-desks service (web dashboard on port 3737 +
the trading loop). State persists in per-team SQLite DBs under `/app/data`.

## Conventions

See `CLAUDE.md` — chiefly: everything is pushed to `main`, and `VERSION` +
`CHANGELOG.md` move together on every shipped change.
