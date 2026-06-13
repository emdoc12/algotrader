"""Web dashboard for the four-desk AI trading competition.

A single-file, dependency-free dashboard (Python stdlib only) that lets the
owner watch Claude / OpenAI / Grok / Qwen trade identical $10k paper accounts,
inspect each desk's thinking and trades, and chat with each team's leader.

Serving:
    python3 -m daytrader.live.dashboard   (if run as __main__ below)
or  from daytrader.live import dashboard; dashboard.serve(8787)

The HTTP server reads each team's SQLite DB per request (fresh LiveDB, read,
close — WAL makes concurrent reads safe alongside the trading loop). The page
is a self-contained dark-theme HTML string with hand-rolled canvas charting in
vanilla JS; it works fully offline (no CDN, no framework).
"""
from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from daytrader.live.competition import (
    START_CASH,
    Competition,
    chat_with_leader,
    db_standings,
    team_db_path,
    team_names,
)
from daytrader.live.db import LiveDB


# --------------------------------------------------------------------------- #
# data helpers (network-free, tolerant of missing DBs)                        #
# --------------------------------------------------------------------------- #
def _team_db(name: str) -> LiveDB | None:
    """Open a fresh LiveDB for a team, or None if it can't be opened."""
    try:
        return LiveDB(team_db_path(name))
    except Exception:  # noqa: BLE001
        return None


def _safe(db: LiveDB | None, method: str, *args, default):
    """Call a LiveDB read method defensively, returning ``default`` on failure."""
    if db is None:
        return default
    try:
        return getattr(db, method)(*args)
    except Exception:  # noqa: BLE001
        return default


def overview_payload() -> dict:
    """Standings for all four teams plus their equity curves."""
    curves: dict[str, list] = {}
    for name in team_names():
        db = _team_db(name)
        try:
            curves[name] = _safe(db, "equity_curve", 500, default=[])
        finally:
            if db is not None:
                db.close()
    return {
        "start_cash": START_CASH,
        "standings": db_standings(),
        "curves": curves,
    }


def team_payload(name: str) -> dict:
    """Everything a single team tab needs, tolerant of a missing DB."""
    db = _team_db(name)
    try:
        return {
            "positions": _safe(db, "load_open_positions", default=[]),
            "trades": _safe(db, "recent_trades", 60, default=[]),
            "journal": _safe(db, "recent_journal", 40, default=[]),
            "thinking": _safe(db, "recent_agent_log", 80, default=[]),
            "dev_requests": _safe(db, "open_dev_requests", default=[]),
            "chat": _safe(db, "recent_chat", 50, default=[]),
        }
    finally:
        if db is not None:
            db.close()


