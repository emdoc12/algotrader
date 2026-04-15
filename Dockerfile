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

FROM node:22-slim AS node-builder

WORKDIR /build

# Install dependencies
COPY package*.json ./
RUN npm ci --ignore-scripts

# Copy source and build
COPY . .
RUN npm run build


# ─────────────────────────────────────────────────────────────────────────────
# Final image: Node 22 slim + Python 3.12 + supervisord
# ─────────────────────────────────────────────────────────────────────────────
FROM node:22-slim

LABEL maintainer="emdoc12"
LABEL org.opencontainers.image.title="AlgoTrader"
LABEL org.opencontainers.image.description="Automated trading bot — Tastytrade + Kraken + Bullflow scanner"

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    supervisor \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── App directory ────────────────────────────────────────────────────────────
WORKDIR /app

# Copy built Node.js dist
COPY --from=node-builder /build/dist ./dist
COPY --from=node-builder /build/node_modules ./node_modules
COPY --from=node-builder /build/package.json ./package.json

# Copy Python engine
COPY engine/ ./engine/

# ── Python virtualenv + deps ─────────────────────────────────────────────────
RUN python3 -m venv /app/engine/.venv \
    && /app/engine/.venv/bin/pip install --upgrade pip --quiet \
    && /app/engine/.venv/bin/pip install -r /app/engine/requirements.txt --quiet

# ── Data directory (SQLite DB lives here — mount as volume) ──────────────────
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# ── supervisord config ────────────────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/algotrader.conf

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
