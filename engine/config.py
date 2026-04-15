"""
Configuration — loaded from .env or environment variables.
Copy .env.example to .env and fill in your credentials.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Tastytrade credentials
TT_USERNAME = os.getenv("TT_USERNAME", "")
TT_PASSWORD = os.getenv("TT_PASSWORD", "")
TT_IS_SANDBOX = os.getenv("TT_IS_SANDBOX", "false").lower() == "true"

# The AlgoTrader web app's local API base URL
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:5000")

# How many seconds between strategy scans (fallback if not set per-strategy)
DEFAULT_SCAN_INTERVAL = int(os.getenv("DEFAULT_SCAN_INTERVAL", "300"))

# Set to "true" to only simulate orders (no real executions)
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Log level
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
