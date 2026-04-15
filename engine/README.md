# AlgoTrader — Python Strategy Engine

This Python sidecar runs alongside the Node.js web app.
It connects to Tastytrade, scans option chains and crypto prices,
and executes orders based on the strategies you configure in the UI.

---

## Setup

### 1. Install dependencies

```bash
cd engine
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Tastytrade username and password
```

### 3. Start the web app first

```bash
cd ..          # back to algo-trader root
npm run dev    # or: NODE_ENV=production node dist/index.cjs
```

### 4. Run the engine

```bash
cd engine
python engine.py
```

---

## Configuration

All strategy parameters are configured in the AlgoTrader web UI.
The engine reads them from the API every 60 seconds — no restart needed
when you change a parameter or toggle a strategy on/off.

| Setting | Description |
|---|---|
| `DRY_RUN=true` | Validate orders but don't execute (default) |
| `DRY_RUN=false` | Live execution — real money |
| `TT_IS_SANDBOX=true` | Use Tastytrade cert environment |

**Start with `DRY_RUN=true` to verify the engine is scanning correctly before going live.**

---

## Strategy Parameters Reference

### Short Put (`short_put`)
| Param | Default | Description |
|---|---|---|
| `minDTE` | 30 | Minimum days to expiration |
| `maxDTE` | 60 | Maximum days to expiration |
| `targetDelta` | 0.30 | Target delta (absolute value for puts) |
| `minDelta` | 0.16 | Minimum delta — won't go further OTM |
| `maxDelta` | 0.35 | Maximum delta — won't go ITM |
| `minPremium` | 0.50 | Minimum mid-price premium to collect |

### Credit Spread (`credit_spread`)
| Param | Default | Description |
|---|---|---|
| `minDTE` | 30 | Min DTE |
| `maxDTE` | 60 | Max DTE |
| `shortDelta` | 0.30 | Target delta for short leg |
| `width` | 5 | Spread width in dollars |
| `minCredit` | 0.80 | Minimum net credit |
| `spreadType` | `put` | `put` or `call` |

### Iron Condor (`iron_condor`)
| Param | Default | Description |
|---|---|---|
| `minDTE` | 30 | Min DTE |
| `maxDTE` | 55 | Max DTE |
| `shortDelta` | 0.16 | Target delta for both short legs |
| `width` | 5 | Wing width in dollars |
| `minCredit` | 1.50 | Minimum total net credit (both wings) |

### Covered Call (`covered_call`)
| Param | Default | Description |
|---|---|---|
| `minDTE` | 20 | Min DTE |
| `maxDTE` | 45 | Max DTE |
| `targetDelta` | 0.30 | Target call delta |
| `minPremium` | 0.30 | Minimum premium |

### Crypto Momentum (`crypto_momentum`)
| Param | Default | Description |
|---|---|---|
| `maPeriod` | 20 | EMA period (scans) |
| `breakoutPercent` | 2.0 | % above EMA to trigger buy |
| `stopLossPercent` | 3.0 | % below entry to exit |
| `takeProfitPercent` | 6.0 | % above entry to take profits |

### Crypto Mean Reversion (`crypto_mean_reversion`)
| Param | Default | Description |
|---|---|---|
| `maPeriod` | 50 | EMA period |
| `deviationPercent` | 5.0 | % below EMA to trigger buy |
| `stopLossPercent` | 3.0 | % below entry to cut |
| `takeProfitPercent` | 4.0 | % above entry to close |

---

## Watchlists

Each strategy has its own watchlist of symbols to scan.
Add symbols from the **Strategies** page in the web UI (via the API).

For options strategies: use equity tickers like `SNDK`, `AAPL`, `SPY`.  
For crypto strategies: use pairs like `BTC/USD`, `ETH/USD`.

---

## Architecture

```
engine.py           ← main entry point, asyncio event loop
├── session_manager.py   ← Tastytrade auth + token refresh
├── api_client.py        ← reads/writes to Node.js REST API
├── order_executor.py    ← wraps SDK order placement (dry-run + live)
└── strategies/
    ├── base.py               ← BaseStrategy class
    ├── short_put.py          ← Short Put
    ├── credit_spread.py      ← Credit Spread
    ├── iron_condor.py        ← Iron Condor
    ├── covered_call.py       ← Covered Call
    ├── crypto_momentum.py    ← Crypto Momentum (EMA breakout)
    └── crypto_mean_reversion.py  ← Crypto Mean Reversion (dip buy)
```
