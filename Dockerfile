# ─────────────────────────────────────────────────────────────────────────────
# AlgoTrader v2.0.0 — Kraken BTC Trading Bot
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
LABEL org.opencontainers.image.version="2.8.4"

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

# ── Dashboard port ───────────────────────────────────────────────────────────
EXPOSE 3737

# ── Health check (hits dashboard) ────────────────────────────────────────────
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:3737/ || exit 1

# ── Run ──────────────────────────────────────────────────────────────────────
CMD ["python3", "-u", "engine/bot.py"]
