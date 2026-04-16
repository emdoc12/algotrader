#!/usr/bin/env bash
set -euo pipefail

# ── Write .env for the Python engine from environment variables ───────────────
# This lets Unraid pass all credentials as container variables
# rather than requiring a mounted .env file

ENV_FILE="/app/engine/.env"

cat > "$ENV_FILE" <<EOF
TT_USERNAME=${TT_USERNAME:-}
TT_PASSWORD=${TT_PASSWORD:-}
TT_IS_SANDBOX=${TT_IS_SANDBOX:-false}
KRAKEN_API_KEY=${KRAKEN_API_KEY:-}
KRAKEN_API_SECRET=${KRAKEN_API_SECRET:-}
BULLFLOW_API_KEY=${BULLFLOW_API_KEY:-}
API_BASE_URL=http://localhost:5000
DRY_RUN=${DRY_RUN:-true}
LOG_LEVEL=${LOG_LEVEL:-INFO}
EOF

# ── Ensure data directory exists and set DB path ─────────────────────────────
export DATABASE_URL="/app/data/data.db"

echo ""
echo "  AlgoTrader starting..."
echo "  DRY_RUN: ${DRY_RUN:-true}"
echo "  Tastytrade: ${TT_USERNAME:-NOT SET}"
echo "  Kraken: $([ -n "${KRAKEN_API_KEY:-}" ] && echo 'configured' || echo 'NOT SET')"
echo "  Bullflow: $([ -n "${BULLFLOW_API_KEY:-}" ] && echo 'configured' || echo 'NOT SET')"
echo ""

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/algotrader.conf
