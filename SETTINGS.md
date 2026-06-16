# Cable Scalp v2.0 v1.2 Settings

Main settings live in `settings.json`.

## Identity

```json
"bot_name": "Cable Scalp v2.0 v1.2"
```

## Risk

```json
"position_partial_usd": 60,
"position_full_usd": 75
```

This means score 4 risks $60 and score 5–6 risks $75.

## News filter

```json
"news_filter_enabled": true,
"news_relevant_currencies": ["GBP", "USD"]
```

## AI News Guard

```json
```

The AI guard only reviews news risk after the existing rule-based setup is valid. It can block/caution/allow, but it does not generate trades or change SL/TP/risk.

## Railway variables

```text
OPENAI_API_KEY=your_*(openai removed in v2.0)*_api_key
AI_NEWS_GUARD_ENABLED=true
```

`AI_NEWS_GUARD_ENABLED` is optional and overrides the JSON setting.

## AI Guard Tracking

```json
```

These settings enable AI decision history and blocked-trade virtual TP/SL outcome tracking. The tracking layer is reporting-only and does not affect trading decisions.
