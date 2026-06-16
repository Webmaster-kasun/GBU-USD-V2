# Cable AI Scalp v1.2 — Technical Specification & Operations Wiki

**Bot name:** Cable AI Scalp v1.2
**Instrument:** GBP/USD (Cable) only
**Exchange:** OANDA (practice & live)
**Deployment:** Railway (PaaS)
**Signal timeframe:** M5 (5-minute candles)
**Cycle interval:** Every 5 minutes
**Status:** Demo mode (`OANDA_DEMO=true`)

---

## 1. Purpose & Scope

Cable AI Scalp v1.2 is a fully automated 5-minute scalping bot dedicated to GBP/USD.
It uses a three-layer signal engine (EMA crossover + ORB + CPR bias) scored 0–6/6,
with minimum score thresholds per session. All configuration lives in `settings.json`.

---

## 2. Architecture Overview

```
scheduler.py  (APScheduler — every 5 min)
      |
      ├── run_bot_cycle()  ← called once per cycle
      |       |
      |       ├── _guard_phase()     Market closed / dead zone / caps / news
      |       ├── _signal_phase()    SignalEngine.analyze() → score + direction
      |       └── _execution_phase() Margin check → spread check → place_order()
      |
      ├── send_daily_report()   07:50 SGT Mon–Fri
      ├── send_weekly_report()  Monday 08:00 SGT
      └── send_monthly_report() First Monday 08:10 SGT
```

---

## 3. Signal Engine

**File:** `signals.py` → `SignalEngine.analyze()`

Three components scored each M5 cycle:

### 3a. EMA Crossover (M5)
- EMA9 fresh cross above EMA21: **+3 pts** → `EMA Fresh Cross Up` (BUY)
- EMA9 fresh cross below EMA21: **+3 pts** → `EMA Fresh Cross Down` (SELL)
- EMA9 aligned above EMA21 (no fresh cross): **+1 pt** → `EMA Trend Up`
- EMA9 aligned below EMA21 (no fresh cross): **+1 pt** → `EMA Trend Down`

### 3b. ORB Confirmation (M15 first candle of session)
- Price beyond ORB, break **fresh** (<60 min): **+2 pts**
- Price beyond ORB, break **aging** (60–120 min): **+1 pt**
- ORB not formed or price inside range: **+0 pts**

### 3c. CPR Bias (Daily pivot)
- BUY signal and price above daily pivot: **+1 pt**
- SELL signal and price below daily pivot: **+1 pt**

**Max score: 6/6**

---

## 4. Session Schedule

All times SGT (UTC+8):

| Window | SGT | Score threshold | Trade max |
|---|---|---|---|
| Dead zone | 04:00–07:59 | No trading | — |
| Tokyo | 08:00–15:59 | ≥ 5/6 | 10 |
| London | 16:00–20:59 | ≥ 4/6 | 10 |
| US session      | 21:00–23:59 | **DISABLED**  | —  |
| US Continuation | 00:00–03:59 | ≥ 4/6         | 4 |

Market fully closed Saturday and Sunday. Monday opens at 08:00 SGT.

---

## 5. Position Sizing

Units calculated from `position_usd ÷ sl_risk_per_unit_usd` per unit.
GBP/USD uses static `pip_value_usd = $10.00` (standard for USD-quoted pairs).

| Score | Position | Risk | Units (GBP/USD 18p SL) |
|---|---|---|---|
| 4 | $30 partial | 1.5% of $2k | ~16,667 |
| 5–6 | $48 full | 2.4% of $2k | ~26,667 |

SL/TP (GBP/USD):

| Pair | SL | TP | RR | Break-even WR | pip_value_usd |
|---|---|---|---|---|---|
| GBP/USD | 18p | 30p | 1.67× | 37.5% | $10.00 (static) |

---

## 6. Risk Guards

Executed in order each cycle, early return on first failure:

1. **Market closed** — Saturday / Sunday / Monday pre-08:00
2. **Dead zone early exit** — 04:00–07:59 AND no open trades → zero API calls
3. **News block** — high-impact event within ±30 min
4. **News penalty** — medium event → −1 to score
5. **Loss cooldown** — consecutive losses → 30 min pause
6. **Friday cutoff** — after 23:00 SGT Friday
7. **Session check** — outside all active windows
8. **Daily loss max** — 8 losing trades → pause until 08:00 SGT
9. **Session max** — per-window trade limit reached
10. **Concurrent cap** — 1 trade per pair, 2 globally
11. **Margin guard** — units reduced if margin insufficient
12. **Min trade units** — reject if units < 1,000 after margin guard
13. **Spread guard** — skip if spread > limit for session

---

## 7. H1 Trend Filter

Fetches H1 EMA21 each cycle to classify the trade as aligned or counter-trend.

| Mode | Effect |
|---|---|
| `soft` (current) | Labels trade as aligned/counter-trend in record — no blocks |
| `strict` | Blocks counter-trend entries entirely |

Flip to `strict` in `settings.json` once data confirms counter-trend trades have
materially lower win rate.

