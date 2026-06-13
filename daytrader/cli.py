"""Command-line interface for the day-trading backtester.

Examples:
    python -m daytrader backtest --interval 5m
    python -m daytrader backtest --interval 1h --pessimistic --html report.html
    python -m daytrader walkforward --interval 5m --html report.html
    python -m daytrader strategies
"""
from __future__ import annotations

import argparse

from daytrader.backtest.engine import CostModel, EngineConfig
from daytrader.backtest.metrics import format_report
from daytrader.backtest.runner import run_backtest
from daytrader.backtest.validate import monte_carlo_dd, strategy_correlation, walk_forward
from daytrader.data import loader
from daytrader.portfolio.book import all_strategies, build_book
from daytrader.report.report import generate_html
from daytrader.risk.manager import RiskConfig, make_sizer


def _config(args) -> EngineConfig:
    cost = CostModel.pessimistic() if args.pessimistic else CostModel()
    return EngineConfig(
        starting_equity=args.equity,
        cost=cost,
        daily_loss_limit_pct=args.daily_loss_limit,
        max_concurrent_positions=args.max_positions,
        allow_short=not args.long_only,
        breakeven_at_r=args.breakeven_r,
        trail_atr_mult=args.trail_atr,
    )


def _sizer(args):
    return make_sizer(RiskConfig(
        risk_per_trade_pct=args.risk_per_trade,
        max_position_pct=args.max_position_pct,
    ))


def cmd_backtest(args):
    cfg = _config(args)
    sizer = _sizer(args)
    book = build_book(adx_threshold=args.adx, market_filter=args.market_filter)
    symbols = args.symbols.split(",") if args.symbols else loader.DEFAULT_UNIVERSE
    res = run_backtest(ensemble=book, symbols=symbols, interval=args.interval,
                       rng=args.range, config=cfg, sizer=sizer)
    print(format_report(res.metrics, f"Book / {args.interval} / {','.join(symbols)}"))
    if args.html:
        mc = monte_carlo_dd(res.trades, cfg.starting_equity)
        generate_html(res, mc=mc, path=args.html)
        print(f"\nHTML report -> {args.html}")


def cmd_walkforward(args):
    cfg = _config(args)
    sizer = _sizer(args)
    book = build_book(adx_threshold=args.adx, market_filter=args.market_filter)
    symbols = args.symbols.split(",") if args.symbols else loader.DEFAULT_UNIVERSE
    data = loader.load_many(symbols, interval=args.interval, rng=args.range)
    wf = walk_forward(book, data, config=cfg, sizer=sizer, oos_fraction=args.oos)
    print(format_report(wf["in_sample"], "IN-SAMPLE"))
    print()
    print(format_report(wf["out_of_sample"], "OUT-OF-SAMPLE"))
    mc = monte_carlo_dd(wf["oos_trades"], cfg.starting_equity)
    print(f"\nMonte-Carlo DD (OOS): p50 {mc['p50']:.1f}%  p95 {mc['p95']:.1f}%  "
          f"p99 {mc['p99']:.1f}%  worst {mc['worst']:.1f}%")
    if args.html:
        # full-window result for the chart + walk-forward table
        res = run_backtest(ensemble=book, symbols=symbols, interval=args.interval,
                           rng=args.range, config=cfg, sizer=sizer, data=data)
        generate_html(res, walk_forward=wf, mc=mc, path=args.html)
        print(f"HTML report -> {args.html}")


def cmd_strategies(args):
    book = all_strategies()
    print("Registered strategies:")
    for s in book.strategies:
        print(f"  {s.name:14s} {s!r}")


def cmd_correlation(args):
    cfg = _config(args)
    sizer = _sizer(args)
    book = all_strategies()
    symbols = args.symbols.split(",") if args.symbols else loader.DEFAULT_UNIVERSE
    res = run_backtest(ensemble=book, symbols=symbols, interval=args.interval,
                       rng=args.range, config=cfg, sizer=sizer)
    corr = strategy_correlation(res.trades)
    print("Daily-PnL correlation across strategies:\n")
    print(corr.round(2).to_string())


def build_parser():
    p = argparse.ArgumentParser(prog="daytrader", description="SPY/Mag7 intraday backtester")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--interval", default="5m", help="1m/5m/15m/30m/1h/1d")
        sp.add_argument("--range", default=None, help="yahoo range (default: max for interval)")
        sp.add_argument("--symbols", default=None, help="comma list (default SPY+Mag7)")
        sp.add_argument("--equity", type=float, default=100_000.0)
        sp.add_argument("--risk-per-trade", type=float, default=0.4, help="%% equity risked/trade")
        sp.add_argument("--max-position-pct", type=float, default=25.0)
        sp.add_argument("--max-positions", type=int, default=4)
        sp.add_argument("--daily-loss-limit", type=float, default=2.0, help="%% halt for the day")
        sp.add_argument("--adx", type=float, default=25.0, help="trend/range ADX threshold")
        sp.add_argument("--pessimistic", action="store_true", help="0.4%% slippage stress test")
        sp.add_argument("--long-only", action="store_true")
        sp.add_argument("--breakeven-r", type=float, default=0.0, help="move stop to breakeven after +N*R")
        sp.add_argument("--trail-atr", type=float, default=0.0, help="trail stop at N*ATR (0=off)")
        sp.add_argument("--market-filter", action="store_true",
                        help="only trade in the direction of SPY's trend")
        sp.add_argument("--html", default=None, help="write HTML report to this path")

    b = sub.add_parser("backtest"); common(b); b.set_defaults(func=cmd_backtest)
    w = sub.add_parser("walkforward"); common(w)
    w.add_argument("--oos", type=float, default=0.35, help="out-of-sample fraction")
    w.set_defaults(func=cmd_walkforward)
    c = sub.add_parser("correlation"); common(c); c.set_defaults(func=cmd_correlation)
    s = sub.add_parser("strategies"); s.set_defaults(func=cmd_strategies)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
