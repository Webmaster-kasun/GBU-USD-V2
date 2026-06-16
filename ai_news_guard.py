"""ai_news_guard.py — Cable Scalp v2.0 (No-AI edition)

The AI News Guard feature has been removed in v2.0.
This module is a transparent no-op stub that keeps the rest of the codebase
100% compatible without requiring any further changes.

All news-risk filtering in v2.0 is handled exclusively by the rule-based
NewsFilter (news_filter.py):
  - Hard block +-30 min around high-impact GBP/USD events (FOMC, NFP, Powell,
    rate decisions).
  - Soft score penalty (-1) for medium-impact events (CPI, PCE, jobless claims).
  - Calendar cache staleness / fail-closed behaviour.

No OpenAI API key is required.  No external requests are made.
Every call to ai_news_guard() returns ALLOW instantly.
"""
from __future__ import annotations

from typing import Any


def ai_news_guard(
    settings: "dict[str, Any]",   # kept for call-site compatibility
    payload:  "dict[str, Any]",   # kept for call-site compatibility
) -> "dict[str, Any]":
    """Return a fixed ALLOW result — AI guard is disabled in v2.0.

    The interface is identical to the v1.x version so bot.py needs no changes.
    The returned dict includes every key that callers may read:
      enabled, risk_level, action, reason, model, decision_id.
    """
    return {
        "enabled":     False,
        "risk_level":  "LOW",
        "action":      "ALLOW",
        "reason":      "Rule-based mode — AI News Guard removed in v2.0.",
        "model":       None,
        "decision_id": None,
    }