# --------------------------------------------------------------------------- #
# HTTP handler                                                                #
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "AITradeDash/1.0"

    # quieter logging
    def log_message(self, fmt, *args):  # noqa: D401, N802
        pass

    # -- low-level send helpers ----------------------------------------- #
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, html: str, code: int = 200) -> None:
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

    # -- routing -------------------------------------------------------- #
    def do_GET(self):  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                self._html(PAGE_HTML)
                return
            if path == "/api/overview":
                self._json(overview_payload())
                return
            if path.startswith("/api/team/"):
                rest = path[len("/api/team/"):]
                name = urllib.parse.unquote(rest.strip("/"))
                if name and name in team_names():
                    self._json(team_payload(name))
                    return
                self._json({"error": f"unknown team {name!r}"}, 404)
                return
            self._json({"error": "not found", "path": path}, 404)
        except Exception as e:  # noqa: BLE001 — never crash the server
            self._json({"error": repr(e)}, 500)

    def do_POST(self):  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path
            if path.startswith("/api/team/") and path.endswith("/chat"):
                middle = path[len("/api/team/"):-len("/chat")]
                name = urllib.parse.unquote(middle.strip("/"))
                if name not in team_names():
                    self._json({"ok": False, "reply": "", "error": f"unknown team {name!r}"}, 404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:  # noqa: BLE001
                    data = {}
                message = (data.get("message") or "").strip()
                if not message:
                    self._json({"ok": False, "reply": "", "error": "empty message"}, 400)
                    return
                result = chat_with_leader(name, message)
                self._json(result)
                return
            self._json({"error": "not found", "path": path}, 404)
        except Exception as e:  # noqa: BLE001 — never crash the server
            self._json({"ok": False, "reply": "", "error": repr(e)}, 500)


# --------------------------------------------------------------------------- #
# server construction + entrypoint                                            #
# --------------------------------------------------------------------------- #
def _make_server(port: int = 8787) -> ThreadingHTTPServer:
    """Build (but do not start) the ThreadingHTTPServer for the dashboard.

    Used by both ``serve()`` and the self-test. Building the server does NOT
    start the trading loop, so it is cheap and side-effect-free.
    """
    return ThreadingHTTPServer(("0.0.0.0", port), _Handler)


def serve(port: int = 8787) -> None:
    """Start the competition trading loop in a daemon thread, then serve.

    The competition self-skips teams without an API key (and prints a note if
    none are configured), so it is safe to start unconditionally. Blocks
    forever serving the dashboard.
    """
    def _run_competition():
        try:
            Competition().run_forever()
        except Exception as e:  # noqa: BLE001 — keep the dashboard alive
            print(f"[dashboard] competition loop exited: {e!r}")

    threading.Thread(target=_run_competition, daemon=True, name="competition").start()

    srv = _make_server(port)
    url = f"http://127.0.0.1:{port}"
    print(f"[dashboard] AI Trading Desk Competition serving at {url}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] shutting down")
        srv.shutdown()


# --------------------------------------------------------------------------- #
# the page (dark theme, vanilla JS, hand-drawn canvas chart)                  #
# --------------------------------------------------------------------------- #
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Trading Desk Competition</title>
<style>
  :root{
    --bg:#0a0a0c; --card:#141417; --line:#23232a;
    --green:#36d399; --red:#f87272; --gray:#8b8b96; --txt:#e6e6ea;
    --accent:#7c8cff;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--line)}
  header h1{margin:0;font-size:20px;letter-spacing:.3px}
  header .sub{color:var(--gray);font-size:12px;margin-top:4px}
  .tabs{display:flex;gap:6px;padding:10px 24px 0;flex-wrap:wrap}
  .tab{padding:8px 16px;border:1px solid var(--line);border-bottom:none;
       background:var(--card);color:var(--gray);cursor:pointer;
       border-radius:8px 8px 0 0;font-size:13px;user-select:none}
  .tab.active{color:var(--txt);background:#1c1c22;border-color:#33333d}
  main{padding:18px 24px 60px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:16px;margin-bottom:18px}
  .card h2{margin:0 0 12px;font-size:14px;color:var(--gray);
           text-transform:uppercase;letter-spacing:.6px;font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--line);
        white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--gray);font-weight:600;font-size:11px;text-transform:uppercase;
     letter-spacing:.5px}
  tr:last-child td{border-bottom:none}
  .green{color:var(--green)} .red{color:var(--red)} .gray{color:var(--gray)}
  .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;
        background:#23232a;color:var(--gray)}
  canvas{width:100%;height:340px;display:block}
  .stats{display:flex;gap:24px;flex-wrap:wrap}
  .stat{min-width:96px}
  .stat .k{font-size:11px;color:var(--gray);text-transform:uppercase;letter-spacing:.5px}
  .stat .v{font-size:20px;margin-top:2px}
  .legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:12px}
  .legend .li{display:flex;align-items:center;gap:6px;color:var(--gray)}
  .legend .sw{width:14px;height:3px;border-radius:2px;display:inline-block}
  .feed{max-height:420px;overflow:auto}
  .feed .item{padding:9px 0;border-bottom:1px solid var(--line)}
  .feed .item:last-child{border-bottom:none}
  .feed .meta{font-size:11px;color:var(--gray);margin-bottom:2px}
  .feed .body{font-size:13px;white-space:pre-wrap;word-break:break-word}
  .tag{font-size:10px;padding:1px 6px;border-radius:8px;margin-right:6px;
       background:#23232a}
  .tag.j{color:var(--accent)} .tag.l{color:#ffd479}
  .muted{color:var(--gray)}
  .chat{display:flex;flex-direction:column;gap:8px;max-height:380px;overflow:auto;
        padding-right:4px}
  .msg{max-width:78%;padding:9px 12px;border-radius:12px;font-size:13px;
       white-space:pre-wrap;word-break:break-word}
  .msg.owner{align-self:flex-end;background:#2a3350;border:1px solid #3a4570}
  .msg.leader{align-self:flex-start;background:#1c1c22;border:1px solid var(--line)}
  .msg .who{font-size:10px;color:var(--gray);margin-bottom:3px}
  .chatbar{display:flex;gap:8px;margin-top:12px}
  .chatbar input{flex:1;background:#0e0e11;border:1px solid var(--line);
       color:var(--txt);padding:10px 12px;border-radius:8px;font-size:13px}
  button{background:var(--accent);color:#0a0a0c;border:none;padding:9px 16px;
       border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
  button:disabled{opacity:.5;cursor:default}
  .btn-ghost{background:#23232a;color:var(--txt)}
  .row{display:flex;justify-content:space-between;align-items:center;
       margin-bottom:12px}
  .err{color:var(--red);font-size:12px;margin-top:6px}
  a{color:var(--accent)}
  .empty{color:var(--gray);font-size:13px;padding:6px 0}
</style>
</head>
<body>
<header>
  <h1>AI Trading Desk Competition</h1>
  <div class="sub">Four AI desks &mdash; Claude, OpenAI, Grok, Qwen &mdash; each start with
    <b>$10,000</b> in an identical paper account. May the best model win.</div>
</header>
<div class="tabs" id="tabs"></div>
<main id="main"></main>

<script>
"use strict";
const TEAMS = ["claude","openai","grok","qwen"];
const LABELS = {claude:"Claude", openai:"OpenAI", grok:"Grok", qwen:"Qwen"};
const COLORS = {claude:"#36d399", openai:"#7c8cff", grok:"#f87272", qwen:"#ffd479"};
const START = 10000;
let current = "overview";
let refreshTimer = null;

function el(tag, attrs, ...kids){
  const e = document.createElement(tag);
  if(attrs) for(const k in attrs){
    if(k === "class") e.className = attrs[k];
    else if(k === "html") e.innerHTML = attrs[k];
    else if(k.startsWith("on")) e.addEventListener(k.slice(2), attrs[k]);
    else e.setAttribute(k, attrs[k]);
  }
  for(const kid of kids){
    if(kid == null) continue;
    e.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return e;
}
function fmtMoney(v){ return "$" + Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtPct(v){ const n = Number(v); return (n>=0?"+":"") + n.toFixed(2) + "%"; }
function clsFor(v){ return Number(v) > 0 ? "green" : (Number(v) < 0 ? "red" : "gray"); }
function esc(s){ const d = document.createElement("div"); d.textContent = (s==null?"":String(s)); return d.innerHTML; }
async function getJSON(url){ const r = await fetch(url, {cache:"no-store"}); return await r.json(); }

// ---- tabs --------------------------------------------------------------- //
function buildTabs(){
  const t = document.getElementById("tabs");
  t.innerHTML = "";
  const mk = (id, label) => {
    const tab = el("div", {class:"tab" + (current===id?" active":""), onclick:()=>switchTab(id)}, label);
    t.appendChild(tab);
  };
  mk("overview", "Overview");
  for(const tm of TEAMS) mk(tm, LABELS[tm]);
}
function switchTab(id){
  current = id;
  if(refreshTimer){ clearInterval(refreshTimer); refreshTimer = null; }
  buildTabs();
  if(id === "overview") loadOverview();
  else loadTeam(id);
}

// ---- overview ----------------------------------------------------------- //
async function loadOverview(){
  const main = document.getElementById("main");
  if(current !== "overview") return;
  let data;
  try{ data = await getJSON("/api/overview"); }
  catch(e){ main.innerHTML = ""; main.appendChild(el("div",{class:"err"}, "Failed to load: "+e)); return; }
  if(current !== "overview") return;

  main.innerHTML = "";
  // leaderboard
  const lbCard = el("div", {class:"card"});
  lbCard.appendChild(el("h2", null, "Leaderboard"));
  const tbl = el("table");
  const head = el("tr");
  ["#","Team","Model","Equity","Return","Drawdown","PF","Win %","Trades","Open"]
    .forEach(h => head.appendChild(el("th", null, h)));
  tbl.appendChild(head);
  (data.standings||[]).forEach(s => {
    const tr = el("tr");
    const nameCell = s.has_key
      ? LABELS[s.team] || s.team
      : (LABELS[s.team]||s.team) + "  (no key — idle)";
    const cells = [
      el("td", null, String(s.rank)),
      el("td", {class: s.has_key?"":"gray"}, nameCell),
      el("td", {class:"gray"}, s.model||"?"),
      el("td", {class: Number(s.equity)>=START?"green":"red"}, fmtMoney(s.equity)),
      el("td", {class: clsFor(s.return_pct)}, fmtPct(s.return_pct)),
      el("td", {class:"red"}, "-"+Number(s.drawdown_pct||0).toFixed(2)+"%"),
      el("td", null, Number(s.profit_factor||0).toFixed(2)),
      el("td", null, Number(s.win_rate||0).toFixed(1)+"%"),
      el("td", null, String(s.n_trades||0)),
      el("td", null, String(s.open_positions||0)),
    ];
    cells.forEach(c => tr.appendChild(c));
    tbl.appendChild(tr);
  });
  lbCard.appendChild(tbl);
  main.appendChild(lbCard);

  // equity chart
  const chartCard = el("div", {class:"card"});
  chartCard.appendChild(el("h2", null, "Equity Curves"));
  const cv = el("canvas", {id:"equityChart"});
  chartCard.appendChild(cv);
  const legend = el("div", {class:"legend"});
  for(const tm of TEAMS){
    legend.appendChild(el("div",{class:"li"},
      el("span",{class:"sw", style:"background:"+COLORS[tm]}), LABELS[tm]));
  }
  legend.appendChild(el("div",{class:"li"},
    el("span",{class:"sw", style:"background:var(--gray)"}), "$10,000 baseline"));
  chartCard.appendChild(legend);
  main.appendChild(chartCard);
  requestAnimationFrame(() => drawEquityChart(cv, data));

  if(!refreshTimer && current === "overview"){
    refreshTimer = setInterval(() => { if(current==="overview") loadOverview(); }, 15000);
  }
}

function drawEquityChart(canvas, data){
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 800, H = canvas.clientHeight || 340;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);

  const padL = 64, padR = 14, padT = 14, padB = 28;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const curves = data.curves || {};
  const start = Number(data.start_cash || START);

  // gather min/max equity + max length
  let lo = start, hi = start, maxLen = 0, anyPoints = false;
  const tsFirst = {}, tsLast = {};
  for(const tm of TEAMS){
    const c = curves[tm] || [];
    if(c.length){ anyPoints = true; tsFirst[tm] = c[0].ts; tsLast[tm] = c[c.length-1].ts; }
    maxLen = Math.max(maxLen, c.length);
    for(const p of c){
      const e = Number(p.equity);
      if(isFinite(e)){ lo = Math.min(lo,e); hi = Math.max(hi,e); }
    }
  }
  // pad range a little
  if(hi === lo){ hi = lo + 1; }
  const range = hi - lo;
  lo -= range * 0.06; hi += range * 0.06;

  const yFor = v => padT + plotH - ((v - lo)/(hi - lo)) * plotH;
  const xFor = (i, n) => padL + (n <= 1 ? plotW/2 : (i/(n-1)) * plotW);

  // grid + axis
  ctx.strokeStyle = "#23232a"; ctx.lineWidth = 1;
  ctx.fillStyle = "#8b8b96"; ctx.font = "11px sans-serif";
  ctx.textAlign = "right"; ctx.textBaseline = "middle";
  const ticks = 4;
  for(let i=0;i<=ticks;i++){
    const v = lo + (hi-lo)*(i/ticks);
    const y = yFor(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W-padR, y); ctx.stroke();
    ctx.fillText("$"+Math.round(v).toLocaleString(), padL-8, y);
  }

  // baseline at start_cash (dashed) if in range
  if(start >= lo && start <= hi){
    const y = yFor(start);
    ctx.save();
    ctx.strokeStyle = "#8b8b96"; ctx.setLineDash([5,4]); ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W-padR, y); ctx.stroke();
    ctx.restore();
  }

  if(!anyPoints){
    ctx.fillStyle = "#8b8b96"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.font = "13px sans-serif";
    ctx.fillText("No equity snapshots yet — waiting for trading to begin.", W/2, H/2);
    return;
  }

  // each team's line, plotted across the full width regardless of length
  for(const tm of TEAMS){
    const c = curves[tm] || [];
    if(c.length === 0) continue;
    ctx.strokeStyle = COLORS[tm]; ctx.lineWidth = 1.8;
    ctx.beginPath();
    if(c.length === 1){
      const y = yFor(Number(c[0].equity));
      ctx.arc(xFor(0,1), y, 2.4, 0, Math.PI*2);
      ctx.fillStyle = COLORS[tm]; ctx.fill();
      continue;
    }
    c.forEach((p,i) => {
      const x = xFor(i, c.length), y = yFor(Number(p.equity));
      if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.stroke();
  }

  // time axis labels (first/last) using the longest available series
  let labelTm = TEAMS.find(t => (curves[t]||[]).length) || TEAMS[0];
  const fmtTs = s => { if(!s) return ""; return String(s).replace("T"," ").slice(5,16); };
  ctx.fillStyle = "#8b8b96"; ctx.textBaseline = "top"; ctx.font = "11px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(fmtTs(tsFirst[labelTm]), padL, H-padB+8);
  ctx.textAlign = "right";
  ctx.fillText(fmtTs(tsLast[labelTm]), W-padR, H-padB+8);
}

// ---- a team tab --------------------------------------------------------- //
let chatBusy = false;

async function loadTeam(name){
  const main = document.getElementById("main");
  if(current !== name) return;
  let standings = [], data;
  try{
    const ov = await getJSON("/api/overview");
    standings = ov.standings || [];
    data = await getJSON("/api/team/" + encodeURIComponent(name));
  }catch(e){
    main.innerHTML = ""; main.appendChild(el("div",{class:"err"}, "Failed to load: "+e)); return;
  }
  if(current !== name) return;
  const row = standings.find(s => s.team === name) || {};
  main.innerHTML = "";

  // header + refresh
  const top = el("div", {class:"row"});
  top.appendChild(el("h2", {style:"margin:0;text-transform:none;font-size:18px;color:var(--txt)"},
    LABELS[name] + " desk" + (row.has_key===false ? "  (no key — idle)" : "")));
  top.appendChild(el("button", {class:"btn-ghost", onclick:()=>loadTeam(name)}, "Refresh"));
  main.appendChild(top);

  // stat header
  const statCard = el("div", {class:"card"});
  const stats = el("div", {class:"stats"});
  const mkStat = (k, v, cls) => {
    const s = el("div", {class:"stat"});
    s.appendChild(el("div", {class:"k"}, k));
    s.appendChild(el("div", {class:"v "+(cls||"")}, v));
    stats.appendChild(s);
  };
  mkStat("Equity", fmtMoney(row.equity!=null?row.equity:START), Number(row.equity)>=START?"green":"red");
  mkStat("Return", fmtPct(row.return_pct||0), clsFor(row.return_pct||0));
  mkStat("Drawdown", "-"+Number(row.drawdown_pct||0).toFixed(2)+"%", "red");
  mkStat("Profit factor", Number(row.profit_factor||0).toFixed(2));
  mkStat("Trades", String(row.n_trades||0));
  mkStat("Model", row.model||"?", "gray");
  statCard.appendChild(stats);
  main.appendChild(statCard);

  // open positions
  const posCard = el("div", {class:"card"});
  posCard.appendChild(el("h2", null, "Open positions"));
  const positions = data.positions || [];
  if(positions.length){
    const t = el("table");
    const h = el("tr");
    ["Symbol","Side","Qty","Entry","Stop","Target","Strategy"].forEach(x=>h.appendChild(el("th",null,x)));
    t.appendChild(h);
    positions.forEach(p => {
      const tr = el("tr");
      [el("td",null,p.symbol||""),
       el("td",{class: (p.side||"").toLowerCase()==="short"?"red":"green"}, (p.side||"").toUpperCase()),
       el("td",null,String(p.qty!=null?p.qty:"")),
       el("td",null,p.entry_price!=null?fmtMoney(p.entry_price):""),
       el("td",null,p.stop!=null?fmtMoney(p.stop):"—"),
       el("td",null,p.target!=null?fmtMoney(p.target):"—"),
       el("td",{class:"gray"},p.strategy||"")].forEach(c=>tr.appendChild(c));
      t.appendChild(tr);
    });
    posCard.appendChild(t);
  } else {
    posCard.appendChild(el("div",{class:"empty"},"No open positions."));
  }
  main.appendChild(posCard);

  // recent trades
  const trCard = el("div", {class:"card"});
  trCard.appendChild(el("h2", null, "Recent trades"));
  const trades = data.trades || [];
  if(trades.length){
    const t = el("table");
    const h = el("tr");
    ["Symbol","Side","Entry → Exit","Qty","P&L","Reason"].forEach(x=>h.appendChild(el("th",null,x)));
    t.appendChild(h);
    trades.forEach(tr0 => {
      const tr = el("tr");
      const entry = tr0.entry_price!=null?fmtMoney(tr0.entry_price):"—";
      const exit = tr0.exit_price!=null?fmtMoney(tr0.exit_price):"open";
      const pnl = Number(tr0.pnl||0);
      [el("td",null,tr0.symbol||""),
       el("td",{class:(tr0.side||"").toLowerCase()==="short"?"red":"green"},(tr0.side||"").toUpperCase()),
       el("td",{class:"gray"}, entry + " → " + exit),
       el("td",null,String(tr0.qty!=null?tr0.qty:"")),
       el("td",{class:clsFor(pnl)}, (pnl>=0?"+":"") + fmtMoney(pnl).replace("$-","$")),
       el("td",{class:"gray"}, tr0.exit_reason||"")].forEach(c=>tr.appendChild(c));
      t.appendChild(tr);
    });
    trCard.appendChild(t);
  } else {
    trCard.appendChild(el("div",{class:"empty"},"No trades yet."));
  }
  main.appendChild(trCard);

  // thinking feed (journal + agent_log merged, reverse-chronological)
  const thinkCard = el("div", {class:"card"});
  thinkCard.appendChild(el("h2", null, "Thinking & activity"));
  const feed = el("div", {class:"feed"});
  const merged = [];
  (data.journal||[]).forEach(j => merged.push({
    ts: j.ts, kind:"journal",
    title: (j.topic||"note") + (j.author?(" · "+j.author):""),
    body: j.note||""
  }));
  (data.thinking||[]).forEach(a => merged.push({
    ts: a.ts, kind:"log",
    title: (a.agent||"agent") + " · " + (a.action||""),
    body: a.detail||""
  }));
  merged.sort((a,b) => String(b.ts).localeCompare(String(a.ts)));
  if(merged.length){
    merged.forEach(m => {
      const item = el("div",{class:"item"});
      const meta = el("div",{class:"meta"});
      meta.appendChild(el("span",{class:"tag " + (m.kind==="journal"?"j":"l")},
        m.kind==="journal"?"JOURNAL":"ACTION"));
      meta.appendChild(document.createTextNode((m.ts? String(m.ts).replace("T"," ").slice(0,19)+"  ":"") + m.title));
      item.appendChild(meta);
      if(m.body) item.appendChild(el("div",{class:"body"}, m.body));
      feed.appendChild(item);
    });
    thinkCard.appendChild(feed);
  } else {
    thinkCard.appendChild(el("div",{class:"empty"},"No journal entries or activity yet."));
  }
  main.appendChild(thinkCard);

  // dev requests
  const devCard = el("div", {class:"card"});
  devCard.appendChild(el("h2", null, "Dev requests"));
  const devs = data.dev_requests || [];
  if(devs.length){
    devs.forEach(d => {
      const item = el("div",{class:"item"});
      const meta = el("div",{class:"meta"});
      meta.appendChild(el("span",{class:"pill"}, d.status||"open"));
      item.appendChild(meta);
      const title = el("div",{class:"body"});
      title.appendChild(document.createTextNode(d.title||"(untitled)"));
      if(d.url){
        title.appendChild(document.createTextNode("  "));
        title.appendChild(el("a",{href:d.url, target:"_blank", rel:"noopener"}, "link"));
      }
      item.appendChild(title);
      if(d.body) item.appendChild(el("div",{class:"muted", style:"font-size:12px;margin-top:3px;white-space:pre-wrap"}, d.body));
      devCard.appendChild(item);
    });
  } else {
    devCard.appendChild(el("div",{class:"empty"},"No open dev requests."));
  }
  main.appendChild(devCard);

  // chat panel
  const chatCard = el("div", {class:"card"});
  chatCard.appendChild(el("h2", null, "Chat with the desk leader"));
  const chatBox = el("div",{class:"chat", id:"chatBox"});
  renderChat(chatBox, data.chat||[]);
  chatCard.appendChild(chatBox);
  const bar = el("div",{class:"chatbar"});
  const input = el("input",{type:"text", id:"chatInput",
    placeholder: row.has_key===false ? "(no API key — leader is idle)" : "Ask the leader about their trades…"});
  if(row.has_key===false) input.disabled = true;
  const sendBtn = el("button",{id:"chatSend"}, "Send");
  if(row.has_key===false) sendBtn.disabled = true;
  const doSend = () => sendChat(name);
  sendBtn.addEventListener("click", doSend);
  input.addEventListener("keydown", e => { if(e.key==="Enter") doSend(); });
  bar.appendChild(input); bar.appendChild(sendBtn);
  chatCard.appendChild(bar);
  chatCard.appendChild(el("div",{class:"err", id:"chatErr"}));
  main.appendChild(chatCard);
}

function renderChat(box, msgs){
  box.innerHTML = "";
  if(!msgs.length){
    box.appendChild(el("div",{class:"empty"},"No messages yet — say hello to the desk leader."));
  }
  msgs.forEach(m => {
    const owner = m.role === "owner";
    const wrap = el("div",{class:"msg " + (owner?"owner":"leader")});
    wrap.appendChild(el("div",{class:"who"}, owner?"You":"Leader"));
    wrap.appendChild(document.createTextNode(m.content||""));
    box.appendChild(wrap);
  });
  box.scrollTop = box.scrollHeight;
}

async function sendChat(name){
  if(chatBusy) return;
  const input = document.getElementById("chatInput");
  const box = document.getElementById("chatBox");
  const errEl = document.getElementById("chatErr");
  const btn = document.getElementById("chatSend");
  const msg = (input.value||"").trim();
  if(!msg) return;
  chatBusy = true; errEl.textContent = "";
  input.disabled = true; btn.disabled = true;

  // optimistic owner bubble + thinking state
  box.appendChild(Object.assign(document.createElement("div"),
    {className:"msg owner", textContent: msg}));
  const thinking = el("div",{class:"msg leader"}, "thinking…");
  box.appendChild(thinking);
  box.scrollTop = box.scrollHeight;
  input.value = "";

  try{
    const r = await fetch("/api/team/" + encodeURIComponent(name) + "/chat", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({message: msg}),
    });
    const res = await r.json();
    if(res && res.error){ errEl.textContent = res.error; }
  }catch(e){
    errEl.textContent = "Request failed: " + e;
  }finally{
    chatBusy = false;
    // re-fetch authoritative chat history
    try{
      const data = await getJSON("/api/team/" + encodeURIComponent(name));
      if(current === name){
        const liveBox = document.getElementById("chatBox");
        if(liveBox) renderChat(liveBox, data.chat||[]);
      }
    }catch(e){ /* leave optimistic state */ }
    const liveInput = document.getElementById("chatInput");
    const liveBtn = document.getElementById("chatSend");
    if(liveInput) liveInput.disabled = false;
    if(liveBtn) liveBtn.disabled = false;
    if(liveInput) liveInput.focus();
  }
}

// ---- boot --------------------------------------------------------------- //
buildTabs();
loadOverview();
window.addEventListener("resize", () => {
  if(current === "overview"){
    const cv = document.getElementById("equityChart");
    if(cv) getJSON("/api/overview").then(d => drawEquityChart(cv, d)).catch(()=>{});
  }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    serve()
