# Polymarket AI Trading Bot

Autonomous trading agent for Polymarket prediction markets. Discovers markets, researches evidence, forecasts probabilities with a multi-model AI ensemble, and executes trades with strict risk controls.

Paper trading by default. Three safety gates must be unlocked for live orders.

> **Need help or an updated version?**  
> **Telegram:** [t.me/dexoryn777](https://t.me/dexoryn777)

---

## Why This Polymarket AI Agent

Most Polymarket tools either mirror another wallet or require you to manually research, price, and size every market. This agent runs the full loop autonomously — discovery, evidence gathering, probability forecasting, risk checks, and execution — so you can focus on configuration and oversight instead of clicking through hundreds of markets.

### End-to-end autonomy, not copy-trading

- **Independent forecasts** — a 3-model LLM ensemble (GPT-4o, Claude, Gemini) estimates probabilities without anchoring to the current market price
- **Autonomous research** — category-aware web search, HTML extraction, and domain authority scoring pull evidence from primary sources (`bls.gov`, `sec.gov`, `fec.gov`, and more)
- **Self-improving calibration** — Platt scaling, Brier score tracking, and automatic retraining after resolved markets tighten forecasts over time

### Built for serious risk control

- **15+ pre-trade checks** — drawdown heat, Kelly sizing, liquidity/spread filters, category exposure caps, and more; one failure blocks the trade
- **Triple dry-run gate** — paper trading by default; live orders require three independent unlocks
- **Whale intelligence** — tracks smart-money wallets and adjusts edge when whales agree or disagree with the model

### Production-ready observability

- **9-tab live dashboard** — engine status, positions, forecasts, risk, smart money, and performance metrics on port 2345
- **Immutable audit trail** — every decision logged with SHA-256 integrity checksums
- **Multi-channel alerts** — Telegram, Discord, and Slack for trades, drawdown warnings, and errors

### How it compares

| Capability | This AI Agent | Typical alternatives |
|------------|---------------|----------------------|
| **Autonomous research & forecasting** | Yes — full pipeline | Copy bots only mirror wallets |
| **Multi-model ensemble** | 3 LLMs with adaptive weighting | Single model or manual guesses |
| **Calibration feedback loop** | Retrains from resolved markets | Static or no calibration |
| **Risk management depth** | 15+ checks + drawdown heat | Basic limits or none |
| **Smart money integration** | Whale scanner + edge adjustment | Wallet copy only |
| **Paper trading default** | Triple safety gate | Often live-only or no guardrails |
| **Live dashboard & audit trail** | 9-tab dashboard + SHA-256 logs | CLI-only or opaque |

---

## Features

### Market Discovery & Classification

- Scans active markets from the Polymarket Gamma API with volume, liquidity, and spread filters
- 11-category classifier (MACRO, ELECTION, CORPORATE, LEGAL, TECHNOLOGY, SCIENCE, CRYPTO, REGULATION, GEOPOLITICS, SPORTS, ENTERTAINMENT) using 100+ regex rules — no LLM cost
- Each market gets a researchability score (0–100) that controls how much research budget it receives
- Pre-research quality filter blocks junk markets before any expensive API calls (~90% cost savings)
- Configurable cooldowns prevent re-scanning the same market too frequently

### Autonomous Research Engine

- Query builder generates site-restricted searches per category — `site:bls.gov` for macro, `site:sec.gov` for corporate, `site:fec.gov` for elections
- Includes contrarian queries to avoid confirmation bias
- 3 pluggable search backends — SerpAPI, Bing, Tavily — with automatic fallback if one fails
- Full HTML extraction via BeautifulSoup, not just search snippets
- Domain authority scoring — primary sources (1.0) > secondary (0.6) > unknown (0.3)
- Auto-filters low-quality domains (Wikipedia, Reddit, Medium, Twitter, TikTok)
- Source caching with configurable TTL (default 1 hour)

### Multi-Model AI Forecasting

- Ensemble of 3 frontier LLMs running in parallel:
  - GPT-4o (40% weight) — primary forecaster
  - Claude 3.5 Sonnet (35% weight) — second opinion
  - Gemini 1.5 Pro (25% weight) — third opinion
- 3 aggregation methods — trimmed mean, median, or weighted average
- Models forecast independently — explicitly told not to anchor to the current market price
- Graceful degradation — if a model fails, the ensemble continues with the remaining models
- Adaptive weighting — tracks per-model Brier scores by category and reweights over time

### Calibration & Self-Improvement

- Platt scaling — logistic compression pulling extreme probabilities toward 0.50
- Historical calibration — learns from past forecast vs. outcome pairs via logistic regression
- Evidence quality penalty — weak evidence pulls the forecast toward 0.50
- Contradiction penalty — conflicting sources increase uncertainty
- Ensemble spread penalty — when models disagree by more than 10%, adds uncertainty
- Calibration feedback loop — retrains automatically after 30+ resolved markets
- Brier score tracking — monitors forecast accuracy over time

### Risk Management (15+ Checks)

Every trade must pass all of these — a single failure blocks the trade:

- Kill switch — manual emergency halt
- Drawdown auto-kill at 20% max drawdown
- 4-level drawdown heat system:
  - Normal (< 10%) → full sizing
  - Warning (≥ 10%) → half sizing
  - Critical (≥ 15%) → quarter sizing
  - Max (≥ 20%) → all trading halted
- Max stake per market ($50 default)
- Daily loss limit ($500 default)
- Max open positions (25)
- Minimum net edge after fees (4%)
- Minimum liquidity ($2,000)
- Maximum spread (6%)
- Evidence quality threshold (0.55)
- Confidence filter (MEDIUM minimum)
- Implied probability floor (5%)
- Portfolio category exposure cap (35% per category)
- Timeline endgame check (48h near resolution)
- Arbitrage detection — scans for mispriced complementary and multi-outcome markets

### Execution Engine

- Fractional Kelly position sizing with 7 multipliers (confidence, drawdown, timeline, volatility, regime, category, liquidity)
- Auto strategy selection:
  - Simple — single limit order for small trades
  - TWAP — splits large orders into 5 time-weighted slices
  - Iceberg — shows only 20% of true order size
  - Adaptive — adjusts pricing based on orderbook depth
- Triple dry-run safety gate:
  - `dry_run` flag on each order object
  - `execution.dry_run` in config.yaml
  - `ENABLE_LIVE_TRADING` environment variable
  - All three must allow it for a real order to go through
- Fill tracker — monitors fill rate, slippage, and time-to-fill per strategy
- 6 exit strategies — dynamic stop-loss, trailing stop, hold-to-resolution, time-based exit, edge reversal, kill switch forced exit

### Whale & Smart Money Intelligence

- Wallet scanner tracks top Polymarket traders seeded from the leaderboard
- Auto-discovers top 50 wallets by profit and top 50 by volume
- Delta detection — spots new entries, exits, size increases, and decreases
- Conviction scoring — combines whale count × dollar size into a signal
- Edge integration — whales agree with model → +8% edge boost; disagree → -2% penalty
- 7-phase liquid scanner pipeline:
  - Seeds wallets from leaderboard → fetches markets → scans global trades → per-market whale scan → ranks addresses → deep wallet analysis → scores and saves to database
- API pool rotates requests across multiple endpoints with round-robin, least-loaded, or weighted-random strategies

### Market Microstructure

- Order flow imbalance across 60min, 4hr, and 24hr windows
- VWAP divergence — signals entry when price is below volume-weighted average
- Whale order detection — flags individual trades above $2,000
- Trade acceleration — detects unusual activity surges (>2× baseline)
- Book depth ratio — measures bid vs. ask pressure
- Smart entry calculator — combines all signals to recommend optimal entry price

### Real-Time Dashboard

9-tab Flask dashboard with glassmorphism dark theme on port 2345:

- **Overview** — engine status, cycle count, P&L, equity curve
- **Trading Engine** — start/stop controls, cycle history, pipeline visualization
- **Positions** — open positions with live P&L, closed trade history
- **Forecasts** — evidence breakdown, model vs. market probability, reasoning
- **Risk & Drawdown** — drawdown gauge, heat level, Kelly multiplier, exposure breakdown
- **Smart Money** — tracked wallets, conviction signals, whale activity feed
- **Liquid Scanner** — 7-phase pipeline status, discovered candidates, API pool health
- **Performance** — win rate, ROI, Sharpe, Sortino, Calmar, category breakdown, model accuracy
- **Settings** — environment status, config viewer, kill switch toggle

Protected by `DASHBOARD_API_KEY`. Auto-refreshing with live status indicators.

### Observability & Alerting

- structlog JSON logging with automatic sensitive data redaction
- Multi-channel alerts — Telegram, Discord, Slack (with cooldowns to prevent spam)
- Alert triggers — trades, drawdown warnings, kill switch activations, errors, daily summaries
- Sentry integration — optional error tracking with data scrubbing
- API cost tracking — per-call cost estimation for LLM and search usage
- JSON run reports — exportable reports saved to `reports/`

### Storage & Audit

- SQLite with WAL mode for concurrent reads and writes
- 10 automatic schema migrations
- Immutable audit trail — every decision recorded with SHA-256 integrity checksums
- TTL cache — search results (1hr), orderbook (30s), LLM responses (30min), market list (5min)
- Automated backups with rotation (max 10), triggered via `make backup`

---

## Quick Start

```
git clone https://github.com/dylanpersonguy/polymarket-ai-trading-bot.git
cd polymarket-ai-trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Add your API keys to `.env` — at minimum `OPENAI_API_KEY` and `SERPAPI_KEY`.

```
make dashboard
```

Open **http://localhost:2345**.

**Help:** Telegram [@dexoryn777](https://t.me/dexoryn777)

---

## Docker

```
cp .env.example .env
docker compose up -d
```

---

## CLI

```
bot scan --limit 20            # discover markets
bot research --market <ID>     # research a market
bot forecast --market <ID>     # full pipeline: research, forecast, risk, size
bot paper-trade --market <ID>  # simulated trade
bot trade --market <ID>        # live trade (needs ENABLE_LIVE_TRADING=true)
bot engine start               # continuous trading loop
bot engine status              # engine health
bot dashboard                  # launch dashboard
bot portfolio                  # portfolio risk report
bot drawdown                   # drawdown status
bot arbitrage                  # scan for arbitrage
bot alerts                     # alert history
```

---

## Configuration

All config lives in `config.yaml` and `.env`.

**Required keys** — `OPENAI_API_KEY`, `SERPAPI_KEY`

**Optional keys** — `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `BING_API_KEY`, `TAVILY_API_KEY`, `DASHBOARD_API_KEY`

**Live trading** — set `ENABLE_LIVE_TRADING=true` and add `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`, `POLYMARKET_PRIVATE_KEY`

---

## Safety

- Dry run by default — three independent gates (order flag, config flag, env var) must all allow live trading
- 4-level drawdown heat system — progressively cuts position sizes, halts at 20% drawdown
- No secrets in the codebase — everything via `.env`
- Docker runs as non-root user

---

## Tests

```
make test
make lint
make format
```

---

## Author & Contact

**Dexoryn Labs** — Polymarket AI trading automation

- **Telegram:** [@dexoryn777](https://t.me/dexoryn777) (fastest response)
- **WeChat:** scan to add **DexorynWe**

<p align="center">
  <img src="wechat.png" alt="WeChat QR code — scan to add DexorynWe as a friend" width="280"/>
</p>

---

## License

MIT

---

Trading on Polymarket involves **significant risk of loss**. Dexoryn is not responsible for losses from use of this software. Wallet security, configuration, and capital risk are your responsibility.

**Only trade with funds you can afford to lose.**

---

If this project helps you, consider starring the repo or opening an issue/PR. Questions: Telegram [@dexoryn777](https://t.me/dexoryn777).

*Built for the prediction market community. Not financial advice.*
