# Changelog

## Cable Scalp v2.0 — 2026-05-15 — AI removed, rule-based news filtering

### Summary

v2.0 removes the optional OpenAI AI News Guard and replaces it with the
existing rule-based NewsFilter that was already running in v1.x as the
**primary** news defence. The signal engine (EMA9/21 crossover + ORB
time-decay + CPR bias), position sizing, SL/TP, margin guard, and all
execution logic are **100% unchanged**.

### What changed

**`ai_news_guard.py`** — replaced with a transparent no-op stub.
Every call returns `{"enabled": False, "action": "ALLOW"}` instantly.
No OpenAI API key is required. No network requests are made.

**`ai_guard_tracker.py`** — replaced with a transparent no-op stub.
All five tracker functions (`record_ai_decision`, `link_trade`,
`mark_trade_failed`, `backfill_actual_trade_result`,
`update_blocked_virtual_outcomes`) are no-ops that return safe neutral
values. The rest of the codebase is unaffected.

**`bot.py`** — AI News Guard block (~100 lines) removed from `_signal_phase`.
AI tracking calls removed from `_guard_phase` (virtual outcome update) and
`_execution_phase` (link_trade / mark_trade_failed). All other logic is
identical to v1.5.

**`settings.json`** — all `ai_news_guard_*` and `ai_tracking_*` keys removed.
`ai_news_guard_enabled` explicitly set to `false` as a safety guard.
Bot name updated to `Cable Scalp v2.0`.

**`requirements.txt`** — `openai>=1.40.0` removed.

**`version.py`** — bumped to `2.0.0`.

### What is unchanged

- Signal engine: EMA9/21 crossover + ORB time-decay + CPR bias scoring (0–6)
- Signal threshold, session thresholds, Tokyo fresh-cross override
- News protection: `NewsFilter` hard-blocks high-impact GBP/USD events
  (FOMC, NFP, Powell, rate decisions) ±30 min; applies −1 score penalty
  for medium-impact events (CPI, PCE, jobless claims)
- ORB max age cap (120 min)
- CPR width filter (0.30%)
- Loss streak cooldown (60 min)
- H1 trend filter (strict mode)
- Dead zone 04:00–07:59 SGT
- Margin guard, spread guard, position sizing
- Telegram alerts (all templates unchanged)
- OANDA execution, reconciliation, PnL back-fill
- Database, scheduler, signal logger

### Why

The AI guard was a second opinion on news risk that read the same calendar
data that `NewsFilter` already uses. Removing it means:

- No OpenAI API key required — simpler deployment
- No 12-second external timeout in a 3-minute cycle
- Fully deterministic — same inputs always produce the same output
- Backtest-safe — no non-reproducible LLM calls in the critical path
- `NewsFilter` hard-block is stricter and more reliable than an LLM opinion

---

## Cable AI Scalp v1.5 — 2026-05-13 — ORB max age cap, CPR width filter, loss cooldown

See original CHANGELOG for v1.5 and earlier history.
