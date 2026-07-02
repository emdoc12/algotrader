# ─────────────────────────────────────────────────────────────────────────────
# AlgoTrader — Competing Autonomous Equity Day-Trading Desks
#
# A set of AI trading desks (Claude, OpenAI, Grok, Qwen), each a full multi-agent
# team (Strategist / Trader / Reviewer) running on its OWN model with an identical
# $25,000 paper account and the same tools + data. They day-trade liquid US stocks
# and ETFs (and can swing/hold longer when warranted), and file GitHub issues when
# they need a developer's help. Ships a web dashboard to watch them compete.
#
# Required at runtime (configure any subset — teams without a key are skipped):
#   ANTHROPIC_API_KEY    — Team Claude
#   OPENAI_API_KEY       — Team OpenAI
#   XAI_API_KEY          — Team Grok
#   DASHSCOPE_API_KEY    — Team Qwen
#   DEEPSEEK_API_KEY     — Team DeepSeek (open-weight)
#   ZAI_API_KEY          — Team GLM      (open-weight)
#   MOONSHOT_API_KEY     — Team Kimi     (open-weight)
# Optional:
#   GITHUB_TOKEN / GITHUB_REPO   — file dev requests as real GitHub issues
#   DISCORD_WEBHOOK_URL          — push alerts
#   DASHBOARD_TOKEN              — require this token on the dashboard's API
#   DASHBOARD_BIND               — bind address (default 0.0.0.0)
#
# Build:  docker build -t emdoc12/algotrader:latest .
# Run:    docker run -p 3737:3737 --env ANTHROPIC_API_KEY=... \
#                    -v /path/to/data:/app/data emdoc12/algotrader:latest
# Then open http://localhost:3737
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

LABEL maintainer="emdoc12"
LABEL org.opencontainers.image.title="AlgoTrader"
LABEL org.opencontainers.image.description="Competing autonomous equity day-trading desks with a web dashboard"

WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
COPY daytrader/live/requirements.txt ./daytrader/live/requirements.txt
RUN pip install --no-cache-dir -r daytrader/live/requirements.txt

# ── Agent / trading code ─────────────────────────────────────────────────────
COPY daytrader/ ./daytrader/
COPY VERSION ./VERSION

# ── Persistent data directory (per-team SQLite DBs) ──────────────────────────
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# ── Environment defaults ─────────────────────────────────────────────────────
ENV DAYTRADER_DATA_DIR=/app/data
ENV DAYTRADER_DB_PATH=/app/data/daytrader_live.db
ENV START_EQUITY=25000
ENV GITHUB_REPO=emdoc12/algotrader
ENV PYTHONUNBUFFERED=1
# Dashboard port. Defaults to 3737 to match the legacy container so existing
# Unraid port mappings keep working. Override with DASHBOARD_PORT if desired.
ENV DASHBOARD_PORT=3737

EXPOSE 3737

# ── Health probe: cheap, DB-only /api/health (no LLM calls) ──────────────────
# (Runs as root to keep the Unraid host-mounted /app/data volume writable —
# switching to a non-root UID risks permission errors on that bind mount.)
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('DASHBOARD_PORT','3737')+'/api/health', timeout=4)" || exit 1

# ── Run the dashboard + competition loop (port from DASHBOARD_PORT) ──────────
CMD ["python3", "-m", "daytrader.agent", "serve"]
