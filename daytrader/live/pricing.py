"""Per-team token pricing so we can estimate spend from usage.

Prices are USD per 1M tokens (input, output) for each team's default model as of
July 2026. They are ESTIMATES for the dashboard's cost readout — not billing.
Override per team with <TEAM>_PRICE_IN / <TEAM>_PRICE_OUT env vars if you change
models or want exact figures.
"""
from __future__ import annotations

import os

# team -> (input $/1M, output $/1M)
_DEFAULT_PRICING = {
    "claude":   (5.00, 25.00),   # claude-opus-4-8
    "openai":   (5.00, 30.00),   # gpt-5.5
    "grok":     (1.25, 2.50),    # grok-4.3
    "qwen":     (2.50, 7.50),    # qwen3.7-max
    "deepseek": (1.74, 3.48),    # deepseek-v4-pro
    "glm":      (1.40, 4.40),    # glm-5.2
    "kimi":     (0.95, 4.00),    # kimi-k2.6
}


def prices(team: str) -> tuple[float, float]:
    """(input, output) $/1M for a team, with optional env overrides."""
    pin, pout = _DEFAULT_PRICING.get(team, (0.0, 0.0))
    try:
        pin = float(os.environ.get(f"{team.upper()}_PRICE_IN", pin))
        pout = float(os.environ.get(f"{team.upper()}_PRICE_OUT", pout))
    except (TypeError, ValueError):
        pass
    return pin, pout


def cost_usd(team: str, input_tokens: float, output_tokens: float,
             cached_input_tokens: float = 0.0) -> float:
    """Estimated USD cost for a call. Cached input (when reported) is billed at
    ~10% of the input rate, matching the major providers' cache discount."""
    pin, pout = prices(team)
    fresh_in = max(0.0, float(input_tokens) - float(cached_input_tokens))
    c = (fresh_in / 1e6) * pin
    c += (float(cached_input_tokens) / 1e6) * pin * 0.1
    c += (float(output_tokens) / 1e6) * pout
    return round(c, 4)
