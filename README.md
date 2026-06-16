# Cable Scalp v2.0 v1.2

Separate AI-enabled GBP/USD scalping bot based on Cable Scalp v2.0 v1.2, with the Rogue-H1 v2.7 AI News Guard pattern adapted for Cable.

## Purpose

Run this as a separate Railway service to compare normal Cable Scalp v2.0 behavior against an AI-assisted news-risk guard.

## Strategy

- Pair: GBP/USD
- Timeframe: M5
- Core strategy: EMA + ORB + CPR
- H1 filter: strict by default
- SL/TP: configured in `pair_sl_tp`
- Risk sizing: fixed USD risk by score
- AI layer: optional OpenAI News Guard, used only after a valid technical setup exists

## AI News Guard

The AI guard does **not** create signals and does **not** change entry, SL, TP, or risk.

Flow:

1. Technical strategy finds a valid setup.
2. Calendar news hard lock/penalty runs first.
3. Spread/margin guards run.
4. AI News Guard reviews GBP/USD news risk.
5. AI can return `ALLOW`, `CAUTION`, or `BLOCK`.

Required Railway variable when enabled:

```text
OPENAI_API_KEY=your_*(openai removed in v2.0)*_api_key
```

Optional override:

```text
AI_NEWS_GUARD_ENABLED=true
```

## Current baseline settings

- Bot name: Cable Scalp v2.0 v1.2
- Risk: $60 score 4 / $75 score 5–6
- Relevant news currencies: GBP + USD
- AI model: gpt-4o-mini
- AI scores checked: 4, 5, 6
- AI fail mode: fail-open by default
- Max open trades: 1
- Session max trades: 4

## Safety design

Calendar hard-lock remains priority. AI cannot override a fixed high-impact news block. If OpenAI API fails and fail-closed is false, the bot allows normal rule-based operation.

## AI Guard Tracking v1.1

Cable Scalp v2.0 v1.2 adds observability for AI decisions:

- `/data/ai_guard_history.json` stores every AI Guard decision.
- `/data/ai_guard_history.csv` mirrors the same data for export/review.
- ALLOW and CAUTION decisions are linked to the real OANDA trade ID when a trade is placed.
- Real closed-trade P&L is backfilled into the AI history after OANDA confirms closure.
- BLOCK decisions are tracked as virtual setups using the same entry, SL and TP snapshot.
- Virtual blocked setups are marked as `WIN`, `LOSS`, `EXPIRED`, or still `OPEN`.
- Positive estimated AI impact means AI avoided losing virtual trades; negative means AI likely blocked winners.

AI tracking is observability only. It does not change score, direction, position size, entry, SL, TP, or order placement.
