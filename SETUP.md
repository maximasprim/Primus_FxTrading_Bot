# Setup Guide

Everything below has been tested against this exact scaffold (Alembic migrations,
pytest suite, the paper-trading execution pipeline). Follow it top to bottom for a
working local instance with zero external API keys required.

---

## 0. Prerequisites

- **Python 3.11+**
- **Docker** (for Postgres + Redis) — or substitute a local Postgres/Redis install,
  or even SQLite for a quick spin (see step 3 note)
- A **TradingView account** (only needed once you get to step 8, Pine Script)

---

## 1. Unpack and create a virtual environment

```bash
unzip forex-ai-analyst.zip && cd forex-ai-analyst

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

---

## 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env`. For a first local run you can leave almost everything at its default —
the system is designed to run with **zero external API keys** (it uses a synthetic
data provider and a paper-trading broker adapter out of the box). The only things
worth checking immediately:

```env
DATABASE_URL=postgresql+asyncpg://forex_user:forex_pass@localhost:5432/forex_ai
DATABASE_URL_SYNC=postgresql+psycopg2://forex_user:forex_pass@localhost:5432/forex_ai
```

These match the credentials baked into `docker-compose.yml` — leave them as-is if
you use Docker for Postgres (step 3). Everything under "Trading mode & auto-execution"
is already set to the safe defaults (`AUTO_TRADING=false`, `BROKER_ADAPTER=paper`) —
**don't change these until you've read the safety section at the bottom.**

---

## 3. Set up the database (Supabase) and Redis

### 3a. Create a Supabase project

If you don't have one already: [supabase.com](https://supabase.com) → New Project.
Wait for provisioning to finish (a couple of minutes).

### 3b. Get your connection string

Dashboard → **Project Settings → Database → Connection string**. Supabase gives you
a few connection modes — this matters:

| Mode | Port | Use for |
|---|---|---|
| **Session pooler** | 5432 | Recommended for both `DATABASE_URL` and `DATABASE_URL_SYNC` — supports prepared statements, works with asyncpg out of the box |
| Direct connection | 5432 (different host) | Also fine, especially for Alembic migrations; not reachable if you're on the free tier's IPv4-restricted network in some regions |
| Transaction pooler | 6543 | Only if you specifically need pgbouncer's connection multiplexing — requires `DATABASE_USE_PGBOUNCER=true` (see below), and is generally unnecessary for a single backend process |

Start with the **session pooler** unless you have a specific reason not to.

### 3c. Fill in `.env`

```env
DATABASE_URL=postgresql+asyncpg://postgres.[PROJECT-REF]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:5432/postgres
DATABASE_URL_SYNC=postgresql+psycopg2://postgres.[PROJECT-REF]:[PASSWORD]@aws-0-[REGION].pooler.supabase.com:5432/postgres?sslmode=require
DATABASE_USE_PGBOUNCER=false
DATABASE_SSL_REQUIRED=true
```

Replace `[PROJECT-REF]`, `[PASSWORD]`, and `[REGION]` with the actual values shown
in your Supabase connection string. Two things worth knowing:

- **SSL is required** by Supabase — `DATABASE_SSL_REQUIRED=true` makes the async
  engine pass `ssl="require"` to asyncpg (see `app/db/session.py`); the sync URL
  handles it via the `?sslmode=require` query param instead, since psycopg2 reads
  that directly from the connection string.
- **If you use the transaction pooler (port 6543) instead**, set
  `DATABASE_USE_PGBOUNCER=true`. This disables asyncpg's server-side prepared
  statement cache, which is required against pgbouncer in transaction mode —
  without it, queries fail intermittently once connections start rotating.

### 3d. Start Redis

Postgres is now hosted by Supabase, so the only thing left to run locally is Redis:

```bash
docker compose up -d
```

