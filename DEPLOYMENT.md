# Deployment & Live Trading Guide

## Quick Start (Paper Trading)

```bash
# 1. Clone and install
git clone https://github.com/dylanpersonguy/polymarket-bot.git
cd polymarket-bot
make dev

# 2. Configure
cp .env.example .env
# Edit .env with your API keys (at minimum: OPENAI_API_KEY, SERPAPI_KEY)

# 3. Run
make dashboard
# Visit http://localhost:2345
```

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | ✅ | GPT-4o for forecasting |
| `SERPAPI_KEY` | ✅ | Web search for research |
| `ANTHROPIC_API_KEY` | Optional | Claude for ensemble |
| `GOOGLE_API_KEY` | Optional | Gemini for ensemble |
| `POLYMARKET_API_KEY` | Live only | Polymarket CLOB API |
| `POLYMARKET_API_SECRET` | Live only | CLOB API secret |
| `POLYMARKET_API_PASSPHRASE` | Live only | CLOB passphrase |
| `POLYMARKET_PRIVATE_KEY` | Live only | Polygon wallet key |
| `ENABLE_LIVE_TRADING` | Live only | Set `true` for real trades |
| `DASHBOARD_API_KEY` | Optional | Protect dashboard |
| `SENTRY_DSN` | Optional | Error tracking |

### Runtime Config (`config.yaml`)

All settings are tunable via `config.yaml`. The file is hot-reloaded
every cycle — changes take effect without restarting.

Key settings to tune:
- `risk.bankroll` — your total capital
- `risk.max_stake_per_market` — max bet size
- `risk.min_edge` — minimum edge to trade (default 5%)
- `engine.cycle_interval_secs` — how often to scan (default 300s)
- `ensemble.enabled` — use multi-model forecasting

---

## Deployment Options

### Option 1: Direct (Development)

```bash
make dashboard        # Flask dev server
# or
make gunicorn         # Production WSGI server
```

### Option 2: Docker

```bash
# Build and run
docker compose up -d

# View logs
docker compose logs -f bot

# Stop
docker compose down
```

### Option 3: Systemd (Linux VPS)

Create `/etc/systemd/system/polymarket-bot.service`:

```ini
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/polymarket-bot
EnvironmentFile=/opt/polymarket-bot/.env
ExecStart=/opt/polymarket-bot/.venv/bin/gunicorn \
    --bind 0.0.0.0:2345 \
    --workers 2 --threads 4 --timeout 120 \
    src.dashboard.app:app
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot
```

---

## Going Live — Testnet First

### Step 1: Get Testnet Credentials

1. Create a Polygon Mumbai wallet (use MetaMask, testnet mode)
2. Get test MATIC from [Mumbai Faucet](https://faucet.polygon.technology/)
3. Generate Polymarket API credentials at [polymarket.com](https://polymarket.com)

### Step 2: Configure for Testnet

```bash
# In .env:
POLYMARKET_CHAIN_ID=80001          # Mumbai testnet
ENABLE_LIVE_TRADING=true
```

```yaml
# In config.yaml:
execution:
  dry_run: false
engine:
  paper_mode: false
risk:
  bankroll: 100.0                  # Small testnet amount
  max_stake_per_market: 5.0        # Tiny bets
```

### Step 3: Install CLOB Client

```bash
pip install py-clob-client
```

### Step 4: Run and Validate

```bash
make dashboard
# Monitor trades on Mumbai testnet
# Verify order placement, fills, P&L tracking
```

### Step 5: Switch to Mainnet

Once validated on testnet:

```bash
# In .env:
POLYMARKET_CHAIN_ID=137            # Polygon mainnet
# Fund wallet with real USDC on Polygon
```

```yaml
# In config.yaml — start conservative:
risk:
  bankroll: 500.0
  max_stake_per_market: 25.0
  kelly_fraction: 0.15             # Conservative Kelly
drawdown:
  max_drawdown_pct: 0.10           # Tight 10% drawdown limit
```

---

## Monitoring

### Health Checks

- `GET /health` — liveness (always returns 200)
- `GET /ready` — readiness (checks DB, engine)
- `GET /metrics` — Prometheus-compatible metrics

### Dashboard Auth

Set `DASHBOARD_API_KEY` in `.env`, then access with:
- Header: `X-API-Key: your-key`
- Query: `?api_key=your-key`

### Database Backups

```bash
make backup            # Manual backup
# Backups stored in data/backups/ (max 10, auto-pruned)
```

### Error Tracking

Set `SENTRY_DSN` in `.env` for automatic exception reporting.

---

## API Cost Estimates

| Component | Cost per cycle | Notes |
|-----------|---------------|-------|
| SerpAPI | ~$0.05-0.15 | 5-15 queries × $0.01/query |
| GPT-4o | ~$0.05-0.10 | Per market forecast |
| Claude/Gemini | ~$0.03-0.05 | If ensemble enabled |
| **Total** | **~$0.15-0.30** | Per 5-minute cycle |

Rate limiter prevents API bans. Search cache (2hr TTL) reduces
redundant queries by ~60%.
