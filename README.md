# AI-Powered Forex Confluence Analyst & Alert System

A market-analysis and high-confluence entry alert platform for EUR/USD, GBP/USD, and
XAU/USD (extensible to more instruments), integrating with TradingView.

> **This system does not place live trades.** It analyses markets, scores setups for
> confluence, and alerts the user (Telegram / WhatsApp / Email / Dashboard / TradingView)
> when a high-probability setup is confirmed. The human is always the final decision-maker.

---

## Philosophy

> "Only identify and alert on trades when multiple independent forms of analysis align.
> If the market is unclear, do nothing."

`NO TRADE` / `WAIT` are first-class, frequently-correct outputs. The system is built to
avoid overtrading, avoid claiming certainty, and to explain every decision.

---

## Project status

This is **Phase 0: scaffold**. Every module below is stubbed with real interfaces,
docstrings, and (where the logic is simple/deterministic — e.g. confluence scoring,
risk sizing) a working first implementation. Modules that need real market-data
provider wiring, ML, or Pine Script deployment are marked `# TODO` with a clear
description of what's needed.

See `PHASES.md` for the incremental build plan and what's implemented vs. stubbed.

---

## Architecture

```
TradingView (charts, Pine Script, webhooks)
        │
        ▼
FastAPI backend (this repo)
  ├── data/            Market Data Engine (ingestion, validation, multi-timeframe)
  ├── engines/         Market Structure, Liquidity, Supply/Demand, FVG, Order Blocks,
  │                    Fibonacci, Indicators, Session Analysis, News Risk, Regime,
  │                    Confluence Scoring, AI Decision
  ├── core/             Risk management, correlation management, security
  ├── services/         Signal generation, alerting, backtesting, paper trading, performance
  ├── models/           Pydantic schemas + SQLAlchemy ORM models
  ├── alerts/           Telegram / WhatsApp / Email / Dashboard channels
  ├── db/               DB session + migrations
  └── api/routes/       FastAPI route modules (markets, analysis, signals, tradingview, 
                         performance, backtests)
        │
        ▼
Alert channels (TradingView, Telegram, WhatsApp, Email, Dashboard, Push)
```

---

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Pydantic v2
- **Data/analysis:** pandas, numpy, pandas-ta
- **DB:** PostgreSQL (+ TimescaleDB extension recommended for candle data)
- **Cache/queue:** Redis + Celery (for scheduled analysis runs / news polling)
- **ML (optional, later phase):** scikit-learn, XGBoost
- **Frontend:** React + TypeScript + Tailwind (not scaffolded yet — see `frontend/README.md`)
- **Chart/integration:** TradingView Pine Script + webhooks

---

## Getting started

See **[SETUP.md](./SETUP.md)** for a full step-by-step walkthrough (env setup,
Docker/Postgres, Alembic migrations, running the test suite, exercising the
trading-mode/execution API, and wiring up TradingView). Quick version:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DB/Redis URLs, webhook secret, alert channel tokens
docker compose up -d   # Postgres + Redis
alembic upgrade head   # create all tables
pytest -v               # 18 tests should pass
uvicorn app.main:app --reload
```

API docs will be available at `http://localhost:8001/docs`.

---

## Repository layout

```
forex-ai-analyst/
├── README.md
├── PHASES.md
├── requirements.txt
├── .env.example
├── docker-compose.yml
├── app/
│   ├── main.py
│   ├── config.py
│   ├── api/routes/          # markets, analysis, signals, tradingview, performance, backtests
│   ├── core/                # risk.py, correlation.py, security.py
│   ├── data/                # market_data_engine.py, validators.py, providers/
│   ├── engines/             # one module per strategy in the Strategy Consortium
│   ├── models/              # schemas.py (Pydantic), db_models.py (SQLAlchemy)
│   ├── services/            # signal_service, alert_service, backtest_service,
│   │                         # paper_trading_service, performance_service
│   ├── alerts/               # telegram.py, whatsapp.py, email.py, dashboard_ws.py
│   ├── db/                  # session.py, migrations/
│   └── pine_scripts/        # .pine files for TradingView
├── tests/
└── frontend/                 # placeholder — React dashboard (future phase)
```

---

## Safety & scope notes (read before extending)

- No module in this scaffold places, modifies, or closes real broker orders. Automated
  execution is explicitly **Phase 10 — future, only after extensive validation** per the
  design spec.
- Risk management (`app/core/risk.py`) hard-blocks signal generation once daily/weekly
  loss limits or max consecutive losses are hit — this logic should never be bypassed
  even in paper trading.
- Backtesting (`app/services/backtest_service.py`) is structured to avoid look-ahead bias
  by construction (only sees data up to the current simulated bar) — preserve this
  invariant when extending it.
- The system must always be able to output `WAIT` / `NO TRADE`, and does so by default
  (`confluence_scoring.py` returns `NO_TRADE` below the configurable threshold).
