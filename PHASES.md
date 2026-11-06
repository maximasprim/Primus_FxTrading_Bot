# Build Phases & Status

Legend: [x] working, tested · [~] partial / needs real credentials to fully verify · [ ] not started

| Phase | Description | Status |
|---|---|---|
| 1 | Market Data Engine (ingestion, validation, multi-timeframe storage) | [~] validators + stub provider tested; OANDA provider written to spec but **untested live** (see below) |
| 2 | Technical Analysis engines (structure, S/R, supply/demand, liquidity, FVG, order blocks, indicators) | [x] all have real detection logic, tested against synthetic OHLCV data |
| 3 | Strategy Consortium + Confluence Engine | [x] scoring engine implemented per spec §11, tested exactly against the spec's point table |
| 4 | TradingView Integration (Pine Script + webhook) | [x] webhook runs the full pipeline and persists results to the DB; example Pine Script included |
| 5 | AI Analysis Engine (scenario reasoning, explanations) | [~] works if `ANTHROPIC_API_KEY` is set; deterministic rule-based fallback otherwise (tested) |
| 6 | Backtesting | [x] no-look-ahead loop + real spread/slippage/commission cost model + walk-forward split utility, all tested |
| 7 | Signal Performance Tracking | [x] every webhook analysis now persists to `signals`/`signal_events`; aggregate queries in `performance_service.py` |
| 8 | Paper Trading | [x] `PaperTradingAdapter` fully implemented and exercised through the whole execution pipeline |
| 9 | Manual Confirmation Mode | [x] default behavior - nothing in this repo auto-executes without explicit two-step activation |
| 10 | Automated Execution | [x] full 16-stage validation pipeline, tested end-to-end against the paper adapter; MT5 adapter is a deliberate stub (see below) |
| 11 | Trading Mode Management | [x] two-step activation, emergency stop, per-instrument overrides, **DB-persisted** (survives restart; verified auto-trading still resets OFF per spec §21) |
| 12 | Multi-channel Notification Engine | [x] priority matrix, retry/fallback chain, delivery logging, approval-via-WhatsApp/SMS - all tested; webhook signature verification tested with forged vs. valid signatures |

---

## What's genuinely real vs. what still needs your own environment to verify

**Fully implemented AND tested in this sandbox** (I ran these, not just wrote them):
- Every engine (structure, FVG, liquidity, supply/demand, order blocks, indicators, regime, confluence scoring)
- The full execution pipeline (16 validation stages, paper adapter, duplicate protection, safety shutdowns)
- Trading-mode management incl. DB persistence and safe-restart behavior
- The notification engine (priority/fallback/retry/delivery-logging), now unified with `alert_service` so
  **every** `TradeSetup` alert - not just `/api/notifications/*` calls - goes through the same tracked path
- Webhook signature verification (Meta HMAC-SHA256, Twilio HMAC-SHA1) - tested against forged requests
- Redis-backed duplicate protection - tested against real `fakeredis`, with confirmed graceful fallback
  to in-memory storage if Redis is unreachable
- Real equity/drawdown tracking (`equity_curve` table) feeding the max-drawdown safety shutdown
- Signal persistence - every webhook analysis writes to `signals`/`signal_events`
- Backtest cost model (spread/slippage/commission applied to fills) + walk-forward date splitting
- All 8 DB migrations, applied and verified against a live SQLite DB from a clean slate

**Written to spec but NOT tested live** (this sandbox can only reach package registries, not third-party
APIs like oanda.com, Meta's Graph API, or Twilio):
- `app/data/providers/oanda_provider.py` - structurally validated (instrument-name mapping, error handling
  on missing credentials, correct base URL construction) but never made a real API call. **Before trusting
  this for real analysis, get a free OANDA practice account and smoke-test each method individually** -
  see the provider's module docstring for a step-by-step checklist.
- Live WhatsApp/Twilio sends - the code paths are correct and exercised via mocked failure responses
  (since no credentials are configured here), but a real successful send has never been observed.

**Intentionally NOT implemented:**
- `app/execution/adapters/mt5_adapter.py` - every method raises `NotImplementedError` by design. This
  isn't a gap so much as a safety feature: MT5 requires a Windows-only proprietary package this sandbox
  can't run anyway, and turning a hard stub into "implemented but never tested" would be a *regression*
  in safety, not progress. Finish this only against a demo account, after extensive paper-trading validation.
- Frontend dashboard - see `frontend/README.md`. Backend is API-complete; nothing has been built yet.

**Known scale limitations** (fine for one user / one process, would need work to grow beyond that):
- `TradingModeManager`, `DuplicateTradeGuard`'s in-memory fallback, and `NotificationPreferencesManager`
  are all single-process singletons. Multi-worker deployments need the Redis/DB-backed paths made
  mandatory rather than fallback-optional.
- `NotificationPreferences` is a single global object - no per-user support yet.
- `TradeJournalEntry` doesn't yet get session/strategy tags at write time, so weekly reports can't
  break performance down by session as spec'd (the query support exists in `report_service.py`, the
  data just isn't populated yet).

---

## Suggested next build session (post-frontend)

1. Get a real OANDA (or similar) account and validate `oanda_provider.py` against live data.
2. Add session/strategy tagging to `TradeJournalEntry` writes.
3. Multi-user support for `NotificationPreferences` and `InstrumentConfig`.
4. Finish `mt5_adapter.py` - demo account only, after real paper-trading track record.
