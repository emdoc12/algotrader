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

import httpx
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
<title>AlgoTrader v2.6.0</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4" async></script>
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
  .toggle-wrap { display: flex; align-items: center; gap: 8px; }
  .toggle-label { font-size: 12px; font-weight: 600; text-transform: uppercase; }
  .toggle-label-paper { color: var(--yellow); }
  .toggle-label-live { color: var(--red); }
  .toggle { position: relative; width: 44px; height: 24px; cursor: pointer; }
  .toggle input { opacity: 0; width: 0; height: 0; }
  .toggle-track { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: var(--yellow); border-radius: 12px; transition: background 0.3s; }
  .toggle input:checked + .toggle-track { background: var(--red); }
  .toggle-thumb { position: absolute; top: 2px; left: 2px; width: 20px; height: 20px; background: white; border-radius: 50%; transition: transform 0.3s; }
  .toggle input:checked ~ .toggle-thumb { transform: translateX(20px); }
  .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.active { display: flex; }
  .modal { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 24px; max-width: 400px; width: 90%; }
  .modal h3 { font-size: 18px; margin-bottom: 12px; }
  .modal p { font-size: 14px; color: var(--muted); margin-bottom: 16px; line-height: 1.5; }
  .modal .warning { color: var(--red); font-weight: 600; }
  .modal-buttons { display: flex; gap: 8px; justify-content: flex-end; }
  .btn { padding: 8px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; }
  .btn-cancel { background: var(--border); color: var(--text); }
  .btn-danger { background: var(--red); color: white; }
  .btn-safe { background: var(--yellow); color: #000; }
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

  /* Chat panel */
  .chat-panel { margin-top: 16px; }
  .chat-messages { max-height: 400px; overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 8px; }
  .chat-msg { padding: 10px 14px; border-radius: 12px; max-width: 85%; font-size: 14px; line-height: 1.5; word-wrap: break-word; }
  .chat-msg-user { background: var(--blue); color: white; align-self: flex-end; border-bottom-right-radius: 4px; }
  .chat-msg-ai { background: var(--border); color: var(--text); align-self: flex-start; border-bottom-left-radius: 4px; }
  .chat-msg-time { font-size: 10px; opacity: 0.6; margin-top: 4px; }
  .chat-input-wrap { display: flex; gap: 8px; margin-top: 12px; }
  .chat-input { flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; color: var(--text); font-size: 14px; outline: none; resize: none; font-family: inherit; }
  .chat-input:focus { border-color: var(--purple); }
  .chat-input::placeholder { color: var(--muted); }
  .btn-send { background: var(--purple); color: white; border: none; border-radius: 8px; padding: 10px 20px; font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .btn-send:hover { opacity: 0.9; }
  .btn-send:disabled { opacity: 0.5; cursor: not-allowed; }
  .chat-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
  .btn-clear { background: transparent; border: 1px solid var(--border); color: var(--muted); border-radius: 6px; padding: 4px 12px; font-size: 11px; cursor: pointer; }
  .btn-clear:hover { border-color: var(--red); color: var(--red); }
  .typing-indicator { color: var(--purple); font-size: 13px; padding: 4px 0; }

  /* Goals panel */
  .goals-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .goal-field { display: flex; flex-direction: column; gap: 4px; }
  .goal-field label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .goal-field input, .goal-field textarea { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; color: var(--text); font-size: 14px; outline: none; font-family: inherit; }
  .goal-field input:focus, .goal-field textarea:focus { border-color: var(--purple); }
  .goal-field textarea { resize: vertical; min-height: 60px; grid-column: span 2; }
  .goal-field-wide { grid-column: span 2; }
  .goals-actions { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; }
  .btn-save-goals { background: var(--green); color: white; border: none; border-radius: 8px; padding: 8px 20px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .btn-save-goals:hover { opacity: 0.9; }
  .goals-status { font-size: 13px; color: var(--green); }
  .progress-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .progress-row:last-child { border: none; }
  .progress-label { font-size: 13px; color: var(--muted); }
  .progress-bar-wrap { width: 120px; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
  .progress-bar-fill { height: 100%; border-radius: 3px; background: var(--green); transition: width 0.5s; }
  .progress-val { font-size: 13px; font-weight: 600; min-width: 80px; text-align: right; }

  /* Tabs for bottom section */
  .tab-bar { display: flex; gap: 4px; margin-bottom: 12px; }
  .tab-btn { background: transparent; border: 1px solid var(--border); color: var(--muted); border-radius: 8px 8px 0 0; padding: 8px 20px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
  .tab-btn.active { background: var(--card); color: var(--purple); border-color: var(--purple); border-bottom-color: var(--card); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } .card-wide { grid-column: span 1; } .big-number { font-size: 24px; } .goals-grid { grid-template-columns: 1fr; } .goal-field-wide { grid-column: span 1; } }
</style>
</head>
<body>
<div class="header">
  <h1>AlgoTrader v2.6.0</h1>
  <div class="badges">
    <span class="badge badge-ai" id="aiLabel">AI</span>
    <div class="toggle-wrap">
      <span class="toggle-label toggle-label-paper">Paper</span>
      <label class="toggle">
        <input type="checkbox" id="modeToggle" onchange="handleModeToggle(this)">
        <div class="toggle-track"></div>
        <div class="toggle-thumb"></div>
      </label>
      <span class="toggle-label toggle-label-live">Live</span>
    </div>
  </div>
</div>

<div class="modal-overlay" id="liveModal">
  <div class="modal">
    <h3 class="warning">Switch to Live Trading?</h3>
    <p>This will use <strong>real money</strong> from your Kraken account. Claude AI will place actual buy/sell orders on your behalf.</p>
    <p>Make sure you have reviewed the bot's paper trading performance before going live.</p>
    <div class="modal-buttons">
      <button class="btn btn-cancel" onclick="cancelModeSwitch()">Cancel</button>
      <button class="btn btn-danger" onclick="confirmModeSwitch('live')">Go Live</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="paperModal">
  <div class="modal">
    <h3>Switch to Paper Trading?</h3>
    <p>This will stop placing real orders. The bot will continue scanning but only simulate trades.</p>
    <p>Any open live positions will <strong>not</strong> be automatically closed.</p>
    <div class="modal-buttons">
      <button class="btn btn-cancel" onclick="cancelModeSwitch()">Cancel</button>
      <button class="btn btn-safe" onclick="confirmModeSwitch('paper')">Switch to Paper</button>
    </div>
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
    <h2>Open Positions</h2>
    <div id="positionInfo">
      <div style="color:var(--muted);font-size:14px">No open positions</div>
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

<div class="card" style="margin-bottom:16px">
  <h2>Market Overview <span style="font-size:11px;color:var(--muted);text-transform:none;letter-spacing:0" id="coinCount"></span></h2>
  <div style="display:flex;gap:16px;margin-bottom:8px;flex-wrap:wrap">
    <div><span style="font-size:12px;color:var(--muted)">Momentum</span><br><span style="font-size:14px;font-weight:600" id="mktMomentum">--</span></div>
    <div><span style="font-size:12px;color:var(--muted)">Rotation</span><br><span style="font-size:14px;font-weight:600" id="mktRotation">--</span></div>
    <div><span style="font-size:12px;color:var(--muted)">Top Movers</span><br><span style="font-size:14px;font-weight:600" id="mktMovers">--</span></div>
  </div>
  <div style="overflow-x:auto">
    <table>
      <thead><tr><th>Coin</th><th>Price</th><th>1h</th><th>24h</th><th>RSI</th><th>Signal</th></tr></thead>
      <tbody id="coinTableBody"><tr><td colspan="6" style="color:var(--muted)">Scanning market...</td></tr></tbody>
    </table>
  </div>
</div>

<div class="chart-container">
  <h2>Equity Curve</h2>
  <canvas id="equityChart" height="80"></canvas>
</div>

<div class="card" style="padding:0;overflow:hidden">
  <div style="padding:16px 16px 0 16px">
    <div class="tab-bar">
      <button class="tab-btn active" onclick="switchTab('trades', this)">Trades</button>
      <button class="tab-btn" onclick="switchTab('alltrades', this)">All Trades</button>
      <button class="tab-btn" onclick="switchTab('chat', this)">Chat with Claude</button>
      <button class="tab-btn" onclick="switchTab('goals', this)">Goals</button>
    </div>
  </div>

  <!-- Trades Tab (last 3 days) -->
  <div class="tab-content active" id="tab-trades" style="padding:0 16px 16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <span style="font-size:12px;color:var(--muted)">Last 3 days</span>
    </div>
    <table>
      <thead><tr><th>Time</th><th>Coin</th><th>Side</th><th>Qty</th><th>Price</th><th>Value</th><th>Fee</th><th>P&L</th></tr></thead>
      <tbody id="tradesBody"><tr><td colspan="8" style="color:var(--muted)">No trades yet</td></tr></tbody>
    </table>
  </div>

  <!-- All Trades Tab -->
  <div class="tab-content" id="tab-alltrades" style="padding:0 16px 16px">
    <div id="allTradesSummary" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:12px"></div>
    <table>
      <thead><tr><th>Time</th><th>Coin</th><th>Side</th><th>Qty</th><th>Price</th><th>Value</th><th>Fee</th><th>P&L</th><th>Running</th></tr></thead>
      <tbody id="allTradesBody"><tr><td colspan="9" style="color:var(--muted)">Loading...</td></tr></tbody>
    </table>
  </div>

  <!-- Chat Tab -->
  <div class="tab-content" id="tab-chat" style="padding:0 16px 16px">
    <div class="chat-panel">
      <div class="chat-messages" id="chatMessages">
        <div class="chat-msg chat-msg-ai">Hey! I'm Claude, your AI trading strategist. Ask me about my current strategy, market outlook, or anything about the bot's performance.</div>
      </div>
      <div id="typingIndicator" class="typing-indicator" style="display:none">Claude is thinking...</div>
      <div class="chat-input-wrap">
        <textarea class="chat-input" id="chatInput" placeholder="Ask Claude about strategy, market outlook, goals..." rows="1" onkeydown="handleChatKey(event)"></textarea>
        <button class="btn-send" id="chatSendBtn" onclick="sendChat()">Send</button>
      </div>
      <div class="chat-actions">
        <button class="btn-clear" onclick="clearChat()">Clear history</button>
      </div>
    </div>
  </div>

  <!-- Goals Tab -->
  <div class="tab-content" id="tab-goals" style="padding:0 16px 16px">
    <div style="margin-bottom:16px">
      <h3 style="font-size:14px;color:var(--text);margin-bottom:4px">Progress</h3>
      <div id="goalsProgress">
        <div style="color:var(--muted);font-size:13px;padding:8px 0">Set targets below to track progress</div>
      </div>
    </div>
    <div class="goals-grid">
      <div class="goal-field">
        <label>Weekly USD Profit Target ($)</label>
        <input type="number" id="goalWeeklyProfit" step="10" min="0" placeholder="e.g. 500">
      </div>
      <div class="goal-field">
        <label>Monthly USD Profit Target ($)</label>
        <input type="number" id="goalMonthlyProfit" step="50" min="0" placeholder="e.g. 2000">
      </div>
      <div class="goal-field">
        <label>Weekly BTC Accumulation Target</label>
        <input type="number" id="goalWeeklyBtc" step="0.001" min="0" placeholder="e.g. 0.01">
      </div>
      <div class="goal-field">
        <label>Monthly BTC Accumulation Target</label>
        <input type="number" id="goalMonthlyBtc" step="0.01" min="0" placeholder="e.g. 0.05">
      </div>
      <div class="goal-field goal-field-wide">
        <label>Notes / Strategy Instructions for Claude</label>
        <textarea id="goalNotes" placeholder="e.g. Focus on accumulating BTC during dips, be more aggressive when Fear &amp; Greed is below 30..."></textarea>
      </div>
    </div>
    <div class="goals-actions">
      <span class="goals-status" id="goalsStatus"></span>
      <button class="btn-save-goals" onclick="saveGoals()">Save Goals</button>
    </div>
  </div>
</div>

<div class="refresh-info">Auto-refreshes every 10 seconds</div>

<script>
let equityChart = null;

async function fetchData() {
  try {
    const resp = await fetch('/api/status');
    const d = await resp.json();
    const sig = d.signals || {};

    // Mode — sync toggle
    document.getElementById('modeToggle').checked = (d.mode === 'live');

    // Price
    document.getElementById('price').textContent = d.price ? '$' + Number(d.price).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : '--';

    // Signal badge
    const sb = document.getElementById('signalBadge');
    const rec = sig.recommendation || sig.ai_action || '--';
    sb.textContent = rec;
    sb.className = 'signal-badge ' + (rec.includes('BUY') ? 'signal-buy' : rec.includes('SELL') ? 'signal-sell' : 'signal-hold');
    document.getElementById('composite').textContent = sig.composite !== undefined ? 'Confidence: ' + (sig.composite * 100).toFixed(0) + '%' : '';

    // AI Decision panel
    const aiSymbol = sig.ai_symbol || 'BTC/USD';
    const aiAction = sig.ai_action ? sig.ai_action + ' ' + aiSymbol : '--';
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

    // Market overview
    if (sig.coins_scanned) {
      document.getElementById('coinCount').textContent = '(' + sig.coins_scanned + ' coins)';
      const mm = sig.market_momentum || '--';
      const mmEl = document.getElementById('mktMomentum');
      mmEl.textContent = mm.replace('_', ' ');
      mmEl.className = mm === 'risk_on' ? 'positive' : mm === 'risk_off' ? 'negative' : 'neutral';
      document.getElementById('mktRotation').textContent = (sig.sector_rotation || '--').replace(/_/g, ' ');
      document.getElementById('mktMovers').textContent = sig.top_movers || '--';

      if (sig.coin_data && sig.coin_data.length > 0) {
        const tbody = document.getElementById('coinTableBody');
        tbody.innerHTML = sig.coin_data.map(function(c) {
          const ch1 = c.change_1h || 0;
          const ch24 = c.change_24h || 0;
          const rsi = c.rsi || 0;
          const mom = c.momentum || 0;
          const sigClass = mom > 0.3 ? 'signal-buy' : mom < -0.3 ? 'signal-sell' : 'signal-hold';
          const sigText = mom > 0.3 ? 'Bullish' : mom < -0.3 ? 'Bearish' : 'Neutral';
          const name = c.symbol.replace('USD', '').replace('XBT', 'BTC');
          return '<tr>' +
            '<td style="font-weight:600">' + name + '</td>' +
            '<td>$' + Number(c.price).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) + '</td>' +
            '<td class="' + (ch1 >= 0 ? 'positive' : 'negative') + '">' + (ch1 >= 0 ? '+' : '') + ch1.toFixed(2) + '%</td>' +
            '<td class="' + (ch24 >= 0 ? 'positive' : 'negative') + '">' + (ch24 >= 0 ? '+' : '') + ch24.toFixed(2) + '%</td>' +
            '<td>' + rsi.toFixed(0) + '</td>' +
            '<td><span class="signal-badge ' + sigClass + '">' + sigText + '</span></td>' +
            '</tr>';
        }).join('');
      }
    }

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

    // Positions (multi-coin)
    const posDiv = document.getElementById('positionInfo');
    const allPos = d.positions || (d.position ? [d.position] : []);
    if (allPos.length > 0) {
      posDiv.innerHTML = allPos.map(pos => {
        const upnl = pos.unrealized_pnl || 0;
        const upnlPct = pos.entry_price > 0 ? ((upnl / (pos.entry_price * pos.quantity)) * 100) : 0;
        const sym = pos.symbol || 'BTC/USD';
        const coin = sym.replace('/USD', '');
        return '<div style="margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid var(--border)">' +
          '<div style="font-weight:600;margin-bottom:4px;color:var(--blue)">' + sym + '</div>' +
          '<div class="stat-row"><span class="stat-label">Entry</span><span>$' + pos.entry_price.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:4}) + '</span></div>' +
          '<div class="stat-row"><span class="stat-label">Quantity</span><span>' + pos.quantity.toFixed(6) + ' ' + coin + '</span></div>' +
          '<div class="stat-row"><span class="stat-label">Stop / Target</span><span>$' + pos.stop_loss.toLocaleString(undefined,{maximumFractionDigits:2}) + ' / $' + pos.take_profit.toLocaleString(undefined,{maximumFractionDigits:2}) + '</span></div>' +
          '<div class="stat-row"><span class="stat-label">Unrealized P&L</span><span class="' + (upnl >= 0 ? 'positive' : 'negative') + '">' + (upnl >= 0 ? '+' : '') + '$' + upnl.toFixed(2) + ' (' + upnlPct.toFixed(2) + '%)</span></div>' +
          '</div>';
      }).join('');
    } else {
      posDiv.innerHTML = '<div style="color:var(--muted);font-size:14px;padding:8px 0">No open positions</div>';
    }

    // Trades (last 3 days with P&L)
    const tbody = document.getElementById('tradesBody');
    if (d.trades && d.trades.length > 0) {
      tbody.innerHTML = d.trades.map(t => {
        const dt = new Date(t.timestamp * 1000);
        const ts = dt.toLocaleDateString() + ' ' + dt.toLocaleTimeString();
        const sideClass = t.side === 'buy' ? 'positive' : 'negative';
        const coin = (t.symbol || 'BTC/USD').replace('/USD', '');
        let pnlCell = '<td style="color:var(--muted)">—</td>';
        if (t.pnl_dollar !== null && t.pnl_dollar !== undefined) {
          const pnlClass = t.pnl_dollar >= 0 ? 'positive' : 'negative';
          const sign = t.pnl_dollar >= 0 ? '+' : '';
          pnlCell = '<td class="' + pnlClass + '">' + sign + '$' + t.pnl_dollar.toFixed(2) + ' (' + sign + t.pnl_pct.toFixed(1) + '%)</td>';
        }
        return '<tr><td>' + ts + '</td><td>' + coin + '</td><td class="' + sideClass + '">' + t.side.toUpperCase() + '</td><td>' + Number(t.quantity).toFixed(6) + '</td><td>$' + Number(t.price).toLocaleString(undefined,{minimumFractionDigits:2}) + '</td><td>$' + Number(t.value).toLocaleString(undefined,{minimumFractionDigits:2}) + '</td><td>$' + Number(t.fee).toFixed(2) + '</td>' + pnlCell + '</tr>';
      }).join('');
    }

    // Equity chart (only if Chart.js loaded)
    if (typeof Chart !== 'undefined' && d.equity_history && d.equity_history.length > 1) {
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
  } catch (e) {
    console.error('Fetch error:', e);
    document.querySelector('.refresh-info').textContent = 'Error: ' + e.message + ' — retrying in 10s';
    document.querySelector('.refresh-info').style.color = 'var(--red)';
  }
}

fetchData();
setInterval(fetchData, 10000);

// --- All Trades tab ---
let allTradesLoaded = false;
async function loadAllTrades() {
  if (allTradesLoaded) return;
  try {
    const r = await fetch('/api/trades/all');
    const d = await r.json();
    if (d.error) { console.error(d.error); return; }

    // Summary cards
    const s = d.summary;
    const sumDiv = document.getElementById('allTradesSummary');
    const pnlClass = s.total_pnl >= 0 ? 'var(--green)' : 'var(--red)';
    const pnlSign = s.total_pnl >= 0 ? '+' : '';
    sumDiv.innerHTML =
      '<div style="background:var(--bg);border-radius:8px;padding:10px;text-align:center"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Total P&L</div><div style="font-size:20px;font-weight:700;color:' + pnlClass + '">' + pnlSign + '$' + s.total_pnl.toFixed(2) + '</div></div>' +
      '<div style="background:var(--bg);border-radius:8px;padding:10px;text-align:center"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Win Rate</div><div style="font-size:20px;font-weight:700">' + s.win_rate.toFixed(0) + '%</div><div style="font-size:11px;color:var(--muted)">' + s.winners + 'W / ' + s.losers + 'L</div></div>' +
      '<div style="background:var(--bg);border-radius:8px;padding:10px;text-align:center"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Avg Win</div><div style="font-size:20px;font-weight:700;color:var(--green)">+$' + s.avg_win.toFixed(2) + '</div></div>' +
      '<div style="background:var(--bg);border-radius:8px;padding:10px;text-align:center"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Avg Loss</div><div style="font-size:20px;font-weight:700;color:var(--red)">$' + s.avg_loss.toFixed(2) + '</div></div>' +
      '<div style="background:var(--bg);border-radius:8px;padding:10px;text-align:center"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Total Trades</div><div style="font-size:20px;font-weight:700">' + s.total_trades + '</div><div style="font-size:11px;color:var(--muted)">' + s.total_buys + ' buys / ' + s.total_sells + ' sells</div></div>' +
      '<div style="background:var(--bg);border-radius:8px;padding:10px;text-align:center"><div style="font-size:11px;color:var(--muted);text-transform:uppercase">Total Fees</div><div style="font-size:20px;font-weight:700;color:var(--orange)">$' + s.total_fees.toFixed(2) + '</div></div>';

    // Trade rows
    const tbody = document.getElementById('allTradesBody');
    if (d.trades && d.trades.length > 0) {
      tbody.innerHTML = d.trades.map(t => {
        const dt = new Date(t.timestamp * 1000);
        const ts = dt.toLocaleDateString() + ' ' + dt.toLocaleTimeString();
        const sideClass = t.side === 'buy' ? 'positive' : 'negative';
        let pnlCell = '<td style="color:var(--muted)">—</td>';
        if (t.pnl_dollar !== null && t.pnl_dollar !== undefined) {
          const pc = t.pnl_dollar >= 0 ? 'positive' : 'negative';
          const sg = t.pnl_dollar >= 0 ? '+' : '';
          pnlCell = '<td class="' + pc + '">' + sg + '$' + t.pnl_dollar.toFixed(2) + ' (' + sg + t.pnl_pct.toFixed(1) + '%)</td>';
        }
        const runClass = t.running_pnl >= 0 ? 'positive' : 'negative';
        const runSign = t.running_pnl >= 0 ? '+' : '';
        const runCell = '<td class="' + runClass + '">' + runSign + '$' + t.running_pnl.toFixed(2) + '</td>';
        const coin = (t.symbol || 'BTC/USD').replace('/USD', '');
        return '<tr><td>' + ts + '</td><td>' + coin + '</td><td class="' + sideClass + '">' + t.side.toUpperCase() + '</td><td>' + Number(t.quantity).toFixed(6) + '</td><td>$' + Number(t.price).toLocaleString(undefined,{minimumFractionDigits:2}) + '</td><td>$' + Number(t.value).toLocaleString(undefined,{minimumFractionDigits:2}) + '</td><td>$' + Number(t.fee).toFixed(2) + '</td>' + pnlCell + runCell + '</tr>';
      }).join('');
    } else {
      tbody.innerHTML = '<tr><td colspan="9" style="color:var(--muted)">No trades yet</td></tr>';
    }
    allTradesLoaded = true;
  } catch(e) {
    console.error('Failed to load all trades:', e);
  }
}

// --- Mode toggle ---
let pendingMode = null;

function handleModeToggle(el) {
  el.checked = !el.checked; // revert — we'll set it after confirmation
  const targetMode = el.checked ? 'paper' : 'live'; // inverted because we reverted
  pendingMode = targetMode === 'paper' ? 'live' : 'paper';
  // Actually: if currently unchecked (paper) and user clicked, they want live
  const currentMode = document.getElementById('modeToggle').checked ? 'live' : 'paper';
  const wantMode = currentMode === 'paper' ? 'live' : 'paper';
  pendingMode = wantMode;

  if (wantMode === 'live') {
    document.getElementById('liveModal').classList.add('active');
  } else {
    document.getElementById('paperModal').classList.add('active');
  }
}

function cancelModeSwitch() {
  document.getElementById('liveModal').classList.remove('active');
  document.getElementById('paperModal').classList.remove('active');
  pendingMode = null;
}

async function confirmModeSwitch(mode) {
  document.getElementById('liveModal').classList.remove('active');
  document.getElementById('paperModal').classList.remove('active');
  try {
    const resp = await fetch('/api/mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: mode})
    });
    const data = await resp.json();
    if (data.error) {
      alert('Failed to switch mode: ' + data.error);
    } else {
      document.getElementById('modeToggle').checked = (mode === 'live');
      fetchData();
    }
  } catch(e) {
    alert('Failed to switch mode: ' + e);
  }
  pendingMode = null;
}

// --- Tabs ---
function switchTab(tab, btn) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  btn.classList.add('active');
  if (tab === 'alltrades') loadAllTrades();
  if (tab === 'chat') loadChatHistory();
  if (tab === 'goals') loadGoals();
}

