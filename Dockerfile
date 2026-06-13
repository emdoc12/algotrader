# ─────────────────────────────────────────────────────────────────────────────
# AlgoTrader — Competing Autonomous Equity Day-Trading Desks
#
# A set of AI trading desks (Claude, OpenAI, Grok, Qwen), each a full multi-agent
# team (Strategist / Trader / Reviewer) running on its OWN model with an identical
# $10,000 paper account and the same tools + data. They day-trade liquid US stocks
# and ETFs (options once a brokerage is connected), and file GitHub issues when
# they need a developer's help. Ships a web dashboard to watch them compete.
#
# Required at runtime (configure any subset — teams without a key are skipped):
#   ANTHROPIC_API_KEY    — Team Claude
#   OPENAI_API_KEY       — Team OpenAI
#   XAI_API_KEY          — Team Grok
#   DASHSCOPE_API_KEY    — Team Qwen
# Optional:
#   GITHUB_TOKEN / GITHUB_REPO   — file dev requests as real GitHub issues
#   DISCORD_WEBHOOK_URL          — push alerts
#
# Build:  docker build -t emdoc12/algotrader:latest .
# Run:    docker run -p 8787:8787 --env ANTHROPIC_API_KEY=... \
#                    -v /path/to/data:/app/data emdoc12/algotrader:latest
# Then open http://localhost:8787
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

# ── Persistent data directory (per-team SQLite DBs) ──────────────────────────
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# ── Environment defaults ─────────────────────────────────────────────────────
ENV DAYTRADER_DATA_DIR=/app/data
ENV DAYTRADER_DB_PATH=/app/data/daytrader_live.db
ENV START_EQUITY=10000
ENV GITHUB_REPO=emdoc12/algotrader
ENV PYTHONUNBUFFERED=1

EXPOSE 8787

# ── Run the dashboard + competition loop ─────────────────────────────────────
CMD ["python3", "-m", "daytrader.agent", "serve", "--port", "8787"]
