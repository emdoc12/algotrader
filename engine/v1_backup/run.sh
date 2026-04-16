#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# AlgoTrader Python Engine — Run Script
# ─────────────────────────────────────────────────────────────
# Usage:
#   ./run.sh              — start engine (reads .env)
#   DRY_RUN=false ./run.sh — start engine in LIVE mode
#
# Prerequisites:
#   1. pip install -r requirements.txt
#   2. cp .env.example .env && fill in TT_USERNAME / TT_PASSWORD
#   3. Ensure the AlgoTrader Node.js web app is running on port 5000
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Sanity checks ──────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo ""
  echo "  ⚠️  .env file not found."
  echo "  Copy .env.example to .env and fill in your credentials:"
  echo ""
  echo "      cp .env.example .env"
  echo ""
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "  ❌  python3 not found. Install Python 3.11+ and try again."
  exit 1
fi

PYTHON=$(command -v python3)

# Check tastytrade is installed
if ! "$PYTHON" -c "import tastytrade" 2>/dev/null; then
  echo ""
  echo "  📦  Dependencies not installed. Running pip install..."
  "$PYTHON" -m pip install -r requirements.txt --quiet
fi

# ── Print banner ──────────────────────────────────────────
echo ""
echo "  ██████╗ ██╗      ██████╗  ██████╗ ████████╗██████╗  █████╗ ██████╗ ███████╗██████╗ "
echo "  ██╔══██╗██║     ██╔════╝ ██╔═══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗"
echo "  ███████║██║     ██║  ███╗██║   ██║   ██║   ██████╔╝███████║██║  ██║█████╗  ██████╔╝"
echo "  ██╔══██║██║     ██║   ██║██║   ██║   ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝  ██╔══██╗"
echo "  ██║  ██║███████╗╚██████╔╝╚██████╔╝   ██║   ██║  ██║██║  ██║██████╔╝███████╗██║  ██║"
echo "  ╚═╝  ╚═╝╚══════╝ ╚═════╝  ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝╚═╝  ╚═╝"
echo ""
echo "  Python Strategy Engine"
echo ""

# ── Load .env and print dry-run status ────────────────────
set -o allexport
source .env
set +o allexport

DRY_RUN="${DRY_RUN:-true}"
if [ "$DRY_RUN" = "true" ]; then
  echo "  🟡  DRY RUN mode — no real orders will be placed."
else
  echo "  🔴  LIVE mode — real orders WILL be placed!"
fi
echo ""
echo "  API base: ${API_BASE_URL:-http://localhost:5000}"
echo "  Sandbox:  ${TT_IS_SANDBOX:-false}"
echo ""
echo "  Starting engine... (Ctrl+C to stop)"
echo ""

# ── Launch ─────────────────────────────────────────────────
exec "$PYTHON" engine.py
