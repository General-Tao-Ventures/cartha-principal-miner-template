# Cartha Principal Miner Template

Backend template for running a **Cartha principal miner** that serves federated miners. This system:

- **Monitors** the Bittensor chain for epoch boundaries
- **Sweeps** accumulated ALPHA from your miner hotkey to an aggregator hotkey
- **Scores** federated miner positions and distributes rewards proportionally
- **Processes claims** from federated miners who want to withdraw their ALPHA

> **New to principal mining?** See [Registration on Cartha](#registration-on-cartha) below to understand the full workflow — deploy first, then apply to be listed.

---

## Table of Contents

- [Quick Start (Docker)](#quick-start-docker)
- [Quick Start (Bare Metal / VPS)](#quick-start-bare-metal--vps)
- [Configuration](#configuration)
- [Registration on Cartha](#registration-on-cartha)
- [Architecture](#architecture)
- [API Endpoints](#api-endpoints)
- [Database Migrations](#database-migrations)
- [Monitoring & Operations](#monitoring--operations)
- [Development](#development)

---

## Quick Start (Docker)

The fastest way to get running on any VPS. Requires Docker and Docker Compose.

```bash
# 1. Clone the template
git clone <your-fork-or-copy>
cd cartha-principal-miner-template

# 2. Configure
cp .env.example .env
# Edit .env — at minimum fill in:
#   MINER_HOTKEY, MINER_COLDKEY, AGGREGATOR_HOTKEY, WALLET_PASSWORD
#   MINER_NAME, MINER_DESCRIPTION (for the listing page)

# 3. Start everything
docker compose up -d

# 4. Check health
curl http://localhost:8100/health
# Expected: {"status":"ok"}

# 5. View logs
docker compose logs -f
```

This starts three services:

| Service | Description | Port |
|---|---|---|
| **db** | PostgreSQL 16 (Alpine) | 5432 |
| **api** | FastAPI rewards server | 8100 |
| **epoch-monitor** | Polls BT chain, triggers sweep + score | — |

### Updating

```bash
git pull
docker compose build
docker compose up -d
```

### Stopping

```bash
docker compose down          # stop containers, keep data
docker compose down -v       # stop containers AND delete database volume
```

---

## Quick Start (Bare Metal / VPS)

For operators who prefer to run directly on the host.

### Prerequisites

- Python 3.11+
- PostgreSQL 16+ (running and accessible)
- Node.js 18+ (for PM2 process manager, optional)

### Steps

```bash
# 1. Create the database
createdb principal_rewards

# 2. Install Python dependencies
pip install -e ".[bt]"
# If using GCP Secret Manager for wallet password:
# pip install -e ".[bt,gcp]"

# 3. Configure
cp .env.example .env
# Edit .env with your values (see Configuration below)

# 4. Option A: Start with PM2 (recommended for production)
npm install -g pm2
pm2 start ecosystem.config.js
pm2 save
pm2 startup    # auto-start on reboot

# 4. Option B: Start manually
# Terminal 1 — API server
uvicorn app.main:app --host 0.0.0.0 --port 8100

# Terminal 2 — Epoch monitor
python -m app.jobs.epoch_monitor
```

### Verifying

```bash
# Health check
curl http://localhost:8100/health

# Check miner info endpoint
curl http://localhost:8100/api/miner-info
```

---

## Configuration

All configuration is via environment variables (or `.env` file). See `.env.example` for the full list.

### Required

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (default: `postgresql+asyncpg://postgres:postgres@localhost:5432/principal_rewards`) |
| `VERIFIER_URL` | Cartha Verifier API URL (default: `https://api.cartha.finance`) |
| `MINER_HOTKEY` | Your miner's Bittensor hotkey (SS58) |
| `MINER_COLDKEY` | Your miner's Bittensor coldkey (SS58) |
| `AGGREGATOR_HOTKEY` | Aggregator hotkey for reward accumulation |
| `WALLET_PASSWORD` | Password to unlock your BT wallet for transfers |

### Identity (shown on the Cartha frontend)

These values are returned by `/api/miner-info` and displayed on the principal miners listing page.

| Variable | Description |
|---|---|
| `MINER_NAME` | Display name for your mining operation |
| `MINER_DESCRIPTION` | Short description of your operation |
| `MINER_WEBSITE` | Website URL (optional) |
| `MINER_DISCORD` | Discord invite link or handle (optional) |
| `MINER_LOGO_URL` | URL to a square logo image, at least 128×128px (optional) |

### Rewards

| Variable | Default | Description |
|---|---|---|
| `COMMISSION_RATE` | `0.05` | Commission on guest rewards (0.0 = 0%, 1.0 = 100%) |
| `HOME_EVM_ADDRESSES` | `[]` | JSON array of your own EVM addresses (no commission charged) |
| `MIN_CLAIM_ALPHA` | `0.001` | Minimum ALPHA amount for claim requests |

### Bittensor

| Variable | Default | Description |
|---|---|---|
| `BT_WALLET_NAME` | `default` | Bittensor wallet name on disk |
| `BT_NETWORK` | `finney` | Bittensor network (`finney`, `test`, `local`) |

### Optional

| Variable | Default | Description |
|---|---|---|
| `CORS_ORIGINS` | `["*"]` | Allowed CORS origins (JSON array) |
| `ADMIN_API_KEY` | — | API key for admin endpoints (`/api/admin/*`) |
| `SLACK_WEBHOOK_URL` | — | Slack webhook for reward distribution notifications |
| `SLACK_ERROR_WEBHOOK_URL` | — | Slack webhook for error alerts |
| `POLL_INTERVAL` | `30` | Seconds between epoch boundary polls |
| `CLAIM_COOLDOWN_SECONDS` | `60` | Minimum seconds between claims per address |
| `GCP_SECRET_ID` | — | GCP Secret Manager secret ID (alternative to `WALLET_PASSWORD`) |
| `GCP_PROJECT` | — | GCP project ID (required if using `GCP_SECRET_ID`) |

---

## Registration on Cartha

After deploying your principal miner, you need to **apply to be listed** on the Cartha frontend so federated miners can discover you and lock capital under your hotkey.

### Step 1 — Deploy and verify your miner

Make sure your API is accessible and returning valid data:

```bash
# Confirm health
curl https://your-miner-domain.com/health
# → {"status":"ok"}

# Confirm identity info is populated
curl https://your-miner-domain.com/api/miner-info
# → {"miner_hotkey":"5C...", "miner_name":"My Mining Operation", ...}
```

### Step 2 — Apply on the Cartha frontend

1. Go to [https://app.cartha.finance/principal-miners/apply](https://app.cartha.finance/principal-miners/apply)
2. Fill in the application form:
   - **Identity** — Name, description, website, Discord, logo URL (should match your `.env` values)
   - **Terms** — Commission rate, payout schedule, minimum lock requirements
   - **Operational** — Your miner hotkey (SS58), connected EVM wallet address, contact email
3. Submit the application

### Step 3 — Wait for approval

The Cartha team reviews applications and may reach out to your contact email with questions. Once approved, your miner will appear on the [Principal Miners](https://app.cartha.finance/principal-miners) listing page.

### Step 4 — Sync with the Verifier

Once approved, your miner will begin syncing with the Cartha Verifier backend automatically. The verifier calls your `/api/miner-info` endpoint to pull identity and stats for the listing page. Make sure your API stays online and your identity env vars are up to date.

### Updating your listing

To update your name, description, commission rate, or other displayed info:

1. Update the relevant env vars in your `.env` file
2. Restart your API service (`docker compose restart api` or `pm2 restart all`)
3. Changes are picked up on the next verifier sync cycle (typically within minutes)

---

## Architecture

```
┌─────────────────────────┐
│   Epoch Monitor         │  Polls BT chain every 30s
│   (background process)  │  Detects epoch boundaries
│                         │  Triggers sweep + score
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐     ┌──────────────────┐
│   FastAPI Server        │◄────│  Cartha Verifier  │
│   (port 8100)           │     │  API              │
│                         │     └──────────────────┘
│   /health               │
│   /api/miner-info       │
│   /api/rewards/:addr    │
│   /api/claims/:addr     │
│   /api/claim            │
│   /api/federated-miners │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐
│   PostgreSQL            │
│   Tables:               │
│     sweeps              │
│     reward_entries      │
│     claims              │
└─────────────────────────┘
```

### How rewards flow

1. **Epoch boundary** — The epoch monitor detects a new BT epoch
2. **Sweep** — ALPHA is transferred from your miner hotkey to the aggregator hotkey
3. **Score** — The verifier provides the list of federated miners and their locked positions; the scoring engine allocates rewards proportionally
4. **Distribution** — Reward entries are written to the database; your commission is deducted automatically
5. **Claims** — Federated miners claim their ALPHA through the `/api/claim` endpoint; transfers are executed from the aggregator hotkey

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | — | Health check |
| GET | `/api/miner-info` | — | Public miner info (identity, commission, stats) |
| GET | `/api/rewards/{evm_address}` | — | Reward summary for an EVM address |
| GET | `/api/claims/{evm_address}` | — | Claim history for an EVM address |
| GET | `/api/federated-miners` | — | List all federated miners with balances |
| POST | `/api/claim` | — | Submit a reward claim |
| POST | `/api/admin/run-scoring` | Admin key | Manually trigger sweep + score |
| GET | `/api/admin/summary` | Admin key | Admin rewards summary |

Admin endpoints require the `X-Admin-Key` header matching `ADMIN_API_KEY`.

---

## Database Migrations

Tables are auto-created on startup via `create_all_tables()`. For production schema changes, use Alembic:

```bash
# Generate a new migration after model changes
alembic revision --autogenerate -m "description"

# Apply pending migrations
alembic upgrade head

# Check current migration version
alembic current
```

---

## Monitoring & Operations

### Health checks

Set up an uptime monitor (e.g., UptimeRobot, Pingdom, or a simple cron) pointing at:

```
GET https://your-miner-domain.com/health
```

### Slack notifications

Set `SLACK_WEBHOOK_URL` to receive notifications when rewards are distributed. Set `SLACK_ERROR_WEBHOOK_URL` for error alerts (failed sweeps, transfer errors, etc.).

### Logs

```bash
# Docker
docker compose logs -f api
docker compose logs -f epoch-monitor

# PM2
pm2 logs
```

### Manual sweep + score

If you need to trigger scoring outside the normal epoch cycle:

```bash
curl -X POST https://your-miner-domain.com/api/admin/run-scoring \
  -H "X-Admin-Key: YOUR_ADMIN_API_KEY"
```

---

## Development

```bash
# Install with dev dependencies
pip install -e ".[bt,dev]"

# Run tests
python tests/test_e2e.py

# Run API in development mode (auto-reload)
uvicorn app.main:app --reload --port 8100
```

---

## License

See repository LICENSE file.
