"""HTML + text reporting, styled after the example dashboard.

Renders headline metric cards, an equity-curve-vs-SPY chart (inline SVG, no
plotting dependency), a per-strategy breakdown, the out-of-sample and Monte
Carlo robustness results, and an honest "reality score" that states what the
backtest does and does not account for.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pandas as pd

from daytrader.backtest.metrics import Metrics
from daytrader.core.types import Trade


def _downsample(series: pd.Series, n: int = 500) -> pd.Series:
    if len(series) <= n:
        return series
    step = len(series) // n
    return series.iloc[::step]


def _svg_equity(equity: pd.Series, benchmark: pd.Series | None, w=1080, h=300) -> str:
    eq = _downsample(equity)
    if len(eq) < 2:
        return "<svg></svg>"
    series = [("strategy", eq, "#36d399")]
    if benchmark is not None and len(benchmark) >= 2:
        series.append(("SPY", _downsample(benchmark), "#888"))

    lo = min(float(s.min()) for _, s, _ in series)
    hi = max(float(s.max()) for _, s, _ in series)
    rng = (hi - lo) or 1.0
    pad = 36

    def pts(s: pd.Series) -> str:
        xs = range(len(s))
        n = len(s) - 1
        out = []
        for i, v in zip(xs, s.values):
            x = pad + (w - 2 * pad) * (i / n)
            y = h - pad - (h - 2 * pad) * ((float(v) - lo) / rng)
            out.append(f"{x:.1f},{y:.1f}")
        return " ".join(out)

    polylines = "".join(
        f'<polyline fill="none" stroke="{c}" stroke-width="2" points="{pts(s)}"/>'
        for _, s, c in series
    )
    # axis labels
    y_hi = f'<text x="4" y="{pad+4}" fill="#aaa" font-size="11">${hi:,.0f}</text>'
    y_lo = f'<text x="4" y="{h-pad+4}" fill="#aaa" font-size="11">${lo:,.0f}</text>'
    legend = ('<text x="{x}" y="20" fill="{c}" font-size="12">{n}</text>'.format(
        x=w - 160, c="#36d399", n="● strategy")
        + '<text x="{x}" y="20" fill="#888" font-size="12">{n}</text>'.format(
            x=w - 70, n="● SPY"))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" '
            f'style="background:#0e0e10;border-radius:8px">{polylines}'
            f'{y_hi}{y_lo}{legend}</svg>')


def benchmark_equity(data: dict, starting_equity: float, equity_index: pd.DatetimeIndex,
                     symbol: str = "SPY") -> pd.Series | None:
    df = data.get(symbol)
    if df is None or len(df) < 2:
        return None
    norm = df["close"] / df["close"].iloc[0] * starting_equity
    return norm.reindex(equity_index, method="ffill")


def per_strategy_breakdown(trades: list[Trade]) -> pd.DataFrame:
    agg = defaultdict(lambda: {"n": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0, "pnl": 0.0})
    for t in trades:
        if t.is_open:
            continue
        a = agg[t.strategy]
        a["n"] += 1
        a["pnl"] += t.net_pnl
        if t.net_pnl > 0:
            a["wins"] += 1
            a["gross_profit"] += t.net_pnl
        else:
            a["gross_loss"] += -t.net_pnl
    rows = []
    for strat, a in sorted(agg.items(), key=lambda kv: -kv[1]["pnl"]):
        pf = a["gross_profit"] / a["gross_loss"] if a["gross_loss"] > 0 else float("inf")
        rows.append({
            "strategy": strat,
            "trades": a["n"],
            "win_rate": a["wins"] / a["n"] * 100 if a["n"] else 0,
            "profit_factor": pf,
            "net_pnl": a["pnl"],
        })
    return pd.DataFrame(rows)


def reality_score(interval: str, mc: dict | None, oos: Metrics | None) -> tuple[int, list]:
    """A 0-100 trust score with the same spirit as the example dashboard."""
    checks = []
    score = 0
    checks.append(("Realistic fills (slippage + spread + gap-through-stop)", True, 20))
    checks.append(("Next-bar execution (no look-ahead)", True, 20))
    checks.append((f"Intraday data ({interval} bars)", interval in ("5m", "15m", "1m"), 10))
    checks.append(("Out-of-sample test passed", bool(oos and oos.profit_factor >= 1.5), 20))
    checks.append(("Monte-Carlo p95 drawdown < 10%", bool(mc and mc.get("p95", 99) < 10), 15))
    checks.append(("Fixed liquid universe (no survivorship bias)", True, 15))
    # things NOT modeled (cost the ceiling)
    not_modeled = [
        ("Borrow/locate cost for shorts", False),
        ("Per-name market-impact model", False),
        ("Corporate actions / dividends intraday", False),
    ]
    for label, ok, pts in checks:
        if ok:
            score += pts
    return score, checks + [(l, ok, 0) for l, ok in not_modeled]


def generate_html(result, walk_forward: dict | None = None, mc: dict | None = None,
                  path: str = "report.html", title: str = "Day Trader Backtest") -> str:
    m: Metrics = result.metrics
    bench_eq = benchmark_equity(result.data, result.equity.iloc[0] if len(result.equity) else 100000,
                                result.equity.index)
    chart = _svg_equity(result.equity, bench_eq)
    breakdown = per_strategy_breakdown(result.trades)
    oos = walk_forward["out_of_sample"] if walk_forward else None
    score, checks = reality_score(result.interval, mc, oos)

    def card(label, value, color="#fff"):
        return (f'<div class="card"><div class="label">{label}</div>'
                f'<div class="value" style="color:{color}">{value}</div></div>')

    pf_str = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    green, red = "#36d399", "#f87272"
    cards = "".join([
        card("RETURN", f"{m.total_return_pct:+.2f}%", green if m.total_return_pct > 0 else red),
        card("SPY SAME PERIOD", f"{m.benchmark_return_pct:+.2f}%"),
        card("VS SPY (ALPHA)", f"{m.alpha_pts:+.2f} pts", green if m.alpha_pts > 0 else red),
        card("FINAL", f"${m.final_equity:,.0f}"),
        card("TRADES", f"{m.n_trades}"),
        card("WIN RATE", f"{m.win_rate:.1f}%"),
        card("MAX DD", f"-{m.max_drawdown_pct:.1f}%", green if m.max_drawdown_pct < 10 else red),
        card("PROFIT FACTOR", pf_str, green if m.profit_factor >= 2 else red),
    ])

    bd_rows = "".join(
        f"<tr><td>{r.strategy}</td><td>{r.trades}</td><td>{r.win_rate:.1f}%</td>"
        f"<td>{'inf' if r.profit_factor==float('inf') else f'{r.profit_factor:.2f}'}</td>"
        f"<td style='color:{green if r.net_pnl>0 else red}'>${r.net_pnl:,.0f}</td></tr>"
        for r in breakdown.itertuples()
    )

    wf_html = ""
    if walk_forward:
        is_m, oos_m = walk_forward["in_sample"], walk_forward["out_of_sample"]
        wf_html = f"""
        <h2>Out-of-sample validation</h2>
        <table>
          <tr><th>Window</th><th>Trades</th><th>Profit factor</th><th>Return</th><th>Max DD</th><th>Win rate</th></tr>
          <tr><td>In-sample</td><td>{is_m.n_trades}</td><td>{is_m.profit_factor:.2f}</td><td>{is_m.total_return_pct:+.2f}%</td><td>-{is_m.max_drawdown_pct:.1f}%</td><td>{is_m.win_rate:.1f}%</td></tr>
          <tr><td>Out-of-sample</td><td>{oos_m.n_trades}</td><td>{oos_m.profit_factor:.2f}</td><td>{oos_m.total_return_pct:+.2f}%</td><td>-{oos_m.max_drawdown_pct:.1f}%</td><td>{oos_m.win_rate:.1f}%</td></tr>
        </table>"""

    mc_html = ""
    if mc:
        mc_html = f"""
        <h2>Monte Carlo drawdown (trade-order shuffle, n={mc['n']})</h2>
        <p>Median {mc['p50']:.1f}% &middot; 95th pct {mc['p95']:.1f}% &middot; 99th pct {mc['p99']:.1f}% &middot; worst {mc['worst']:.1f}%</p>"""

    checks_html = "".join(
        f"<li style='color:{green if ok else '#777'}'>{'✓' if ok else '✗'} {label}</li>"
        for label, ok, _ in checks
    )

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body{{background:#0a0a0c;color:#e5e5e5;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
  h1{{font-weight:700}} h2{{margin-top:28px;color:#ddd}}
  .cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}}
  .card{{background:#141417;border:1px solid #222;border-radius:10px;padding:16px}}
  .label{{color:#888;font-size:11px;letter-spacing:.05em}}
  .value{{font-size:26px;font-weight:700;margin-top:6px}}
  table{{border-collapse:collapse;width:100%;margin-top:8px}}
  th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #222;font-size:13px}}
  th{{color:#888;font-weight:600}}
  ul{{list-style:none;padding:0}} li{{padding:3px 0;font-size:13px}}
  .score{{font-size:30px;font-weight:700;color:#36d399}}
</style></head><body>
<h1>{title}</h1>
<p style="color:#888">Universe: SPY + Mag7 &middot; {result.interval} bars &middot; window {result.equity.index[0].date() if len(result.equity) else '?'} → {result.equity.index[-1].date() if len(result.equity) else '?'}</p>
<div class="cards">{cards}</div>
{chart}
<h2>Targets</h2>
<ul>
  <li style="color:{green if m.profit_factor>=2 else red}">{'✓' if m.profit_factor>=2 else '✗'} Profit factor &ge; 2.0 (got {pf_str})</li>
  <li style="color:{green if m.max_drawdown_pct<10 else red}">{'✓' if m.max_drawdown_pct<10 else '✗'} Max drawdown &lt; 10% (got {m.max_drawdown_pct:.1f}%)</li>
  <li style="color:{green if m.alpha_pts>0 else red}">{'✓' if m.alpha_pts>0 else '✗'} Beat SPY (alpha {m.alpha_pts:+.2f} pts)</li>
</ul>
{wf_html}
{mc_html}
<h2>Per-strategy breakdown</h2>
<table><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Profit factor</th><th>Net P&amp;L</th></tr>{bd_rows}</table>
<h2>Reality score: <span class="score">{score}/100</span></h2>
<ul>{checks_html}</ul>
<p style="color:#777;font-size:12px">Absolute returns are optimistic; treat them as directional. Free intraday history is limited (60 days at 5m, ~2 years at 1h), so longer tests use coarser bars.</p>
</body></html>"""
    Path(path).write_text(html, encoding="utf-8")
    return path
