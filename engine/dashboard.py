"""
Lightweight web dashboard for the Kraken BTC trading bot.

Serves a single-page monitoring UI on port 3737 with:
- Live BTC price and indicator signals
- Current position and P&L
- Trade history
- Equity curve chart
- Paper trading balance

Built with aiohttp (async, no extra frameworks needed).
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML template — single self-contained page with AI reasoning panel
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AlgoTrader v2.0.0</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --text: #e1e4eb; --muted: #8b8fa3; --green: #22c55e;
    --red: #ef4444; --blue: #3b82f6; --yellow: #eab308;
    --orange: #f97316; --purple: #a855f7;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px; max-width: 1400px; margin: 0 auto; }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
  .header h1 { font-size: 20px; font-weight: 600; }
  .header .badges { display: flex; gap: 8px; }
  .badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
  .mode-paper { background: var(--yellow); color: #000; }
  .mode-live { background: var(--red); color: #fff; }
  .badge-ai { background: var(--purple); color: #fff; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .card-wide { grid-column: span 2; }
  .card h2 { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }
  .big-number { font-size: 32px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .stat-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
  .stat-row:last-child { border: none; }
  .stat-label { color: var(--muted); }
  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral { color: var(--muted); }
  .signal-badge { display: inline-block; padding: 4px 10px; border-radius: 8px; font-size: 12px; font-weight: 700; }
  .signal-buy { background: rgba(34,197,94,0.15); color: var(--green); }
  .signal-sell { background: rgba(239,68,68,0.15); color: var(--red); }
  .signal-hold { background: rgba(139,143,163,0.15); color: var(--muted); }
  .ai-reasoning { background: rgba(168,85,247,0.08); border: 1px solid rgba(168,85,247,0.2); border-radius: 8px; padding: 12px; margin-top: 8px; font-size: 14px; line-height: 1.5; }
  .confidence-bar { height: 6px; background: var(--border); border-radius: 3px; margin-top: 8px; overflow: hidden; }
  .confidence-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
  .fear-greed-bar { height: 8px; background: linear-gradient(to right, #ef4444, #f97316, #eab308, #22c55e, #22c55e); border-radius: 4px; position: relative; margin: 8px 0; }
  .fear-greed-marker { position: absolute; top: -3px; width: 14px; height: 14px; background: white; border-radius: 50%; border: 2px solid var(--bg); transform: translateX(-50%); }
  .chart-container { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
  .chart-container h2 { font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 6px; border-bottom: 1px solid var(--border); }
  td { padding: 8px 6px; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
  .refresh-info { text-align: center; color: var(--muted); font-size: 12px; padding: 12px; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } .card-wide { grid-column: span 1; } .big-number { font-size: 24px; } }
</style>
</head>
<body>
<div class="header">
  <h1>AlgoTrader v2.0.0</h1>
  <div class="badges">
    <span class="badge badge-ai" id="aiLabel">AI</span>
    <span class="badge" id="modeLabel">--</span>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>BTC / USD</h2>
    <div class="big-number" id="price">--</div>
    <div style="margin-top:8px">
      <span class="signal-badge signal-hold" id="signalBadge">--</span>
      <span style="margin-left:8px;font-size:13px;color:var(--muted)" id="composite">--</span>
    </div>
  </div>
  <div class="card">
    <h2>Account</h2>
    <div class="big-number" id="equity">--</div>
    <div class="stat-row"><span class="stat-label">Cash</span><span id="cash">--</span></div>
    <div class="stat-row"><span class="stat-label">BTC Holdings</span><span id="btcQty">--</span></div>
    <div class="stat-row"><span class="stat-label">Total P&L</span><span id="totalPnl">--</span></div>
  </div>
  <div class="card">
    <h2>Position</h2>
    <div id="positionInfo">
      <div style="color:var(--muted);font-size:14px">No open position</div>
    </div>
  </div>
  <div class="card">
    <h2>Market Sentiment</h2>
    <div class="stat-row"><span class="stat-label">Fear & Greed</span><span id="fearGreed">--</span></div>
    <div class="fear-greed-bar"><div class="fear-greed-marker" id="fgMarker" style="left:50%"></div></div>
    <div class="stat-row"><span class="stat-label">News Sentiment</span><span id="newsSentiment">--</span></div>
    <div class="stat-row"><span class="stat-label">AI Outlook</span><span id="aiOutlook">--</span></div>
  </div>
</div>

<div class="grid" style="grid-template-columns: 1fr 1fr;">
  <div class="card">
    <h2>AI Decision</h2>
    <div style="display:flex;align-items:center;gap:12px">
      <span class="signal-badge signal-hold" id="aiAction" style="font-size:16px;padding:6px 16px">--</span>
      <div>
        <div style="font-size:13px;color:var(--muted)">Confidence</div>
        <div style="font-size:18px;font-weight:600" id="aiConfidence">--</div>
      </div>
      <div>
        <div style="font-size:13px;color:var(--muted)">Strategy</div>
        <div style="font-size:14px" id="aiStrategy">--</div>
      </div>
    </div>
    <div class="confidence-bar"><div class="confidence-fill" id="confFill" style="width:0;background:var(--muted)"></div></div>
    <div class="ai-reasoning" id="aiReasoning">Waiting for first scan...</div>
  </div>
  <div class="card">
    <h2>Indicators</h2>
    <div class="stat-row"><span class="stat-label">EMA Fast / Slow</span><span id="ema">--</span></div>
    <div class="stat-row"><span class="stat-label">EMA Crossover</span><span id="emaCross">--</span></div>
    <div class="stat-row"><span class="stat-label">RSI (14)</span><span id="rsi">--</span></div>
    <div class="stat-row"><span class="stat-label">BB Position</span><span id="bbPos">--</span></div>
  </div>
</div>

<div class="chart-container">
  <h2>Equity Curve</h2>
  <canvas id="equityChart" height="80"></canvas>
</div>

<div class="card">
  <h2>Recent Trades</h2>
  <table>
    <thead><tr><th>Time</th><th>Side</th><th>Qty</th><th>Price</th><th>Value</th><th>Fee</th></tr></thead>
    <tbody id="tradesBody"><tr><td colspan="6" style="color:var(--muted)">No trades yet</td></tr></tbody>
  </table>
</div>

<div class="refresh-info">Auto-refreshes every 10 seconds</div>

<script>
let equityChart = null;

async function fetchData() {
  try {
    const resp = await fetch('/api/status');
    const d = await resp.json();
    const sig = d.signals || {};

    // Mode
    const ml = document.getElementById('modeLabel');
    ml.textContent = d.mode;
    ml.className = 'badge mode-' + d.mode;

    // Price
    document.getElementById('price').textContent = d.price ? '$' + Number(d.price).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '--';

    // Signal badge
    const sb = document.getElementById('signalBadge');
    const rec = sig.recommendation || sig.ai_action || '--';
    sb.textContent = rec;
    sb.className = 'signal-badge ' + (rec.includes('BUY') ? 'signal-buy' : rec.includes('SELL') ? 'signal-sell' : 'signal-hold');
    document.getElementById('composite').textContent = sig.composite !== undefined ? 'Confidence: ' + (sig.composite * 100).toFixed(0) + '%' : '';

    // AI Decision panel
    const aiAction = sig.ai_action || '--';
    const aiEl = document.getElementById('aiAction');
    aiEl.textContent = aiAction;
    aiEl.className = 'signal-badge ' + (aiAction.includes('BUY') ? 'signal-buy' : aiAction.includes('SELL') ? 'signal-sell' : 'signal-hold');

    const conf = sig.ai_confidence || 0;
    document.getElementById('aiConfidence').textContent = (conf * 100).toFixed(0) + '%';
    const confFill = document.getElementById('confFill');
    confFill.style.width = (conf * 100) + '%';
    confFill.style.background = conf >= 0.6 ? (aiAction === 'BUY' ? 'var(--green)' : aiAction === 'SELL' ? 'var(--red)' : 'var(--muted)') : 'var(--muted)';

    document.getElementById('aiStrategy').textContent = sig.ai_strategy || '--';
    document.getElementById('aiReasoning').textContent = sig.ai_reasoning || 'Waiting for scan...';

    // Sentiment
    const fg = sig.fear_greed;
    if (fg !== undefined) {
      document.getElementById('fearGreed').textContent = fg + ' (' + (sig.fear_greed_label || '') + ')';
      document.getElementById('fgMarker').style.left = fg + '%';
    }
    const ns = sig.news_sentiment || '--';
    const nsEl = document.getElementById('newsSentiment');
    nsEl.textContent = ns.replace('_', ' ');
    nsEl.className = ns.includes('bullish') ? 'positive' : ns.includes('bearish') ? 'negative' : 'neutral';

    const outlook = sig.ai_outlook || '--';
    const olEl = document.getElementById('aiOutlook');
    olEl.textContent = outlook;
    olEl.className = outlook === 'bullish' ? 'positive' : outlook === 'bearish' ? 'negative' : 'neutral';

    // Account
    const bal = d.balance || {};
    document.getElementById('equity').textContent = bal.total_equity ? '$' + Number(bal.total_equity).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '--';
    document.getElementById('cash').textContent = bal.cash_usd ? '$' + Number(bal.cash_usd).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '--';
    document.getElementById('btcQty').textContent = bal.btc_quantity !== undefined ? Number(bal.btc_quantity).toFixed(6) + ' BTC' : '--';

    const startCap = d.starting_capital || 10000;
    const pnl = bal.total_equity ? bal.total_equity - startCap : 0;
    const pnlEl = document.getElementById('totalPnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    pnlEl.className = pnl >= 0 ? 'positive' : 'negative';

    // Indicators
    document.getElementById('ema').textContent = sig.ema_fast && sig.ema_slow ? '$' + sig.ema_fast.toLocaleString(undefined,{maximumFractionDigits:0}) + ' / $' + sig.ema_slow.toLocaleString(undefined,{maximumFractionDigits:0}) : '--';
    const cross = sig.ema_crossover || '--';
    const crossEl = document.getElementById('emaCross');
    crossEl.textContent = cross;
    crossEl.className = cross === 'bullish' ? 'positive' : cross === 'bearish' ? 'negative' : 'neutral';

    const rsiEl = document.getElementById('rsi');
    if (sig.rsi !== undefined) {
      rsiEl.textContent = sig.rsi.toFixed(1) + ' (' + sig.rsi_signal + ')';
      rsiEl.className = sig.rsi_signal === 'oversold' ? 'positive' : sig.rsi_signal === 'overbought' ? 'negative' : 'neutral';
    }
    document.getElementById('bbPos').textContent = sig.bb_position !== undefined ? (sig.bb_position * 100).toFixed(1) + '%' : '--';

    // Position
    const posDiv = document.getElementById('positionInfo');
    const pos = d.position;
    if (pos) {
      const upnl = (d.price - pos.entry_price) * pos.quantity;
      const upnlPct = ((d.price - pos.entry_price) / pos.entry_price * 100);
      posDiv.innerHTML =
        '<div class="stat-row"><span class="stat-label">Side</span><span>LONG</span></div>' +
        '<div class="stat-row"><span class="stat-label">Entry</span><span>$' + pos.entry_price.toLocaleString(undefined,{minimumFractionDigits:2}) + '</span></div>' +
        '<div class="stat-row"><span class="stat-label">Quantity</span><span>' + pos.quantity.toFixed(6) + ' BTC</span></div>' +
        '<div class="stat-row"><span class="stat-label">Stop / Target</span><span>$' + pos.stop_loss.toLocaleString(undefined,{maximumFractionDigits:0}) + ' / $' + pos.take_profit.toLocaleString(undefined,{maximumFractionDigits:0}) + '</span></div>' +
        '<div class="stat-row"><span class="stat-label">Unrealized P&L</span><span class="' + (upnl >= 0 ? 'positive' : 'negative') + '">' + (upnl >= 0 ? '+' : '') + '$' + upnl.toFixed(2) + ' (' + upnlPct.toFixed(2) + '%)</span></div>';
    } else {
      posDiv.innerHTML = '<div style="color:var(--muted);font-size:14px;padding:8px 0">No open position</div>';
    }

    // Trades
    const tbody = document.getElementById('tradesBody');
    if (d.trades && d.trades.length > 0) {
      tbody.innerHTML = d.trades.map(t => {
        const dt = new Date(t.timestamp * 1000);
        const ts = dt.toLocaleDateString() + ' ' + dt.toLocaleTimeString();
        const sideClass = t.side === 'buy' ? 'positive' : 'negative';
        return '<tr><td>' + ts + '</td><td class="' + sideClass + '">' + t.side.toUpperCase() + '</td><td>' + Number(t.quantity).toFixed(6) + '</td><td>$' + Number(t.price).toLocaleString(undefined,{minimumFractionDigits:2}) + '</td><td>$' + Number(t.value).toLocaleString(undefined,{minimumFractionDigits:2}) + '</td><td>$' + Number(t.fee).toFixed(2) + '</td></tr>';
      }).join('');
    }

    // Equity chart
    if (d.equity_history && d.equity_history.length > 1) {
      const labels = d.equity_history.map(p => {
        const dt = new Date(p.timestamp * 1000);
        return dt.toLocaleDateString() + ' ' + dt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      });
      const data = d.equity_history.map(p => p.equity);
      if (equityChart) {
        equityChart.data.labels = labels;
        equityChart.data.datasets[0].data = data;
        equityChart.update('none');
      } else {
        equityChart = new Chart(document.getElementById('equityChart'), {
          type: 'line',
          data: { labels, datasets: [{ label: 'Equity', data, borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }] },
          options: { responsive: true, plugins: { legend: { display: false } }, scales: {
            x: { ticks: { color: '#8b8fa3', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#2a2d3a' } },
            y: { ticks: { color: '#8b8fa3', callback: v => '$' + v.toLocaleString() }, grid: { color: '#2a2d3a' } }
          }}
        });
      }
    }
  } catch (e) { console.error('Fetch error:', e); }
}

fetchData();
setInterval(fetchData, 10000);
</script>
</body>
</html>"""


