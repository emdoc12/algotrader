"""Web dashboard for the four-desk AI trading competition.

A single-file, dependency-free dashboard (Python stdlib only) that lets the
owner watch Claude / OpenAI / Grok / Qwen trade identical $25k paper accounts,
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
from daytrader.live import settings


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


def close_dev_request(team: str, req_id, status: str = "closed",
                       resolution: str = "") -> dict:
    """Owner-side close/update of a dev request from the dashboard."""
    if team not in team_names():
        return {"ok": False, "error": f"unknown team {team!r}"}
    try:
        rid = int(req_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "id (integer) required"}
    if status not in ("closed", "wont_fix", "open"):
        status = "closed"
    db = _team_db(team)
    if db is None:
        return {"ok": False, "error": "team db unavailable"}
    try:
        changed = db.update_dev_request(rid, status=status, resolution=resolution)
        return {"ok": bool(changed), "id": rid, "status": status}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}
    finally:
        db.close()


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

    # -- security helpers ---------------------------------------------- #
    def _token_ok(self) -> bool:
        """When DASHBOARD_TOKEN is set, require it on /api/* requests (header or
        ?token=). Unset (default) = open, so existing deployments don't break."""
        import os
        tok = os.environ.get("DASHBOARD_TOKEN", "")
        if not tok:
            return True
        got = self.headers.get("X-Dashboard-Token", "")
        if not got:
            got = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("token", [""])[0]
        import hmac
        return hmac.compare_digest(str(got), str(tok))

    def _origin_ok(self) -> bool:
        """Block cross-origin (CSRF) POSTs: if an Origin header is present it must
        match Host. Absent Origin (curl / same-origin no-CORS) is allowed."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
        except Exception:  # noqa: BLE001
            return False

    def _read_body(self, cap: int = 262144) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(min(length, cap))

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
                self._html(_render_page())
                return
            if path.startswith("/api/") and not self._token_ok():
                self._json({"error": "unauthorized"}, 401)
                return
            if path == "/api/overview":
                self._json(overview_payload())
                return
            if path == "/api/settings":
                self._json(settings.masked_status())
                return
            if path == "/api/health":
                from daytrader.live.healthcheck import health_snapshot
                self._json(health_snapshot())
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
            # CSRF + optional-token gate on every mutating / paid endpoint.
            if not self._origin_ok():
                self._json({"error": "cross-origin request refused"}, 403)
                return
            if not self._token_ok():
                self._json({"error": "unauthorized"}, 401)
                return
            if path == "/api/check":
                # Live provider pings — moved to POST so a cross-origin <img>/GET
                # can't trigger paid API calls (drive-by cost-spend).
                from daytrader.live.healthcheck import check_providers
                self._json({"results": check_providers()})
                return
            if path == "/api/settings":
                try:
                    body = json.loads(self._read_body().decode("utf-8") or "{}")
                except Exception:  # noqa: BLE001
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                self._json(settings.save(body))
                return
            if path == "/api/devrequest/close":
                try:
                    data = json.loads(self._read_body().decode("utf-8") or "{}")
                except Exception:  # noqa: BLE001
                    data = {}
                self._json(close_dev_request(
                    data.get("team", ""), data.get("id"),
                    status=data.get("status", "closed"),
                    resolution=data.get("resolution", "closed from dashboard"),
                ))
                return
            if path.startswith("/api/team/") and path.endswith("/chat"):
                middle = path[len("/api/team/"):-len("/chat")]
                name = urllib.parse.unquote(middle.strip("/"))
                if name not in team_names():
                    self._json({"ok": False, "reply": "", "error": f"unknown team {name!r}"}, 404)
                    return
                try:
                    data = json.loads(self._read_body().decode("utf-8") or "{}")
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
def _version() -> str:
    """Running version, read from the VERSION file (repo root or /app)."""
    from pathlib import Path
    for p in (Path(__file__).resolve().parents[2] / "VERSION", Path("/app/VERSION")):
        try:
            v = p.read_text().strip()
            if v:
                return v
        except Exception:  # noqa: BLE001
            continue
    return "?"


def _render_page() -> str:
    """Fill the static page with the live version + starting-cash values so the
    header never goes stale."""
    try:
        from daytrader.live.competition import START_CASH
        cash = int(START_CASH)
    except Exception:  # noqa: BLE001
        cash = 25000
    return (PAGE_HTML
            .replace("__VERSION__", _version())
            .replace("__START_CASH_NUM__", str(cash))
            .replace("__START_CASH__", f"{cash:,}"))


def _make_server(port: int = 8787) -> ThreadingHTTPServer:
    """Build (but do not start) the ThreadingHTTPServer for the dashboard.

    Used by both ``serve()`` and the self-test. Building the server does NOT
    start the trading loop, so it is cheap and side-effect-free.
    """
    import os
    bind = os.environ.get("DASHBOARD_BIND", "0.0.0.0")
    return ThreadingHTTPServer((bind, port), _Handler)


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
  html{-webkit-text-size-adjust:100%}
  html,body{max-width:100%;overflow-x:hidden}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--line);position:relative}
  header h1{margin:0;font-size:20px;letter-spacing:.3px}
  header .sub{color:var(--gray);font-size:12px;margin-top:4px}
  header .ver{position:absolute;top:16px;right:24px;color:var(--gray);
       font-size:12px;background:#17171c;border:1px solid var(--line);
       padding:3px 9px;border-radius:999px;font-variant-numeric:tabular-nums}
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

  /* ── phones / narrow screens ───────────────────────────────────────────
     Tighten chrome, stack the version badge above the title, and let wide
     data tables (esp. the 10-col leaderboard + the trades "Reason" column)
     scroll INSIDE their card instead of forcing the whole page sideways.
     The card is the scroll container; the page itself never overflows. */
  @media (max-width:640px){
    header{padding:14px 16px 12px}
    header h1{font-size:17px}
    header .sub{font-size:11px}
    header .ver{position:static;display:inline-block;margin-bottom:8px}
    .tabs{padding:8px 10px 0;gap:4px;overflow-x:auto;flex-wrap:nowrap;
          -webkit-overflow-scrolling:touch}
    .tab{padding:7px 12px;font-size:12px;white-space:nowrap;flex:0 0 auto}
    main{padding:14px 12px 56px;max-width:100%}
    /* the CARD scrolls its overflowing table; tables keep their natural
       (nowrap) width and scroll within the card, not the page */
    .card{padding:12px;border-radius:10px;max-width:100%;
          overflow-x:auto;-webkit-overflow-scrolling:touch}
    table{min-width:100%}
    th,td{padding:7px 8px}
    /* the long free-text trade reason is in the activity feed too — hide it
       from the table on phones so the essentials fit without scrolling */
    .col-reason{display:none}
    .stats{gap:14px 18px}
    .stat{min-width:80px}
    .stat .v{font-size:18px}
    canvas{height:260px}
    .msg{max-width:88%}
    .chatbar input{font-size:16px}  /* 16px stops iOS Safari zoom-on-focus */
  }
