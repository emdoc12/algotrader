from .short_put import ShortPutStrategy
from .credit_spread import CreditSpreadStrategy
from .iron_condor import IronCondorStrategy
from .covered_call import CoveredCallStrategy
from .crypto_momentum import CryptoMomentumStrategy
from .crypto_mean_reversion import CryptoMeanReversionStrategy
# options_flow_scanner is NOT in this map — engine handles it directly
# (it runs as a persistent SSE stream, not a periodic scan loop)

STRATEGY_MAP = {
    "short_put": ShortPutStrategy,
    "credit_spread": CreditSpreadStrategy,
    "iron_condor": IronCondorStrategy,
    "covered_call": CoveredCallStrategy,
    "crypto_momentum": CryptoMomentumStrategy,
    "crypto_mean_reversion": CryptoMeanReversionStrategy,
}

# Strategy types that run as long-lived SSE streams (managed separately in engine.py)
STREAMING_STRATEGY_TYPES = {"options_flow_scanner"}
