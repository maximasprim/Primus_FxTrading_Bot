# Scalper, MT5 data and API-key protection

## What this patch adds
- **Scalper** (`app/scalper/`): a fast loop (default every 0.5 s) that opens several short trades, takes profit,
  and stops itself on the limits you set. Dashboard tab: **Scalper**.
- **MT5 market data** (`MARKET_DATA_PROVIDER=mt5`): candles/prices straight from the MT5 terminal -- no rate limit.
- **API-key protection**: every route now needs the `X-API-Key` header (except the TradingView webhook,
  the Meta/Twilio inbound callbacks and `/health`, which have their own checks).

## Install
1. Unzip this patch over your project (your `.env` files are untouched).
2. `pip install -r requirements.txt`   (installs `MetaTrader5` on Windows only)
3. In `Server/.env` set a long random key and use the same value in the dashboard (Settings -> API key):
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`  ->  `API_SECRET_KEY=...`
4. **Rotate the credentials that were in the zip you shared** (MT5 password, Anthropic/Gemini/OpenAI keys,
   Twelve Data, WhatsApp token, Redis). `.env.example` in this patch is cleaned of them.

## Run it on MT5 (Windows)
1. Install the MT5 desktop terminal from your broker, log in, and keep it open.
2. In MT5: toolbar **Algo Trading** button must be **green**; Tools -> Options -> Expert Advisors -> *Allow algorithmic trading*.
3. Make sure each pair is visible in Market Watch. If your broker adds a suffix (EURUSDm) set `MT5_SYMBOL_SUFFIX=m`.
4. `Server/.env`: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, and `MARKET_DATA_PROVIDER=mt5`.
5. Start the backend **on the same Windows machine** (not Docker -- MT5 only works on Windows).
6. Dashboard -> Scalper -> **Run checks**. Fix anything red, then Start.

### Desktop vs mobile MT5
Mobile MT5 has no automation interface, so a bot cannot run on it. The bot drives the **desktop** terminal. Trades it
opens appear in the mobile app on the same account (you can close them there by hand), and you can monitor and control
the bot from this dashboard on your phone. For 24/5 operation use a Windows VPS.

## Suggested rollout
1. **Simulation** backend (default) -- check the dashboard and every limit behaves how you expect.
2. **MT5 demo account** for several weeks. Compare results *after spread/commission*.
3. Only then a real account: enable *Allow REAL-money accounts* (you must type LIVE), minimum lot, tight loss limit.

## Safety built in
- Every order carries a broker-side stop-loss and take-profit, so trades stay protected if the bot/PC dies.
- Refuses to start on a live account unless allowed; refuses if Algo Trading is off; needs a session loss limit.
- Hard caps: open trades, trades per pair, trades/hour, total open risk %, lot size, spread, loss streak.
- If the lot size implied by "% risk" is below the broker minimum, the trade is **skipped**, never rounded up.
- Only touches trades with its own magic number (20260901); your manual trades are never closed or modified.
- Respects the Control Center emergency stop. Always starts STOPPED after a restart.

## Backtesting (dashboard: Scalper -> Backtest, or the command line)
Replays your **saved scalper settings** through the *real* engine over historical 1-minute candles, charging spread,
slippage and commission, and compares against three controls so you can tell skill from luck:

| Run | What it tells you |
|---|---|
| Your settings | The headline result, after costs |
| Random direction | Same entry times, coin-flip buy/sell. If you can't beat it, the signal's direction has no value |
| Scrambled prices | Same candles in shuffled order. Destroys real market patterns; if you can't beat it, you found nothing real |
| Costs doubled | Does the profit survive wider spreads? |

It also shows win rate by signal score (does a higher score mean better trades?), by pair, by exit type, and the
first-60% vs last-40% split. The verdict says **promising** only if every check agrees; otherwise inconclusive / not viable.

Data: MT5 terminal history (real per-bar spreads), or CSV files (MT5 History Center export works; one file per pair,
`EURUSD.csv`), or synthetic (machinery test only). Command line:
`python -m app.scalper.backtest run --source csv --csv-dir C:\data --days 20` (add `--set min_signal_score=60` to try a setting).
It runs in separate processes, so it can't slow down a live bot. Trade-by-trade CSVs are saved in `data/backtests/<id>/`.

How much to trust it:
- It was validated against known answers: on pure random prices it reports *not viable*; on prices with a planted
  trend it reports *promising* (both are permanent tests: `RUN_SLOW_TESTS=1 pytest -k planted_edge`).
- Rebuilding each minute from open/high/low/close is approximate. Measured on data where the true path is known, the
  rebuild is slightly **pessimistic** for momentum entries (about 0.03 per 0.01-lot trade, ~0.3 pip).
- Not modelled: requotes/rejections, margin, swap, news-time spread spikes that aren't in your data, partial fills.
- Don't tune settings on the same data until the result looks good. That is how people fool themselves. Tune on one period,
  confirm on a different one, then demo-trade.
- A good backtest earns a demo-account trial. It never earns live money.

## Cent accounts (Exness Standard Cent, symbols ending in `m`)
- The balance is in **US cents (USC)**: 100.00 USC = $1.00. Every money setting (session loss, profit target, basket
  take-profit, per-trade profit) is in the account's own units, so on a cent account "20" means 20 cents. The **%** settings
  don't depend on units.
- A cent lot is 100x smaller (1 lot = 1,000 units), so 0.01 lot is worth about 0.1 of your balance units per pip on
  EUR/USD, the same *proportion* of your equity as 0.01 lot on a 100-dollar standard account.
- Don't take that on trust: **Run checks** shows "Pip value": what 1 pip is worth on YOUR account, straight from the broker,
  as a % of your equity. The backtester also reads that figure (and your real balance) from MT5, so its profit and loss
  are in the same units as your account.
- With CSV data the backtester assumes standard contract sizes, which reads correctly as cents on a cent account but not
  in dollars on a standard one. Prefer "MT5 terminal history" for exact figures.

## Honest limits
- Developed and tested on Linux with a simulator and a fake MT5 module; **not yet run against a real MT5 terminal**.
  Do the demo phase for this reason alone. The same applies to the MT5 history download used by the backtester.
- The built-in signal is a simple heuristic (EMA stack, breakout, volatility burst, RSI). It has **no proven edge**; in a
  synthetic test it fired on random noise about a third of the time at a score of 55. Lowering the score means more trades,
  not better ones. Backtest before trusting it.
- Around news, spreads widen and fills slip; the spread filter will often (correctly) keep the bot out of the first seconds.
- Netting accounts merge trades per symbol, so the bot uses 1 position per pair there (hedging accounts allow stacking).
- Broker-time offset is estimated from live ticks (half-hour steps); if it can't be verified (market closed) prices read as
  stale and the bot simply doesn't trade.
