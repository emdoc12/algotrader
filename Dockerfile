# ─────────────────────────────────────────────────────────────────────────────
# AlgoTrader v4.0.0 — Multi-Agent Crypto Trading Bot
#
# Lean Python-only container for 24/7 Bitcoin trading on Kraken.
#
# Build:  docker build -t emdoc12/algotrader:latest .
# Run:    docker run --env-file engine/.env -v /path/to/data:/app/data emdoc12/algotrader:latest
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

LABEL maintainer="emdoc12"
LABEL org.opencontainers.image.title="AlgoTrader"
LABEL org.opencontainers.image.description="24/7 Bitcoin trading bot for Kraken"
LABEL org.opencontainers.image.version="4.0.0"

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ──────────────────────────────────────────────────────
COPY engine/requirements.txt ./engine/requirements.txt
RUN pip install --no-cache-dir -r engine/requirements.txt

# ── Engine code ──────────────────────────────────────────────────────────────
COPY engine/ ./engine/
COPY VERSION ./VERSION

# ── Persistent data directory (mount as Unraid volume) ───────────────────────
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# ── Environment defaults ────────────────────────────────────────────────────
ENV BOT_MODE=paper
ENV BOT_DB_PATH=/app/data/bot_data.db
ENV BOT_LOG_LEVEL=INFO
ENV PYTHONUNBUFFERED=1
ENV DASHBOARD_PORT=3737
# v4.0 Multi-Agent Model Hierarchy
ENV AI_MODEL=claude-opus-4-6
ENV HAIKU_MODEL=claude-haiku-4-5-20251001
ENV CHAT_MODEL=claude-haiku-4-5-20251001
# v4.0 Timing
ENV PM_INTERVAL_SECONDS=7200
ENV AGENT_INTERVAL_SECONDS=300
ENV MAX_WAKES_PER_DAY=6
ENV WAKE_COOLDOWN_SECONDS=1800
# v4.0.2 Discord Alerts
ENV DISCORD_WEBHOOK_URL=

# ── Dashboard port ───────────────────────────────────────────────────────────
EXPOSE 3737

# ── Health check (hits dashboard) ────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:3737/ || exit 1

# ── Run ──────────────────────────────────────────────────────────────────────
CMD ["python3", "-u", "engine/bot.py"]
