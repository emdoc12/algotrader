# daytrader — SPY / Mag7 intraday backtesting system

A ground-up rewrite focused on **day-trading SPY and the Mag7** (AAPL, MSFT,
GOOGL, AMZN, NVDA, META, TSLA) with an honest, realistic backtester. The goal
is a strategy *book* with **profit factor ≥ 2.0**, **max drawdown < 10%**, and
positive alpha versus buy-and-hold SPY.

> The legacy crypto bot still lives under `engine/` and in git history. This new
> system is independent and lives entirely under `daytrader/`.

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

### Key knobs

- `--risk-per-trade` — % of equity risked entry→stop (default 0.4). The single
  biggest lever on drawdown.
- `--adx` — trend/range threshold for regime gating (default 25).
- `--daily-loss-limit` — halt trading for the day past this % loss (default 2).
- `--max-positions` — cap on simultaneous open positions (default 4).
