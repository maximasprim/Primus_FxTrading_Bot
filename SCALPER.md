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

## Entry styles (Scalper -> Live & settings -> How picky -> Entry style)
Four ways to decide *what* to bet on. They share the same exits, limits and costs, so they can be compared fairly:

| Style | Bets that... | Does nothing when... |
|---|---|---|
| Momentum (original) | a move already running will continue | no clear direction |
| Breakout | price leaving a quiet 30-minute range keeps going | price is inside the range |
| Spike fade | a sudden over-stretched spike snaps back | there is no 1.5x-ATR burst |
| Range reversal | the top/bottom of a sideways range reverses | the market is trending |

None has a proven edge. Scores aren't comparable between styles (each has a suggested minimum score). Test one style at a
time in the Backtest tab; the backtest uses your *saved* settings, and every result is labelled with the style it tested.

How the styles were validated (permanent tests; `RUN_SLOW_TESTS=1 pytest tests/test_scalper_styles.py`):
each style finds an edge only where one is planted (breakout on trending prices; spike fade and range reversal on
mean-reverting prices), loses on pure noise, and momentum loses on a mean-reverting market. Measured replay bias per
style (zero-cost, known intrabar path): breakout -0.047, spike fade -0.015, range reversal -0.017 per trade, i.e. the
replay is pessimistic, never flattering.

## The locked test period (Backtest tab)
Every time you tweak a setting after looking at a result you fit it a little more to that data. Do it enough and
anything looks good, then fails on new data. The lock prevents that:
1. **Explore** (default). The first run locks the most recent 30% of the data. From then on Explore only ever sees data
   *before* that point (it is physically removed; a test checks that no trade can open or close after it). Try styles
   and settings as much as you like.
2. **Confirm**. When you have settled on ONE configuration, run Confirm. It tests it on the locked period (with a short
   indicator warm-up before it, no trading before it). **Once.** That number is the one to believe. Compare it with what
   you saw while exploring: a big drop means the settings were fitted to noise.
3. The lock is then spent. A second Confirm is refused. You can **Reset** the lock, but that period now counts as seen and
   the history records it.
`Use all data (no lock)` still exists for quick looks. CLI: `--holdout explore|confirm|off`.
It can't stop you from peeking at charts of the locked period elsewhere; it only makes the honest path the easy one.

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


## Trade history, presets and scheduled sessions

**History tab.** Every trade is written to `data/scalper_history.jsonl` twice: when it opens (pair, side, lots, entry, stop,
target, spread, signal score and reasons, the preset in use, and the exit rules it was opened under) and when it closes
(exit price, real profit/loss from the broker, and *why* it ended: take-profit, stop-loss, trailing stop, time limit, you
pressed Stop, a safety rule, the end of a scheduled session, or closed while the bot was off). The file is append-only, so
a crash can't damage old history. Trades that closed while the bot was off are filled in the next time you press Start.
Export to CSV from the History tab. A problem writing the history never interrupts trading.

**Presets.** A preset is a named bundle of settings (`data/scalper_presets.json`). Using one validates "current settings +
preset" through the normal config rules, so every bound and safety rule still applies. Presets can never change the
simulation/MT5 switch or the real-money switch. Four starter presets are included; they are examples, **not** proven profit
settings (see the backtest results). Save your own from the current settings. Each trade is stamped with the preset it was
opened under ("(edited)" if you tweaked it afterwards).

**Each trade keeps its own exit rules.** With *Each trade keeps the exit rules it was opened with* on (the default), a trade
follows the hold time, trailing stop, break-even and per-trade profit target that were active when it opened, even if you
switch presets afterwards. Session-wide rules (loss limit, profit target, basket take-profit, emergency stop) always use the
live settings. Turn the option off to get the old behaviour where changes also hit trades that are already open.

**Schedule tab.** Add sessions such as "15:30 on weekdays, run 90 minutes or until +5.00". Rules:
* OFF until you switch it on. A scheduled start runs the same checks as the Start button.
* The backend must be running on the PC with MT5 open and logged in at the start time. If it isn't ready within 10 minutes
  of the start time, that day is marked *missed* and is **not** started late.
* "Only start if the market is open" skips weekends/holidays. The end time is fixed on the clock (start + duration).
* Each session runs at most once per local day. A session can end on time, on a profit target (counting open trades),
  or by a loss rule; it then closes its trades (or leaves them under their own stop/target, your choice).
* "Start now with limits" does a one-off run with a time limit and/or profit target.
Times use the timezone you pick (handles daylight saving). The schedule is stored in `data/scalper_schedule.json`.


## Hybrid (regime switch) entry style: experimental, not recommended

The `hybrid` style reads the market and hands each decision to the one style built for it (breakout after a squeeze, spike fade
unless the market is trending, range reversal when sideways, otherwise no trade). It is selectable like any other style and
changes nothing unless you pick it.

Backtest on the earlier 70% of the MT5 data (same costs and exits as the single styles, target 2x / stop 1x ATR, score 70),
net pips per trade after costs, with a random-direction control at the same entry times:

| Pair | Hybrid | Random direction | Spike fade alone |
|---|---|---|---|
| EUR/USD | -0.56 | -0.41 | -0.36 |
| GBP/USD | -1.65 | -1.51 | -1.22 |
| XAU/USD | -5.13 | -4.23 | -1.41 |

It lost more than spike fade alone and its direction was no better than random (before costs it was negative: -0.14, -0.28
and -2.3 pips per trade). It also trades MORE often (about 5,100 EUR/USD trades against about 1,960 for spike fade), because it
adds the triggers of the two weaker styles. The locked final 30% of the data was not used.