---

## 8. Telegram Alerts

All message cards defined in `telegram_templates.py`.
WATCHING cards for score < `telegram_min_score_alert` (default 4) are suppressed. Score 3 alerts never resulted in trades and caused message flooding — silenced in v1.8.

### Trade duration guards (v2.0)

Two new guards prevent M5 scalp trades from becoming overnight drift trades:

**1. Max trade duration** (`max_trade_duration_hours: 4`)
Any open trade held longer than 4 hours is force-closed at market price.
Reason: M5 signal context (ORB age, EMA cross) is stale after 4 hours.

**2. Session-end force close** (`force_close_at_session_end: true`)
A trade is force-closed when its originating session ends:
- London trade → closes at 21:00 SGT
- Tokyo trade → closes at 16:00 SGT
- US Cont. trade → closes at 04:00 SGT

Both guards send a Telegram alert with P&L and max pips reached.

### US / US Continuation label separation (v2.0)

US session (21:00–23:59 SGT) and US Continuation (00:00–03:59 SGT) previously
shared the same `macro_session: "US"` label. In v2.0 they are separate:
- US session → `macro_session: "US"` (disabled, sentinel 99)
- US Continuation → `macro_session: "US_Cont"` (enabled, max 10, threshold 4)

This enables correct per-session reporting in weekly/monthly summaries.

### TP reduced to 25 pips (v2.0)

Reduced from 30p → 25p based on live data analysis:
- Avg achieved win was 22–25 pip equivalent (spread cost eating 2–3p)
- 30p TP caused overnight hold risk — market couldn't reach target in one session
- 25p TP resolves within London session in trending conditions
- Break-even WR at 25p/18p RR: 41.9% (well below current 55%+ London rate)
- `min_rr_ratio` updated from 1.6 → 1.3 (actual RR = 1.39×, buffer of 0.09)

### Signal logging (v1.9)

Every signal evaluation (score ≥ `signal_log_min_score`, default 3) can be logged to `/data/signal_log.csv` for future AI/ML training data. Disabled by default — enable via `signal_logging_enabled: true` in settings.json.

**CSV columns:** `timestamp_sgt, day_of_week, session, hour_sgt, direction, score, ema_pts, orb_pts, cpr_pts, setup, h1_trend, h1_aligned, orb_age_min, orb_formed, cpr_width_pct, atr, spread_pips, action, block_reason, outcome, pl_usd, trade_id`

**Actions captured:** FIRED · WATCHED · BLOCKED_H1 · BLOCKED_NEWS · BLOCKED_SPREAD · BLOCKED_RR · NOISE

**Export:** Sent automatically to Telegram on last day of month at 08:35 SGT (5 min after trade CSV). Only exports when logging is enabled. Score 3 alerts never resulted in trades and caused message flooding — silenced in v1.8.
Score 1–2 = noise; score 3+ sends.

### Report schedule

| Report | Time | Content |
|---|---|---|
| Daily | 07:50 SGT Mon–Fri | Session breakdown + day total + MTD |
| Weekly | Monday 08:00 SGT | Session + setup bars |
| Monthly | First Monday 08:10 SGT | Full breakdown + verdict + **H1 filter split** |

---

## 9. Key Files

| File | Purpose |
|---|---|
| `scheduler.py` | Entry point — APScheduler, jobs, startup |
| `bot.py` | Trade cycle: guard → signal → execution |
| `signals.py` | EMA + ORB + CPR engine |
| `oanda_trader.py` | OANDA REST API wrapper |
| `telegram_templates.py` | All Telegram message cards |
| `reporting.py` | Daily / weekly / monthly report builders |
| `config_loader.py` | Settings loading + all defaults |
| `settings.json` | **Single source of truth for all config** |
| `database.py` | SQLite — cycle + signal + state logging |
| `reconcile_state.py` | Startup reconciliation of open trades |
| `news_filter.py` | Forex Factory news block/penalty |

---

## 10. Data Directory (`/data` on Railway volume)

| File | Purpose |
|---|---|
| `trade_history.json` | All trade records — open + closed |
| `settings.json` | Volume copy (synced from bundle on startup) |
| `runtime_state.json` | Last cycle status, balance |
| `ops_state_gbpusd.json` | GBP/USD ops state (session, caps, alerts) |
| `score_cache_gbpusd.json` | Last signal dedup |
| `calendar_cache.json` | Cached Forex Factory events |
| `rf_scalp.db` | SQLite — cycle log, signal log |

---

## 11. Deployment

### Railway
1. Push folder to GitHub
2. New Railway project → Deploy from GitHub
3. Set environment variables (see below)
4. Add persistent volume mounted at `/data`
5. Railway builds and deploys automatically

### Environment Variables

| Variable | Required | Notes |
|---|---|---|
| `OANDA_API_KEY` | ✅ | Practice or live |
| `OANDA_ACCOUNT_ID` | ✅ | e.g. `101-003-XXXXXXX-001` |
| `OANDA_DEMO` | ✅ | `true` / `false` |
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your chat/channel ID |