**No Docker?** Install Redis directly, or just skip it for now — nothing in the
current scaffold hard-depends on Redis yet (it's wired in for the Celery task queue,
which isn't used by any endpoint in this scaffold yet).

**Just want to try things out before setting up Supabase at all?** You can still
point `DATABASE_URL`/`DATABASE_URL_SYNC` at local SQLite instead (`pip install
aiosqlite`, then `sqlite+aiosqlite:///./local.db` / `sqlite:///./local.db`) — the
schema in `app/models/db_models.py` is portable SQL, so this works for development.
Switch to Supabase before anything you care about persisting long-term, since
SQLite won't survive container resets and doesn't give you the managed
backups/dashboard/connection pooling Supabase provides.

---

## 4. Run database migrations

```bash
alembic upgrade head
```

This creates every table: `candles`, `signals`, `signal_events`, `backtest_runs`,
`trade_journal`, `trading_mode_audit`, `instrument_config`. The initial migration
(`app/db/migrations/versions/0001_init.py`) is already generated and committed in
this scaffold — you don't need to run `alembic revision --autogenerate` yourself
unless you change `app/models/db_models.py` later, in which case:

```bash
alembic revision --autogenerate -m "describe your change"
alembic upgrade head
```

---

## 5. Run the test suite

```bash
pytest -v
```

You should see 18 tests pass — confluence scoring math, FVG detection/mitigation,
trading-mode safety invariants (auto-trading defaults off, two-step activation,
emergency stop), and duplicate-trade protection. This is a good sanity check that
your environment is wired correctly before moving on.

---

## 6. Start the API

```bash
uvicorn app.main:app --reload
```

Open **http://localhost:8000/docs** — this is the full interactive API (Swagger UI).
Try:

- `GET /api/markets/EURUSD` — current price, bias, regime for EUR/USD (synthetic data)
- `POST /api/analysis/run?instrument=EUR/USD` — run the full multi-engine analysis
  pipeline and get back a scored `TradeSetup`
- `GET /api/trading-control/status` — see the current global trading mode, per-instrument
  overrides, and audit log

---

## 7. Try the trading-mode / execution flow

By default the system is in `ALERT_MODE` with auto-trading off — it will analyze and
alert but never place a trade. To see the full pipeline (still using the safe paper
adapter, no real money/broker involved):

```bash
# 1. Turn an instrument on for auto-trading
curl -X POST localhost:8000/api/trading-control/instrument-mode \
  -H "Content-Type: application/json" \
  -d '{"instrument": "EUR/USD", "mode": "AUTO"}'

# 2. Two-step activation — step 1: request
curl -X POST localhost:8000/api/trading-control/auto-trading/request-enable \
  -H "Content-Type: application/json" -d '{"actor": "you"}'

# 3. Two-step activation — step 2: confirm (must be the same actor)
curl -X POST localhost:8000/api/trading-control/auto-trading/confirm-enable \
  -H "Content-Type: application/json" -d '{"actor": "you"}'

# 4. Simulate a TradingView webhook event
curl -X POST localhost:8000/api/tradingview/webhook \
  -H "Content-Type: application/json" \
  -d '{
        "secret": "change-me",
        "instrument": "EUR/USD",
        "event": "approaching_zone",
        "timeframe": "5M",
        "price": 1.0810
      }'
```

The `secret` must match `TRADINGVIEW_WEBHOOK_SECRET` in your `.env`. The webhook
response includes the full `setup` (score, grade, reasoning), the `alert` dispatch
result, and — since EUR/USD is now armed for auto-trading — an `execution` result
showing whether the paper adapter placed a simulated trade or which pipeline stage
blocked it (most setups on synthetic data will correctly come back `NO_TRADE` —
that's the system working as intended, not a bug).

To stop everything immediately without touching open positions:

```bash
curl -X POST localhost:8000/api/trading-control/emergency/stop-new-trades \
  -H "Content-Type: application/json" -d '{"actor": "you"}'
```

---

## 8. Wire up TradingView (optional, once you're ready)

1. Open TradingView → Pine Editor → paste in `app/pine_scripts/confluence_overlay.pine`.
2. In the script, replace every `REPLACE_WITH_YOUR_WEBHOOK_SECRET` with your actual
   `TRADINGVIEW_WEBHOOK_SECRET` value.
3. Add the indicator to a chart, then create an Alert on one of its `alertcondition`s.
4. In the alert's **Notifications** tab, enable **Webhook URL** and point it at:
   `https://<your-deployed-backend>/api/tradingview/webhook`
   (this must be a publicly reachable URL — use something like ngrok for local testing).

---

## 9. Alert channels (optional)

Fill in whichever of these you want live in `.env`:

- **Telegram:** `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` (create a bot via @BotFather)
- **Email:** `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`ALERT_EMAIL_FROM`/`ALERT_EMAIL_TO`
- **WhatsApp:** `WHATSAPP_API_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` (Meta Cloud API) —
  note `app/alerts/whatsapp.py` still has a `TODO` for wiring the actual recipient
  number from user config rather than a placeholder.

Any channel left blank just no-ops (`{"sent": False, "reason": "... not configured"}`)
rather than erroring, so you can leave all of these empty and still run everything else.

---

## 9a. Notification engine (priority matrix, fallback, approvals)

With contact info set, you can:

```bash
# Set your contact info
curl -X POST localhost:8000/api/notifications/preferences/contact-info \
  -H "Content-Type: application/json" \
  -d '{"whatsapp_number": "+254700000000", "sms_number": "+254700000000", "email": "you@example.com"}'

# Send a test notification through the full priority/fallback pipeline
curl -X POST localhost:8000/api/notifications/test-send \
  -H "Content-Type: application/json" \
  -d '{"event_type": "confirmed_entry", "message": "Test A+ setup alert"}'

# See what was actually attempted/delivered
curl localhost:8000/api/notifications/log
```

HIGH-priority events (confirmed entries, auto-trade execution, safety shutdowns)
automatically fall back WhatsApp → SMS → Email if a channel fails — you don't
need to configure this, it's built into `app/services/notification_service.py`.

Trade approvals can also come back via WhatsApp/SMS reply (`APPROVE EURUSD
SIGNAL-ID` / `REJECT EURUSD SIGNAL-ID`) once you've pointed your WhatsApp
Business/Twilio webhook at `/api/notifications/whatsapp/inbound` or
`/sms/inbound` — the system verifies the reply comes from your configured
number before acting on it, and will refuse to approve a setup if the market
has moved too far from the original entry zone.

---

## 10. Before you touch real capital — read this

This scaffold is deliberately built so that **nothing here can move real money**
until you explicitly build/configure it to:

- `BROKER_ADAPTER` defaults to `paper`. The only other option, `mt5`, is a stub where
  every method raises `NotImplementedError` — it cannot be accidentally activated.
- `DEFAULT_AUTO_TRADING_ENABLED=false` and enabling it requires the two-step confirm
  flow from the *same actor* — see `tests/test_trading_mode.py` for the exact
  invariants this guarantees.
- Per your spec's own phased approach: **Analysis → Alerts → Paper Trading → Manual
  Approval → Limited Auto-Trading → Full Auto-Trading.** Nothing in this codebase
  skips that ladder for you — you decide when to climb it.

Next build step (not yet started): the multi-channel notification engine
(WhatsApp/SMS/email priority matrix + delivery tracking) — see `PHASES.md` for the
full status of every module.