</style>
</head>
<body>
<header>
  <span class="ver" title="Running version">v__VERSION__</span>
  <h1>AI Trading Desk Competition</h1>
  <div class="sub">Four AI desks &mdash; Claude, OpenAI, Grok, Qwen &mdash; each start with
    <b>$__START_CASH__</b> in an identical paper account. May the best model win.</div>
</header>
<div class="tabs" id="tabs"></div>
<main id="main"></main>

<script>
"use strict";
const TEAMS = ["claude","openai","grok","qwen"];
const LABELS = {claude:"Claude", openai:"OpenAI", grok:"Grok", qwen:"Qwen"};
const COLORS = {claude:"#36d399", openai:"#7c8cff", grok:"#f87272", qwen:"#ffd479"};
const START = __START_CASH_NUM__;
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
function fmtWhen(ts){
  if(!ts) return "";
  const raw = String(ts);
  const stamp = raw.replace("T"," ").slice(5,16);  // MM-DD HH:MM
  const d = new Date(raw);
  if(isNaN(d.getTime())) return stamp;
  const diff = (Date.now() - d.getTime())/1000;
  let rel;
  if(diff < 0) rel = "just now";
  else if(diff < 60) rel = "just now";
  else if(diff < 3600) rel = Math.floor(diff/60)+"m ago";
  else if(diff < 86400) rel = Math.floor(diff/3600)+"h ago";
  else rel = Math.floor(diff/86400)+"d ago";
  return stamp + " (" + rel + ")";
}
function safeUrl(u){ u = String(u==null?"":u); return /^https?:\/\//i.test(u) ? u : null; }
function fmtPF(v, n){ if(v===null||v===undefined) return (Number(n)>0?"∞":"—"); return Number(v).toFixed(2); }
function dashToken(){ try{ return localStorage.getItem("dashToken")||""; }catch(e){ return ""; } }
function authHeaders(extra){ const h = Object.assign({}, extra||{}); const t = dashToken(); if(t) h["X-Dashboard-Token"]=t; return h; }
async function apiFetch(url, opts){
  opts = opts || {}; opts.cache = "no-store"; opts.headers = authHeaders(opts.headers);
  let r = await fetch(url, opts);
  if(r.status === 401){
    const t = prompt("This dashboard requires a token (DASHBOARD_TOKEN). Enter it:");
    if(t){ try{ localStorage.setItem("dashToken", t); }catch(e){} opts.headers = authHeaders(opts.headers); r = await fetch(url, opts); }
  }
  return r;
}
async function getJSON(url){ const r = await apiFetch(url); return await r.json(); }

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
  mk("health", "Health");
  mk("settings", "Settings");
}
function switchTab(id){
  current = id;
  if(refreshTimer){ clearInterval(refreshTimer); refreshTimer = null; }
  buildTabs();
  if(id === "overview") loadOverview();
  else if(id === "settings") loadSettings();
  else if(id === "health") loadHealth();
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
      el("td", null, fmtPF(s.profit_factor, s.n_trades)),
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
    el("span",{class:"sw", style:"background:var(--gray)"}),
    "$"+START.toLocaleString()+" baseline"));
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
  mkStat("Profit factor", fmtPF(row.profit_factor, row.n_trades));
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
    ["Symbol","Side","Qty","Entry","Stop","Target","Hold","Strategy"].forEach(x=>h.appendChild(el("th",null,x)));
    t.appendChild(h);
    positions.forEach(p => {
      const tr = el("tr");
      const hz = (p.horizon||"day");
      [el("td",null,p.symbol||""),
       el("td",{class: (p.side||"").toLowerCase()==="short"?"red":"green"}, (p.side||"").toUpperCase()),
       el("td",null,String(p.qty!=null?p.qty:"")),
       el("td",null,p.entry_price!=null?fmtMoney(p.entry_price):""),
       el("td",null,(p.stop!=null?fmtMoney(p.stop):"—") + ((p.trail_atr_mult||p.trail_pct)?" ⤴":"")),
       el("td",null,p.target!=null?fmtMoney(p.target):"—"),
       el("td",{class: hz==="day"?"gray":"green"}, hz),
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
    ["Symbol","Side","Entry → Exit","Qty","P&L"].forEach(x=>h.appendChild(el("th",null,x)));
    h.appendChild(el("th",{class:"col-reason"},"Reason"));
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
       el("td",{class:"gray col-reason"}, tr0.exit_reason||"")].forEach(c=>tr.appendChild(c));
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
      const meta = el("div",{class:"meta", style:"display:flex;align-items:center;gap:10px;justify-content:space-between"});
      const left = el("div",{style:"display:flex;align-items:center;gap:8px;flex-wrap:wrap"});
      left.appendChild(el("span",{class:"pill"}, "#"+(d.id!=null?d.id:"?")+" "+(d.status||"open")));
      if(d.ts) left.appendChild(el("span",{class:"gray", style:"font-size:11px"}, fmtWhen(d.ts)));
      meta.appendChild(left);
      meta.appendChild(el("button",{class:"btn-ghost", style:"padding:4px 10px;font-size:11px",
        onclick:()=>closeDevReq(name, d.id)}, "Mark done"));
      item.appendChild(meta);
      const title = el("div",{class:"body"});
      title.appendChild(document.createTextNode(d.title||"(untitled)"));
      if(safeUrl(d.url)){
        title.appendChild(document.createTextNode("  "));
        title.appendChild(el("a",{href:safeUrl(d.url), target:"_blank", rel:"noopener"}, "link"));
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
    const r = await apiFetch("/api/team/" + encodeURIComponent(name) + "/chat", {
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

async function closeDevReq(team, id){
  if(id == null) return;
  try{
    await apiFetch("/api/devrequest/close", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({team: team, id: id, status: "closed",
                            resolution: "marked done from dashboard"}),
    });
  }catch(e){ /* ignore; reload reflects truth */ }
  if(current === team) loadTeam(team);
  else if(current === "health") loadHealth();
}

// ---- health ------------------------------------------------------------- //
async function loadHealth(){
  const main = document.getElementById("main");
  if(current !== "health") return;
  let h;
  try{ h = await getJSON("/api/health"); }
  catch(e){ main.innerHTML = ""; main.appendChild(el("div",{class:"err"}, "Failed to load: "+e)); return; }
  if(current !== "health") return;
  main.innerHTML = "";

  // System status
  const sys = el("div", {class:"card"});
  sys.appendChild(el("h2", null, "System"));
  const mk = (label, val, ok) => {
    const r = el("div", {style:"display:flex;justify-content:space-between;padding:4px 0;font-size:13px"});
    r.appendChild(el("span", {class:"muted"}, label));
    r.appendChild(el("span", {style:"color:"+(ok?"var(--green)":"var(--gray)")}, val));
    return r;
  };
  sys.appendChild(mk("Market", h.market_open ? "OPEN" : "closed", h.market_open));
  sys.appendChild(mk("Stock/ETF data (Yahoo)", h.data_feed.yahoo ? "ok" : "down", h.data_feed.yahoo));
  sys.appendChild(mk("tastytrade data", h.data_feed.tastytrade_configured ? "connected" : "not configured", h.data_feed.tastytrade_configured));
  sys.appendChild(mk("As of", new Date(h.now_et).toLocaleString(), true));
  main.appendChild(sys);

  // Per-team status
  const tc = el("div", {class:"card"});
  tc.appendChild(el("h2", null, "Teams"));
  const tbl = el("table", {style:"width:100%;border-collapse:collapse;font-size:13px"});
  const hdr = el("tr");
  ["Team","Model","Key","Equity","Errors today","Halted","Open","Last activity"].forEach(x =>
    hdr.appendChild(el("th",{style:"text-align:left;padding:6px 8px;color:var(--gray);border-bottom:1px solid var(--line)"},x)));
  tbl.appendChild(hdr);
  (h.teams||[]).forEach(t => {
    const tr = el("tr");
    const cells = [
      t.team, t.model,
      t.configured ? "✓" : "—",
      "$"+Number(t.equity).toLocaleString(),
      String(t.errors_today),
      t.halted ? "YES" : "no",
      String(t.open_positions),
      t.last_cycle ? new Date(t.last_cycle).toLocaleTimeString() : "—",
    ];
    cells.forEach((c,i) => {
      let color = "var(--txt)";
      if(i===2) color = t.configured ? "var(--green)" : "var(--gray)";
      if(i===4 && t.errors_today>0) color = "var(--red)";
      if(i===5 && t.halted) color = "var(--red)";
      tr.appendChild(el("td",{style:"padding:6px 8px;color:"+color}, c));
    });
    tbl.appendChild(tr);
  });
  tc.appendChild(tbl);
  main.appendChild(tc);

  // Live API test
  const test = el("div", {class:"card"});
  test.appendChild(el("h2", null, "API connectivity (live test)"));
  test.appendChild(el("div",{class:"muted",style:"font-size:12px;margin-bottom:10px"},
    "Pings each team's model with its key. Costs a tiny API call per team."));
  const tb = el("button", {id:"healthTestBtn", onclick:()=>testConnections("healthTestBtn","healthTestResults")}, "Test APIs now");
  test.appendChild(tb);
  test.appendChild(el("div", {id:"healthTestResults", style:"margin-top:12px"}));
  main.appendChild(test);

  // Recent errors
  const ec = el("div", {class:"card"});
  ec.appendChild(el("h2", null, "Recent errors & refusals"));
  const errs = h.recent_errors || [];
  if(!errs.length){ ec.appendChild(el("div",{class:"muted",style:"font-size:13px"}, "None recorded. 🎉")); }
  else errs.forEach(e => {
    const row = el("div", {style:"padding:6px 0;border-bottom:1px solid var(--line);font-size:12px"});
    row.appendChild(el("span", {class:"red"}, "["+e.team+"/"+e.agent+"] "));
    row.appendChild(el("span", {class:"muted"}, (e.ts||"")+" — "));
    row.appendChild(el("span", null, e.detail||e.action));
    ec.appendChild(row);
  });
  main.appendChild(ec);

  // Dev requests
  const dc = el("div", {class:"card"});
  dc.appendChild(el("h2", null, "Open dev requests (agents asking for help)"));
  const dev = h.open_dev_requests || [];
  if(!dev.length){ dc.appendChild(el("div",{class:"muted",style:"font-size:13px"}, "None.")); }
  else dev.forEach(d => {
    const row = el("div", {style:"padding:6px 0;border-bottom:1px solid var(--line);font-size:13px;display:flex;align-items:center;justify-content:space-between;gap:10px"});
    const left = el("div");
    left.appendChild(el("span",{class:"muted"}, "["+d.team+" #"+(d.id!=null?d.id:"?")+"] "));
    if(safeUrl(d.url)) left.appendChild(el("a",{href:safeUrl(d.url),target:"_blank",style:"color:var(--green)"}, d.title||"(issue)"));
    else left.appendChild(el("span", null, d.title||"(request)"));
    if(d.ts) left.appendChild(el("span",{class:"gray", style:"font-size:11px;margin-left:8px"}, fmtWhen(d.ts)));
    row.appendChild(left);
    if(d.id!=null) row.appendChild(el("button",{class:"btn-ghost",
      style:"padding:4px 10px;font-size:11px;flex:0 0 auto",
      onclick:()=>closeDevReq(d.team, d.id)}, "Mark done"));
    dc.appendChild(row);
  });
  main.appendChild(dc);

  if(!refreshTimer && current === "health"){
    refreshTimer = setInterval(() => { if(current==="health") loadHealth(); }, 20000);
  }
}

// ---- settings ----------------------------------------------------------- //
async function loadSettings(){
  const main = document.getElementById("main");
  if(current !== "settings") return;
  let status;
  try{ status = await getJSON("/api/settings"); }
  catch(e){ main.innerHTML = ""; main.appendChild(el("div",{class:"err"}, "Failed to load: "+e)); return; }
  if(current !== "settings") return;
  renderSettings(main, status);
}

// field registry: maps each input id to {key, secret}
let _settingsFields = [];

function settingsSecretField(status, key, label, placeholder){
  const st = status[key] || {};
  const wrap = el("div", {style:"margin-bottom:14px"});
  const head = el("div", {style:"display:flex;align-items:center;gap:10px;margin-bottom:4px"});
  head.appendChild(el("label", {class:"k", style:"font-size:11px;color:var(--gray);text-transform:uppercase;letter-spacing:.5px"}, label));
  if(st.set){
    head.appendChild(el("span", {class:"green", style:"font-size:11px"}, "set (" + (st.hint||"••••") + ")"));
  }
  wrap.appendChild(head);
  const id = "set_" + key;
  const input = el("input", {type:"password", id:id, autocomplete:"off",
    placeholder: placeholder || "leave blank to keep",
    style:"width:100%;background:#0e0e11;border:1px solid var(--line);color:var(--txt);padding:9px 12px;border-radius:8px;font-size:13px"});
  wrap.appendChild(input);
  _settingsFields.push({id:id, key:key, secret:true});
  return wrap;
}

function settingsTextField(status, key, label, opts){
  opts = opts || {};
  const st = status[key] || {};
  const wrap = el("div", {style:"margin-bottom:14px"});
  wrap.appendChild(el("label", {class:"k", style:"display:block;font-size:11px;color:var(--gray);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px"}, label));
  const id = "set_" + key;
  const cur = st.value != null ? st.value : "";
  let input;
  if(opts.options){
    input = el("select", {id:id,
      style:"width:100%;background:#0e0e11;border:1px solid var(--line);color:var(--txt);padding:9px 12px;border-radius:8px;font-size:13px"});
    const chosen = cur || opts.def || "";
    opts.options.forEach(o => {
      const op = el("option", {value:o}, o);
      if(o === chosen) op.setAttribute("selected", "selected");
      input.appendChild(op);
    });
  } else {
    input = el("input", {type:"text", id:id, value: cur || opts.def || "",
      placeholder: opts.placeholder || "",
      style:"width:100%;background:#0e0e11;border:1px solid var(--line);color:var(--txt);padding:9px 12px;border-radius:8px;font-size:13px"});
  }
  wrap.appendChild(input);
  if(opts.hint) wrap.appendChild(el("div", {class:"muted", style:"font-size:11px;margin-top:4px"}, opts.hint));
  _settingsFields.push({id:id, key:key, secret:false});
  return wrap;
}

function renderSettings(main, status){
  _settingsFields = [];
  main.innerHTML = "";

  // Team API keys
  const keyCard = el("div", {class:"card"});
  keyCard.appendChild(el("h2", null, "Team API keys"));
  keyCard.appendChild(settingsSecretField(status, "ANTHROPIC_API_KEY", "Team Claude (ANTHROPIC_API_KEY)"));
  keyCard.appendChild(settingsSecretField(status, "OPENAI_API_KEY", "Team OpenAI (OPENAI_API_KEY)"));
  keyCard.appendChild(settingsSecretField(status, "XAI_API_KEY", "Team Grok (XAI_API_KEY)"));
  keyCard.appendChild(settingsSecretField(status, "DASHSCOPE_API_KEY", "Team Qwen (DASHSCOPE_API_KEY)"));
  main.appendChild(keyCard);

  // Connection test
  const testCard = el("div", {class:"card"});
  testCard.appendChild(el("h2", null, "Test team connections"));
  testCard.appendChild(el("div", {class:"muted", style:"font-size:12px;margin-bottom:10px"},
    "Pings each team's model with its current key to confirm it responds. Save your keys first."));
  const testBtn = el("button", {id:"testConnBtn"}, "Test connections");
  testBtn.addEventListener("click", testConnections);
  testCard.appendChild(testBtn);
  testCard.appendChild(el("div", {id:"testResults", style:"margin-top:12px"}));
  main.appendChild(testCard);

  // Model / endpoint overrides
  const modelCard = el("div", {class:"card"});
  modelCard.appendChild(el("h2", null, "Model / endpoint overrides (optional)"));
  modelCard.appendChild(settingsTextField(status, "CLAUDE_MODEL", "CLAUDE_MODEL"));
  modelCard.appendChild(settingsTextField(status, "OPENAI_MODEL", "OPENAI_MODEL"));
  modelCard.appendChild(settingsTextField(status, "XAI_MODEL", "XAI_MODEL"));
  modelCard.appendChild(settingsTextField(status, "QWEN_MODEL", "QWEN_MODEL", {
    hint:"To run Qwen locally, set QWEN_BASE_URL to your local OpenAI-compatible server (vLLM/Ollama/LM Studio) and put any placeholder in DASHSCOPE_API_KEY."}));
  modelCard.appendChild(settingsTextField(status, "OPENAI_BASE_URL", "OPENAI_BASE_URL"));
  modelCard.appendChild(settingsTextField(status, "XAI_BASE_URL", "XAI_BASE_URL"));
  modelCard.appendChild(settingsTextField(status, "QWEN_BASE_URL", "QWEN_BASE_URL"));
  main.appendChild(modelCard);

  // Alpaca
  const alpacaCard = el("div", {class:"card"});
  alpacaCard.appendChild(el("h2", null, "Alpaca (for live/realistic data + options, once integrated)"));
  alpacaCard.appendChild(el("div", {class:"muted", style:"font-size:12px;margin-bottom:12px"},
    "Alpaca paper account keys. The full Alpaca integration is coming; entering these now prepares for it."));
  alpacaCard.appendChild(settingsSecretField(status, "ALPACA_API_KEY", "ALPACA_API_KEY"));
  alpacaCard.appendChild(settingsSecretField(status, "ALPACA_SECRET_KEY", "ALPACA_SECRET_KEY"));
  alpacaCard.appendChild(settingsTextField(status, "ALPACA_PAPER", "ALPACA_PAPER", {def:"true"}));
  alpacaCard.appendChild(settingsTextField(status, "ALPACA_DATA_PLAN", "ALPACA_DATA_PLAN", {options:["free","plus"], def:"free"}));
  main.appendChild(alpacaCard);

  // tastytrade (READ-ONLY market data)
  const ttCard = el("div", {class:"card"});
  ttCard.appendChild(el("h2", null, "Brokerage data — tastytrade (READ-ONLY market data)"));
  ttCard.appendChild(el("div", {class:"muted", style:"font-size:12px;margin-bottom:12px"},
    "Live stock + option quotes and Greeks for the teams. READ-ONLY — the desks never place trades on your tastytrade account; all orders are simulated in the paper books. Uses OAuth (works with 2FA accounts; no rolling code needed). On tastytrade.com: My Profile → API → OAuth Applications → Manage → Create Grant — copy the client secret and the generated refresh token here."));
  ttCard.appendChild(settingsSecretField(status, "TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_CLIENT_SECRET"));
  ttCard.appendChild(settingsSecretField(status, "TASTYTRADE_REFRESH_TOKEN", "TASTYTRADE_REFRESH_TOKEN"));
  main.appendChild(ttCard);

  // Research data providers (optional — the desks call these on demand)
  const dataCard = el("div", {class:"card"});
  dataCard.appendChild(el("h2", null, "Research data providers (optional)"));
  dataCard.appendChild(el("div", {class:"muted", style:"font-size:12px;margin-bottom:12px"},
    "Extra data the desks can look up on demand to find an edge: options flow, news, screeners, congressional/insider activity. Add a key to enable that source for all teams; leave blank to skip."));
  dataCard.appendChild(settingsSecretField(status, "POLYGON_API_KEY", "POLYGON_API_KEY (quotes / news / aggregates / movers)"));
  dataCard.appendChild(settingsSecretField(status, "UNUSUAL_WHALES_API_KEY", "UNUSUAL_WHALES_API_KEY (options flow / dark pool)"));
  dataCard.appendChild(settingsSecretField(status, "BULLFLOW_API_KEY", "BULLFLOW_API_KEY (options flow alerts)"));
  dataCard.appendChild(settingsSecretField(status, "QUIVER_API_KEY", "QUIVER_API_KEY (congress / insider / WSB / gov contracts)"));
  dataCard.appendChild(settingsSecretField(status, "FINVIZ_AUTH_TOKEN", "FINVIZ_AUTH_TOKEN (Elite screener export)"));
  main.appendChild(dataCard);

  // Other
  const otherCard = el("div", {class:"card"});
  otherCard.appendChild(el("h2", null, "Other (optional)"));
  otherCard.appendChild(settingsSecretField(status, "GITHUB_TOKEN", "GITHUB_TOKEN (for filing dev-request issues)"));
  otherCard.appendChild(settingsTextField(status, "GITHUB_REPO", "GITHUB_REPO"));
  otherCard.appendChild(settingsSecretField(status, "DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"));
  main.appendChild(otherCard);

  // Save bar + notes
  const saveCard = el("div", {class:"card"});
  const saveBtn = el("button", {id:"settingsSave"}, "Save settings");
  saveBtn.addEventListener("click", saveSettings);
  saveCard.appendChild(saveBtn);
  saveCard.appendChild(el("div", {class:"err", id:"settingsErr"}));
  saveCard.appendChild(el("div", {id:"settingsMsg", class:"green", style:"font-size:12px;margin-top:8px"}));
  saveCard.appendChild(el("div", {class:"muted", style:"font-size:11px;margin-top:12px"},
    "Keys are stored locally in this container's data volume (paper trading only) and are never committed or logged."));
  main.appendChild(saveCard);
}

async function testConnections(btnId, outId){
  if(typeof btnId !== "string") btnId = "testConnBtn";
  if(typeof outId !== "string") outId = "testResults";
  const btn = document.getElementById(btnId);
  const out = document.getElementById(outId);
  if(btn){ btn.disabled = true; btn.textContent = "Testing… (a few seconds)"; }
  if(out) out.innerHTML = "";
  try{
    const r = await (await apiFetch("/api/check", {method:"POST"})).json();
    const rows = (r && r.results) || [];
    const tbl = el("table", {style:"width:100%;border-collapse:collapse;font-size:13px"});
    const hdr = el("tr");
    ["Team","Model","Status","Latency","Detail"].forEach(h =>
      hdr.appendChild(el("th", {style:"text-align:left;padding:6px 8px;color:var(--gray);border-bottom:1px solid var(--line)"}, h)));
    tbl.appendChild(hdr);
    rows.forEach(row => {
      const tr = el("tr");
      const status = row.ok ? "✓ OK" : (row.configured ? "✗ FAIL" : "— no key");
      const color = row.ok ? "var(--green)" : (row.configured ? "var(--red)" : "var(--gray)");
      tr.appendChild(el("td", {style:"padding:6px 8px"}, row.team));
      tr.appendChild(el("td", {style:"padding:6px 8px"}, row.model));
      tr.appendChild(el("td", {style:"padding:6px 8px;color:"+color}, status));
      tr.appendChild(el("td", {style:"padding:6px 8px"}, row.configured ? (row.latency_ms+"ms") : "-"));
      tr.appendChild(el("td", {style:"padding:6px 8px;color:var(--gray)"}, row.detail || ""));
      tbl.appendChild(tr);
    });
    if(out){ out.innerHTML = ""; out.appendChild(tbl); }
  }catch(e){ if(out) out.appendChild(el("div", {class:"err"}, "Test failed: "+e)); }
  finally{ if(btn){ btn.disabled = false; btn.textContent = "Test connections"; } }
}

async function saveSettings(){
  const btn = document.getElementById("settingsSave");
  const errEl = document.getElementById("settingsErr");
  const msgEl = document.getElementById("settingsMsg");
  if(errEl) errEl.textContent = "";
  if(msgEl) msgEl.textContent = "";
  const updates = {};
  _settingsFields.forEach(f => {
    const inp = document.getElementById(f.id);
    if(!inp) return;
    const v = inp.value != null ? inp.value : "";
    if(f.secret){
      // skip blank secret inputs so stored keys stay unchanged
      if(v.trim() !== "") updates[f.key] = v.trim();
    } else {
      // include plain fields as-is
      updates[f.key] = v;
    }
  });
  if(btn) btn.disabled = true;
  try{
    const r = await apiFetch("/api/settings", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(updates),
    });
    const res = await r.json();
    if(res && res.error){
      if(errEl) errEl.textContent = res.error;
    } else if(current === "settings"){
      // re-fetch authoritative status and re-render
      const status = await getJSON("/api/settings");
      if(current === "settings"){
        renderSettings(document.getElementById("main"), status);
        const m = document.getElementById("settingsMsg");
        if(m) m.textContent = "Saved. New API keys activate their team within the next cycle (no restart needed).";
      }
    }
  }catch(e){
    if(errEl) errEl.textContent = "Request failed: " + e;
  }finally{
    const liveBtn = document.getElementById("settingsSave");
    if(liveBtn) liveBtn.disabled = false;
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
