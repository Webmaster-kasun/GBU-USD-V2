"""ai_guard_tracker.py — Cable Scalp v2.0 (No-AI edition)

The AI guard tracker feature has been removed in v2.0.
This module is a transparent no-op stub that keeps every import and every
call-site in bot.py, reporting.py, and backfill_pnl fully compatible.

None of the stub functions perform any I/O, file writes, or network calls.
They all return safe, typed values that every caller already guards against.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public API — mirrors the v1.x signatures exactly
# ---------------------------------------------------------------------------

def record_ai_decision(
    settings: "dict[str, Any]" = None,
    payload:  "dict[str, Any]" = None,
    ai_result: "dict[str, Any]" = None,
    entry: float = 0.0,
    sl_price: float = 0.0,
    tp_price: float = 0.0,
    estimated_risk_usd: float = 0.0,
    estimated_reward_usd: float = 0.0,
    sl_pips: int = 0,
    tp_pips: int = 0,
    **kwargs: Any,
) -> None:
    """No-op — decision tracking removed in v2.0.

    Returns None; bot.py no longer checks this value in v2.0.
    """
    return None


def link_trade(
    decision_id: "str | None",
    trade_id: "str | None",
) -> None:
    """No-op — AI trade linking removed in v2.0."""
    return None


def mark_trade_failed(
    decision_id: "str | None",
    error: str = "",
) -> None:
    """No-op — AI failure marking removed in v2.0."""
    return None


def backfill_actual_trade_result(
    trade_id: str,
    pnl: float,
    closed_at_sgt: "str | None" = None,
) -> None:
    """No-op — AI result back-fill removed in v2.0."""
    return None


def update_blocked_virtual_outcomes(
    trader: Any = None,
    instrument: str = "",
    now_sgt: Any = None,
) -> dict:
    """No-op — virtual outcome tracking removed in v2.0.

    Returns an empty dict so bot.py's guard evaluates to False safely.
    """
    return {}


def summarize_ai_tracking(
    start: Any = None,
    end: Any = None,
) -> dict:
    """No-op — returns empty summary so reporting templates skip the AI section.

    _ai_stats_section() in telegram_templates.py already handles
    ai_stats=None or total_decisions==0 by returning an empty string.
    """
    return {"total_decisions": 0}


def get_ai_guard_csv_path() -> "Path | None":
    """No-op — AI guard CSV removed in v2.0. Returns None."""
    return None