// --- Chat ---
let chatLoaded = false;

async function loadChatHistory() {
  if (chatLoaded) return;
  try {
    const resp = await fetch('/api/chat/history');
    const d = await resp.json();
    if (d.messages && d.messages.length > 0) {
      const container = document.getElementById('chatMessages');
      container.innerHTML = '';
      d.messages.forEach(m => appendChatMsg(m.role, m.message, m.timestamp));
      scrollChat();
    }
    chatLoaded = true;
  } catch(e) { console.error('Chat history load failed:', e); }
}

function appendChatMsg(role, text, ts) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + (role === 'user' ? 'chat-msg-user' : 'chat-msg-ai');
  // Simple markdown-ish: bold, line breaks
  let html = text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  html = html.replace(/[*][*](.*?)[*][*]/g, '<strong>$1</strong>');
  html = html.replace(/\\n/g, '<br>');
  if (ts) {
    const dt = new Date(ts * 1000);
    html += '<div class="chat-msg-time">' + dt.toLocaleTimeString() + '</div>';
  }
  div.innerHTML = html;
  container.appendChild(div);
}

function scrollChat() {
  const c = document.getElementById('chatMessages');
  c.scrollTop = c.scrollHeight;
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
}

async function sendChat() {
  const input = document.getElementById('chatInput');
  const msg = input.value.trim();
  if (!msg) return;

  const btn = document.getElementById('chatSendBtn');
  btn.disabled = true;
  input.value = '';

  appendChatMsg('user', msg);
  scrollChat();

  document.getElementById('typingIndicator').style.display = 'block';

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    const d = await resp.json();
    document.getElementById('typingIndicator').style.display = 'none';

    if (d.error) {
      appendChatMsg('assistant', 'Error: ' + d.error);
    } else {
      appendChatMsg('assistant', d.reply);
    }
    scrollChat();
  } catch(e) {
    document.getElementById('typingIndicator').style.display = 'none';
    appendChatMsg('assistant', 'Failed to reach Claude: ' + e);
    scrollChat();
  }
  btn.disabled = false;
  input.focus();
}

