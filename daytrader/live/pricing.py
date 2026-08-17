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
    "claude":   (5.00, 25.00),   # claude-opus-5 (same rates as Opus 4.8)
    "openai":   (5.00, 30.00),   # gpt-5.5
    # grok-4.5 AND grok-4.6 both bill $2.00 / $6.00 (verified against xAI's own
    # model pricing endpoint). This entry previously carried 1.25/2.50, which is
    # grok-4.3's rate — it understated the Grok desk's cost by 1.6x on input and
    # 2.4x on output on the dashboard's $/day readout.
    "grok":     (2.00, 6.00),    # grok-4.5 / grok-4.6
    "qwen":     (2.50, 7.50),    # qwen3.7-max
    "deepseek": (1.74, 3.48),    # deepseek-v4-pro
    "glm":      (1.40, 4.40),    # glm-5.2
    "kimi":     (0.95, 4.00),    # kimi-k3 (approx; verify)
}

# team -> cached-input $/1M, where the provider publishes it. The generic
# "10% of input" assumption is wrong by a wide margin for some providers
# (grok-4.6 caches at 25% of its input rate, not 10%), and prompt caching is
# most of the token volume here because every cycle resends the same mission.
_CACHED_PRICING = {
    "grok": 0.50,      # grok-4.6; grok-4.5 is 0.30
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


def cached_price(team: str, input_price: float) -> float:
    """Cached-input $/1M: the provider's published rate where we have it,
    otherwise the usual ~10%-of-input cache discount."""
    try:
        env = os.environ.get(f"{team.upper()}_PRICE_CACHED")
        if env:
            return float(env)
    except (TypeError, ValueError):
        pass
    return _CACHED_PRICING.get(team, input_price * 0.1)


def cost_usd(team: str, input_tokens: float, output_tokens: float,
             cached_input_tokens: float = 0.0) -> float:
    """Estimated USD cost for a call, using the provider's cached rate when known."""
    pin, pout = prices(team)
    fresh_in = max(0.0, float(input_tokens) - float(cached_input_tokens))
    c = (fresh_in / 1e6) * pin
    c += (float(cached_input_tokens) / 1e6) * cached_price(team, pin)
    c += (float(output_tokens) / 1e6) * pout
    return round(c, 4)
