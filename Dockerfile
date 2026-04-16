# ─────────────────────────────────────────────────────────────────────────────
# AlgoTrader — Single-container image for Unraid
#
# Contains:
#   - Node.js 22 (Express web server + React dashboard on port 3000)
#   - Python 3.12 (strategy engine)
#   - supervisord (runs both processes, restarts on crash)
#
# Build:  docker build -t emdoc12/algotrader:latest .
# Run:    docker run -p 3000:3000 --env-file engine/.env emdoc12/algotrader:latest
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: build the React/Vite client + bundle the Express server ─────────
FROM node:22-slim AS builder

WORKDIR /build

# Need python3 + build tools here only for any native deps during npm install
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

COPY package*.json ./
# Run scripts so better-sqlite3 compiles its native addon in the builder
RUN npm ci

COPY . .
RUN npm run build


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: lean runtime image
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-slim

LABEL maintainer="emdoc12"
LABEL org.opencontainers.image.title="AlgoTrader"
LABEL org.opencontainers.image.description="Automated trading bot — Tastytrade + Kraken + Bullflow scanner"

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    supervisor \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Node app: copy built dist + node_modules (native .node already compiled) ──
COPY --from=builder /build/dist        ./dist
COPY --from=builder /build/node_modules ./node_modules
COPY --from=builder /build/package.json ./package.json

# ── Python engine ─────────────────────────────────────────────────────────────
COPY engine/ ./engine/

RUN python3 -m venv /app/engine/.venv \
    && /app/engine/.venv/bin/pip install --upgrade pip --quiet \
    && /app/engine/.venv/bin/pip install -r /app/engine/requirements.txt --quiet

# ── Persistent data directory (mount as Unraid volume) ───────────────────────
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# ── Process supervisor ────────────────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/algotrader.conf

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
