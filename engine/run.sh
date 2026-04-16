#!/bin/bash
# ============================================================
# AlgoTrader v2.0.0 — Kraken BTC Bot Runner
# ============================================================
# Usage:
#   ./run.sh          # Run with defaults (paper mode)
#   ./run.sh live     # Run in live mode (requires API keys in .env)
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
    echo "No .env file found. Creating from .env.example..."
    cp .env.example .env
    echo "Edit .env with your settings, then run again."
    exit 1
fi

# Override mode if passed as argument
if [ "$1" = "live" ]; then
    export BOT_MODE=live
    echo "WARNING: Running in LIVE mode — real money will be used!"
    echo "Press Ctrl+C within 5 seconds to cancel..."
    sleep 5
fi

# Install dependencies if needed
if ! python3 -c "import httpx" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

echo "Starting AlgoTrader v2.0.0..."
python3 -m bot