async function clearChat() {
  if (!confirm('Clear all chat history?')) return;
  try {
    await fetch('/api/chat/clear', {method: 'POST'});
    const container = document.getElementById('chatMessages');
    container.innerHTML = '<div class="chat-msg chat-msg-ai">Chat cleared. Ask me anything about the bot, strategy, or market!</div>';
    chatLoaded = false;
  } catch(e) { console.error('Clear failed:', e); }
}

// --- Goals ---
let goalsLoaded = false;

async function loadGoals() {
  try {
    const resp = await fetch('/api/goals');
    const d = await resp.json();
    const g = d.goals || {};

    document.getElementById('goalWeeklyProfit').value = g.weekly_profit_target || '';
    document.getElementById('goalMonthlyProfit').value = g.monthly_profit_target || '';
    document.getElementById('goalWeeklyBtc').value = g.weekly_btc_target || '';
    document.getElementById('goalMonthlyBtc').value = g.monthly_btc_target || '';
    document.getElementById('goalNotes').value = g.notes || '';

    // Progress display
    const wp = d.weekly_progress || {};
    const mp = d.monthly_progress || {};
    const progDiv = document.getElementById('goalsProgress');
    let progHTML = '';

    if (g.weekly_profit_target > 0) {
      const pct = Math.min(100, Math.max(0, (wp.realized_pnl / g.weekly_profit_target) * 100));
      const color = pct >= 100 ? 'var(--green)' : pct >= 50 ? 'var(--yellow)' : 'var(--red)';
      progHTML += '<div class="progress-row"><span class="progress-label">Weekly USD</span><div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div><span class="progress-val">$' + wp.realized_pnl.toFixed(2) + ' / $' + g.weekly_profit_target.toFixed(0) + '</span></div>';
    }
    if (g.monthly_profit_target > 0) {
      const pct = Math.min(100, Math.max(0, (mp.realized_pnl / g.monthly_profit_target) * 100));
      const color = pct >= 100 ? 'var(--green)' : pct >= 50 ? 'var(--yellow)' : 'var(--red)';
      progHTML += '<div class="progress-row"><span class="progress-label">Monthly USD</span><div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:' + pct + '%;background:' + color + '"></div></div><span class="progress-val">$' + mp.realized_pnl.toFixed(2) + ' / $' + g.monthly_profit_target.toFixed(0) + '</span></div>';
    }

    progHTML += '<div class="progress-row"><span class="progress-label">Weekly trades</span><span class="progress-val">' + (wp.trade_count || 0) + '</span></div>';
    progHTML += '<div class="progress-row"><span class="progress-label">Monthly trades</span><span class="progress-val">' + (mp.trade_count || 0) + '</span></div>';

    progDiv.innerHTML = progHTML || '<div style="color:var(--muted);font-size:13px;padding:8px 0">Set targets below to track progress</div>';

    goalsLoaded = true;
  } catch(e) { console.error('Goals load failed:', e); }
}

