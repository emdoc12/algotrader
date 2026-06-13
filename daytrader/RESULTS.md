# Results — honest scorecard

**Universe:** SPY + Mag7 (AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA)
**Data:** free Yahoo Finance. 5-minute bars cover ~60 days; hourly bars ~2 years.
**Costs modeled:** 2 bps slippage + 1 bps half-spread per side, gap-through-stop
on market exits. Next-bar execution (no look-ahead). Flat by end of day.

## Your targets vs. what was achieved

| Target | Result | Verdict |
|--------|--------|---------|
| Profit factor ≥ 2.0 | 1.35 in-sample / **1.60 out-of-sample** (1.96 long-only) | **Not robustly met** |
| Max drawdown < 10% | ~1.8% in-sample, ~1.7% out-of-sample, MC p95 1.5% | **Met, with huge margin** |
| Beat the market | Beat SPY out-of-sample (+4.5 pts); trailed in the in-sample bull run | **Mixed** |

## The production book (5-minute bars, `trend` profile, market filter on)

Four trend/momentum strategies — Opening Range Breakout, VWAP-Trend pullback,
EMA pullback, MACD continuation — each gated to fire only when ADX confirms a
trend, and globally filtered to trade only in the direction of SPY's own trend.

|              | In-sample | Out-of-sample |
|--------------|-----------|---------------|
| Trades       | 275       | 123           |
| Win rate     | 53.5%     | 56.9%         |
| Profit factor| 1.35      | 1.60          |
| Return       | +5.09%    | +4.09%        |
| SPY same     | +13.13%   | −0.42%        |
| Alpha        | −8.04 pts | **+4.51 pts** |
| Max drawdown | 1.78%     | 1.73%         |
| Sharpe       | 4.05      | 5.59          |

Monte-Carlo (reshuffle the out-of-sample trade order, n=123): median DD 0.9%,
95th-pct 1.5%, worst 2.5%. The low drawdown is a property of the system, not of
one lucky trade ordering.

## What the experiments showed (and why this is the honest answer)

1. **No single strategy hits 2:1.** In isolation, the best strategies reach
   PF ~1.1–1.4. Anyone claiming a clean 2:1 from one intraday rule on liquid
   large-caps is almost certainly curve-fitting or ignoring costs.
2. **Regime is everything.** The 60-day 5m window was a strong SPY uptrend
   (+13%). Mean-reversion and short-fades bled in it — confirmed independently
   across ~200 parameter variants. Dropping them and trading *with* the trend is
   what produced a positive, market-beating book.
3. **The market-direction filter is the biggest honest lever.** Trading only in
   SPY's direction lifted profit factor and roughly halved drawdown — a single
   robust rule, not a fitted parameter.
4. **The strategies do NOT transfer to other timeframes.** Re-run on 2 years of
   hourly bars (parameters tuned for 5m), the book loses badly (PF 0.84,
   −38%, 40% DD). This is a deliberately included, sobering check: an intraday
   edge calibrated to one bar size is not a universal edge.
5. **Out-of-sample beat in-sample here only because SPY was flat in the OOS
   window.** Beating a −0.4% market by trading with low exposure is far easier
   than beating a +13% rip. Do not read the +4.5 OOS alpha as a promise.

## Bottom line

A realistic, look-ahead-free, cost-aware backtest of SPY/Mag7 day trading
produces a system with **excellent risk control (sub-2% drawdown)** and a
**modest, regime-dependent edge (PF ~1.3–1.6, ~1.96 long-only out-of-sample)**.
It can beat the market in flat/choppy periods and lags a strong bull market
(where buy-and-hold is hard to beat). A durable, statistically robust 2:1
profit factor was **not** achieved on free intraday data without overfitting,
and the report says so rather than dressing it up.

### What could plausibly push it further (honestly)
- **More and better data** — paid intraday history (years of 1m/5m) for real
  out-of-sample testing across multiple regimes; this is the single biggest gap.
- **Options structures on SPX** (defined-risk spreads) can engineer payoff
  asymmetry that lifts profit factor in ways share trading cannot.
- **Execution edge** — the slippage/spread assumptions dominate net P&L at this
  trade frequency; real fills and smarter order types matter more than another
  indicator.
- **Position sizing / vol targeting** across the uncorrelated strategies to
  smooth equity further (correlation tooling is built in: `daytrader correlation`).

Reproduce everything with:
```
python -m daytrader.evaluate                 # full scorecard (5m + 1h)
python -m daytrader walkforward --interval 5m --html report.html
```