class Dashboard:
    """Async web dashboard served alongside the trading bot."""

    def __init__(self, db, paper_trader=None, config=None):
        self.db = db
        self.paper_trader = paper_trader
        self.config = config
        self._last_signals = {}
        self._last_price = 0
        self.app = web.Application()
        self.app.router.add_get('/', self._index)
        self.app.router.add_get('/api/status', self._api_status)

    def update_signals(self, price: float, signals_dict: dict):
        """Called by the bot after each scan to update displayed signals."""
        self._last_price = price
        self._last_signals = signals_dict

    async def _index(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

    async def _api_status(self, request):
        """JSON endpoint with all dashboard data."""
        # Balance
        balance = {}
        starting_capital = 10000
        if self.paper_trader:
            bal = self.paper_trader.get_balance()
            balance = {
                "cash_usd": bal.cash_usd,
                "btc_quantity": bal.btc_quantity,
                "total_equity": bal.total_equity,
            }
            starting_capital = self.config.paper.starting_capital if self.config else 10000

        # Position
        position = None
        pos = self.db.get_open_position()
        if pos:
            position = {
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "entry_time": pos.entry_time,
            }

        # Trades
        trades = self.db.get_trades(limit=20)
        trades_data = [
            {
                "timestamp": t.timestamp,
                "side": t.side,
                "price": t.price,
                "quantity": t.quantity,
                "value": t.value,
                "fee": t.fee,
                "status": t.status,
            }
            for t in trades
        ]

        # Equity history
        equity_history = self.db.get_equity_history(limit=500)

        data = {
            "mode": self.config.mode if self.config else "paper",
            "price": self._last_price,
            "signals": self._last_signals,
            "balance": balance,
            "starting_capital": starting_capital,
            "position": position,
            "trades": trades_data,
            "equity_history": equity_history,
        }

        return web.json_response(data)

    async def start(self, host: str = "0.0.0.0", port: int = 3737):
        """Start the web server (non-blocking)."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Dashboard running at http://{host}:{port}")
        return runner