async function saveGoals() {
  const data = {
    weekly_profit_target: parseFloat(document.getElementById('goalWeeklyProfit').value) || 0,
    monthly_profit_target: parseFloat(document.getElementById('goalMonthlyProfit').value) || 0,
    weekly_btc_target: parseFloat(document.getElementById('goalWeeklyBtc').value) || 0,
    monthly_btc_target: parseFloat(document.getElementById('goalMonthlyBtc').value) || 0,
    notes: document.getElementById('goalNotes').value,
  };

  try {
    const resp = await fetch('/api/goals', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const d = await resp.json();
    const status = document.getElementById('goalsStatus');
    if (d.error) {
      status.textContent = 'Error: ' + d.error;
      status.style.color = 'var(--red)';
    } else {
      status.textContent = 'Saved!';
      status.style.color = 'var(--green)';
      loadGoals(); // refresh progress bars
      setTimeout(() => { status.textContent = ''; }, 3000);
    }
  } catch(e) {
    document.getElementById('goalsStatus').textContent = 'Save failed';
    document.getElementById('goalsStatus').style.color = 'var(--red)';
  }
}
</script>
</body>
</html>"""


class Dashboard:
    """Async web dashboard served alongside the trading bot."""

    def __init__(self, db, paper_trader=None, config=None, bot=None):
        self.db = db
        self.paper_trader = paper_trader
        self.config = config
        self.bot = bot  # reference to AlgoTraderBot for mode switching
        self._last_signals = {}
        self._last_price = 0
        self._http = httpx.AsyncClient(timeout=60.0)
        self.app = web.Application()
        self.app.router.add_get('/', self._index)
        self.app.router.add_get('/api/status', self._api_status)
        self.app.router.add_post('/api/mode', self._api_set_mode)
        self.app.router.add_get('/api/goals', self._api_get_goals)
        self.app.router.add_post('/api/goals', self._api_save_goals)
        self.app.router.add_get('/api/trades/all', self._api_all_trades)
        self.app.router.add_post('/api/chat', self._api_chat)
        self.app.router.add_get('/api/chat/history', self._api_chat_history)
        self.app.router.add_post('/api/chat/clear', self._api_chat_clear)

    def update_signals(self, price: float, signals_dict: dict):
        """Called by the bot after each scan to update displayed signals."""
        self._last_price = price
        self._last_signals = signals_dict

    async def _index(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type='text/html')

    async def _api_status(self, request):
        """JSON endpoint with all dashboard data."""
        try:
            return await self._api_status_inner(request)
        except Exception as e:
            logger.error(f"Status API error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _api_status_inner(self, request):
        """Actual status logic (wrapped for error handling)."""
        # Balance
        balance = {}
        starting_capital = 10000
        if self.paper_trader:
            bal = self.paper_trader.get_balance()
            balance = {
                "cash_usd": bal.cash_usd,
                "btc_quantity": bal.btc_quantity,
                "total_equity": bal.total_equity,
                "holdings": bal.holdings if bal.holdings else {},
            }
            starting_capital = self.config.paper.starting_capital if self.config else 10000

        # All open positions
        positions_list = self.db.get_open_positions()
        positions_data = [
            {
                "symbol": pos.symbol,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "entry_time": pos.entry_time,
                "unrealized_pnl": pos.unrealized_pnl,
            }
            for pos in positions_list
        ]
        # Backward compat: "position" is the first/primary position
        position = positions_data[0] if positions_data else None

        # Trades (last 3 days with P&L)
        three_days_ago = time.time() - (3 * 86400)
        trades_data = self.db.get_trades_with_pnl(since_ts=three_days_ago)

        # Equity history
        equity_history = self.db.get_equity_history(limit=500)

        data = {
            "mode": self.config.mode if self.config else "paper",
            "price": self._last_price,
            "signals": self._last_signals,
            "balance": balance,
            "starting_capital": starting_capital,
            "position": position,
            "positions": positions_data,
            "trades": trades_data,
            "equity_history": equity_history,
            "has_kraken_keys": bool(self.config and self.config.kraken.api_key),
        }

        return web.json_response(data)

    async def _api_set_mode(self, request):
        """Switch between paper and live mode."""
        try:
            body = await request.json()
            new_mode = body.get("mode", "").lower()

            if new_mode not in ("paper", "live"):
                return web.json_response({"error": "Mode must be 'paper' or 'live'"}, status=400)

            if new_mode == "live" and self.config and not self.config.kraken.api_key:
                return web.json_response(
                    {"error": "Cannot switch to live mode: KRAKEN_API_KEY not configured"},
                    status=400,
                )

            if self.config:
                old_mode = self.config.mode
                self.config.mode = new_mode

                # Update strategy's paper flag
                if self.bot and hasattr(self.bot, 'strategy'):
                    self.bot.strategy.is_paper = (new_mode == "paper")

                self.db.log("INFO", f"Mode switched from {old_mode} to {new_mode} via dashboard")
                logger.info(f"Mode switched: {old_mode} -> {new_mode}")

                return web.json_response({"mode": new_mode, "previous": old_mode})

            return web.json_response({"error": "Config not available"}, status=500)

        except Exception as e:
            logger.error(f"Mode switch failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Goals API
    # ------------------------------------------------------------------

    async def _api_get_goals(self, request):
        """Get current profit goals."""
        goals = self.db.get_goals()
        weekly_pnl = self.db.get_period_pnl(7 * 86400)
        monthly_pnl = self.db.get_period_pnl(30 * 86400)
        return web.json_response({
            "goals": goals,
            "weekly_progress": weekly_pnl,
            "monthly_progress": monthly_pnl,
        })

    async def _api_save_goals(self, request):
        """Save profit goals."""
        try:
            body = await request.json()
            self.db.save_goals(
                weekly_profit=float(body.get("weekly_profit_target", 0)),
                monthly_profit=float(body.get("monthly_profit_target", 0)),
                weekly_btc=float(body.get("weekly_btc_target", 0)),
                monthly_btc=float(body.get("monthly_btc_target", 0)),
                notes=body.get("notes", ""),
            )
            self.db.log("INFO", "Goals updated via dashboard")
            return web.json_response({"status": "saved"})
        except Exception as e:
            logger.error(f"Save goals failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    # ------------------------------------------------------------------
    # Chat API
    # ------------------------------------------------------------------

    async def _api_all_trades(self, request):
        """Return ALL trades with P&L data for the full history view."""
        try:
            trades = self.db.get_trades_with_pnl()
            stats = self.db.get_trade_stats()
            # Compute summary
            sells = [t for t in trades if t["side"] == "sell" and t["pnl_dollar"] is not None]
            total_pnl = sum(t["pnl_dollar"] for t in sells)
            winners = [t for t in sells if t["pnl_dollar"] > 0]
            losers = [t for t in sells if t["pnl_dollar"] < 0]
            win_rate = (len(winners) / len(sells) * 100) if sells else 0
            avg_win = (sum(t["pnl_dollar"] for t in winners) / len(winners)) if winners else 0
            avg_loss = (sum(t["pnl_dollar"] for t in losers) / len(losers)) if losers else 0

            return web.json_response({
                "trades": trades,
                "summary": {
                    "total_trades": stats.get("total_trades", 0),
                    "total_buys": stats.get("buys", 0),
                    "total_sells": stats.get("sells", 0),
                    "total_fees": round(stats.get("total_fees", 0) or 0, 2),
                    "total_pnl": round(total_pnl, 2),
                    "win_rate": round(win_rate, 1),
                    "avg_win": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "winners": len(winners),
                    "losers": len(losers),
                },
            })
        except Exception as e:
            logger.error(f"All trades endpoint failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _api_chat(self, request):
        """Handle a chat message from the user — uses the SAME trading engine brain."""
        try:
            body = await request.json()
            user_msg = body.get("message", "").strip()
            if not user_msg:
                return web.json_response({"error": "Empty message"}, status=400)

            api_key = self.config.anthropic_api_key if self.config else ""
            if not api_key:
                return web.json_response(
                    {"error": "No ANTHROPIC_API_KEY configured. Add it to your .env file."},
                    status=400,
                )

            # Save user message
            self.db.add_chat_message("user", user_msg)

            # Get the REAL trading engine system prompt and context
            # This makes chat Claude the SAME brain as trading Claude
            from ai_strategy import SYSTEM_PROMPT as TRADING_PROMPT
            system_prompt = TRADING_PROMPT + """

## CHAT MODE
Your operator is talking to you via the dashboard chat. Respond conversationally — do NOT respond in JSON format.
Be direct and data-driven. Reference specific numbers from the market data below.
You ARE the trading engine — when you say "I'll do X", you mean it. Your next scan cycle will reflect your thinking here.
Keep responses concise (2-4 paragraphs). Use $ and % formatting for financial data.
If the operator gives you instructions, acknowledge them — they will be fed into your next trading scan."""

            # Build the SAME rich context the trading engine sees
            context = self._build_full_trading_context()

            # Build conversation history (last 10 messages)
            history = self.db.get_chat_history(limit=10)
            messages = []

            # First message: full market context
            messages.append({
                "role": "user",
                "content": f"[LIVE MARKET DATA - SAME DATA YOU USE FOR TRADING]\n{context}\n[END MARKET DATA]",
            })
            messages.append({
                "role": "assistant",
                "content": "I have the full market picture. What would you like to discuss?",
            })

            # Add conversation history
            for msg in history:
                messages.append({"role": msg["role"], "content": msg["message"]})

            # Ensure proper alternation
            messages = self._fix_message_alternation(messages)

            # Call Claude with the SAME model and personality as the trading engine
            model = self.config.ai_model if self.config else "claude-sonnet-4-20250514"
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 800,
                    "system": system_prompt,
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            result = resp.json()
            ai_reply = result["content"][0]["text"]

            # Save AI response
            self.db.add_chat_message("assistant", ai_reply)

            return web.json_response({"reply": ai_reply})

        except httpx.HTTPStatusError as e:
            logger.error(f"Chat API error: {e.response.status_code} {e.response.text}")
            return web.json_response({"error": f"Claude API error: {e.response.status_code}"}, status=500)
        except Exception as e:
            logger.error(f"Chat failed: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _api_chat_history(self, request):
        """Get chat history."""
        history = self.db.get_chat_history(limit=50)
        return web.json_response({"messages": history})

    async def _api_chat_clear(self, request):
        """Clear chat history."""
        self.db.clear_chat_history()
        return web.json_response({"status": "cleared"})

    def _fix_message_alternation(self, messages):
        """Ensure messages alternate user/assistant (Anthropic API requirement)."""
        if not messages:
            return messages
        fixed = [messages[0]]
        for msg in messages[1:]:
            if msg["role"] == fixed[-1]["role"]:
                # Same role back-to-back — merge content
                fixed[-1]["content"] += "\n" + msg["content"]
            else:
                fixed.append(msg)
        # Must start with user
        if fixed and fixed[0]["role"] != "user":
            fixed.insert(0, {"role": "user", "content": "Hello"})
        # Must end with user for a new query
        if fixed and fixed[-1]["role"] != "user":
            pass  # The last user message should already be there
        return fixed

    def _build_full_trading_context(self) -> str:
        """Return the EXACT same context the trading engine used on its last scan."""
        # Use the cached context from the last trading scan — this is the real deal
        if self.bot and hasattr(self.bot, 'strategy'):
            cached = getattr(self.bot.strategy, '_last_context', '')
            if cached:
                logger.info(f"Chat using cached trading context ({len(cached)} chars)")
                return cached
            else:
                logger.warning(f"Bot strategy exists but _last_context is empty — scan may not have run yet")
        else:
            logger.warning(f"Bot reference missing: bot={self.bot is not None}, has_strategy={hasattr(self.bot, 'strategy') if self.bot else 'N/A'}")

        # Fallback: build what we can from dashboard's own cached data
        parts = []
        if self._last_price:
            parts.append(f"BTC/USD: ${self._last_price:,.2f}")
        else:
            parts.append("Waiting for first scan to complete — price data not yet available.")

        if self._last_signals:
            sigs = self._last_signals
            if sigs.get("ai_action"):
                parts.append(f"Last AI Decision: {sigs['ai_action']} (confidence: {sigs.get('ai_confidence', '?')})")
                parts.append(f"Reasoning: {sigs.get('ai_reasoning', 'N/A')}")
            if sigs.get("fear_greed"):
                parts.append(f"Fear & Greed: {sigs['fear_greed']} ({sigs.get('fear_greed_label', '')})")
            if sigs.get("rsi"):
                parts.append(f"RSI: {sigs['rsi']}")
            if sigs.get("coin_data"):
                parts.append("Market Overview:")
                for coin in sigs["coin_data"]:
                    parts.append(f"  {coin['symbol']}: ${coin['price']:,.2f} (1h: {coin.get('change_1h', 0):+.1f}%, 24h: {coin.get('change_24h', 0):+.1f}%)")

        if self.paper_trader:
            bal = self.paper_trader.get_balance()
            parts.append(f"Equity: ${bal.total_equity:,.2f} | Cash: ${bal.cash_usd:,.2f}")

        position = self.db.get_open_position()
        if position:
            parts.append(f"Open Position: {position.quantity:.6f} BTC @ ${position.entry_price:,.2f}")
        else:
            parts.append("No open position")

        parts.append(f"Mode: {'PAPER' if self.config and self.config.mode == 'paper' else 'LIVE'}")
        return "\n".join(parts)

    async def start(self, host: str = "0.0.0.0", port: int = 3737):
        """Start the web server (non-blocking)."""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"Dashboard running at http://{host}:{port}")
        return runner
