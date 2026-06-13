"""Comprehensive, honest evaluation of the production book.

Runs the regime-gated book with the SPY market-direction filter on:
  1. 60 days of 5-minute bars (high fidelity), walk-forward IS/OOS + Monte Carlo
  2. ~2 years of hourly bars (multi-regime, coarser fills)

Prints a single consolidated scorecard. This is the script behind the headline
numbers; run it with `python -m daytrader.evaluate`.
"""
from __future__ import annotations

from daytrader.backtest.engine import EngineConfig
from daytrader.backtest.metrics import format_report
from daytrader.backtest.runner import run_backtest
from daytrader.backtest.validate import monte_carlo_dd, walk_forward
from daytrader.data import loader
from daytrader.portfolio.book import build_book
from daytrader.risk.manager import RiskConfig, make_sizer


def evaluate(interval="5m", market_filter=True, risk=0.4, adx=25.0,
             daily_loss=2.0, oos=0.35, label=""):
    sizer = make_sizer(RiskConfig(risk_per_trade_pct=risk))
    cfg = EngineConfig(daily_loss_limit_pct=daily_loss)
    data = loader.load_many(loader.DEFAULT_UNIVERSE, interval=interval)
    book = build_book(adx_threshold=adx, market_filter=market_filter)

    res = run_backtest(ensemble=book, data=data, config=cfg, sizer=sizer, interval=interval)
    print(format_report(res.metrics, f"FULL WINDOW {label} ({interval}, market_filter={market_filter})"))

    wf = walk_forward(book, data, config=cfg, sizer=sizer, oos_fraction=oos)
    print()
    print(format_report(wf["in_sample"], f"IN-SAMPLE {label}"))
    print()
    print(format_report(wf["out_of_sample"], f"OUT-OF-SAMPLE {label}"))
    mc = monte_carlo_dd(wf["oos_trades"], cfg.starting_equity)
    print(f"\n  MonteCarlo OOS DD: p50 {mc['p50']:.1f}%  p95 {mc['p95']:.1f}%  "
          f"p99 {mc['p99']:.1f}%  worst {mc['worst']:.1f}%  (n={mc['n']})")
    return res, wf, mc


if __name__ == "__main__":
    print("\n############## 5-MINUTE (60 days, high fidelity) ##############\n")
    evaluate(interval="5m", market_filter=True, label="5m+filter")
    print("\n############## HOURLY (2 years, multi-regime) ##############\n")
    evaluate(interval="1h", market_filter=True, label="1h+filter")
