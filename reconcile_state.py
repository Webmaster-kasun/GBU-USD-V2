"""
Startup/runtime reconciliation helpers for the RF Scalp Bot.
Broker state is treated as the source of truth for open positions/trades.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def reconcile_runtime_state(trader, history: list, instrument: str, now_sgt, alert=None) -> dict:
    """
    Reconcile local history with broker truth.

    What it does:
    - detects currently open trades at the broker
    - inserts a recovered FILLED record into history if an open broker trade
      exists but local history does not know about it
    - back-fills realized P&L on local FILLED trades that are now closed
    - returns a summary for logging/decision-making
    """
    summary = {
        "open_trade_ids": [],
        "open_trade_count": 0,
        "recovered_trade_ids": [],
        "backfilled_trade_ids": [],
        "recent_closed_count": 0,
    }

    try:
        open_trades = trader.get_open_trades(instrument)
    except Exception as exc:
        log.warning("Could not fetch open trades during reconciliation: %s", exc)
        open_trades = []

    summary["open_trade_ids"] = [str(t.get("id")) for t in open_trades if t.get("id")]
    summary["open_trade_count"] = len(summary["open_trade_ids"])

    local_trade_ids = {
        str(t.get("trade_id")) for t in history
        if t.get("status") == "FILLED" and t.get("trade_id") is not None
    }

    for trade in open_trades:
        trade_id = str(trade.get("id", "")).strip()
        if not trade_id or trade_id in local_trade_ids:
            continue

        current_units = _safe_float(trade.get("currentUnits"))
        direction = "BUY" if current_units > 0 else "SELL"
        entry = _safe_float(trade.get("price"))
        recovered = {
            "timestamp_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "RECOVERED",
            "instrument": instrument,
            "direction": direction,
            "setup": "startup_reconciled",
            "session": "Recovered",
            "macro_session": "Recovered",
            "score": None,
            "threshold": None,
            "entry": round(entry, 2) if entry > 0 else None,
            "sl_price": None,
            "tp_price": None,
            "size": abs(current_units),
            "cpr_width_pct": None,
            "estimated_risk_usd": None,
            "estimated_reward_usd": None,
            "spread_pips": None,
            "stop_pips": None,
            "tp_pips": None,
            "levels": {"source": "broker_reconciliation"},
            "details": "Recovered from broker openTrades during startup/runtime reconciliation.",
            "trade_id": trade_id,
            "status": "FILLED",
            "realized_pnl_usd": None,
            "breakeven_moved": False,
        }
        history.append(recovered)
        summary["recovered_trade_ids"].append(trade_id)
        local_trade_ids.add(trade_id)
        log.warning("Recovered open broker trade into local history: %s", trade_id)

    try:
        recent_closed = trader.get_recent_closed_trades(instrument, count=25)
        summary["recent_closed_count"] = len(recent_closed)
    except Exception as exc:
        log.warning("Could not fetch recent closed trades during reconciliation: %s", exc)
        recent_closed = []

    pnl_by_trade_id = {}
    for trade in recent_closed:
        trade_id = str(trade.get("id", "")).strip()
        if not trade_id:
            continue
        pnl = trade.get("realizedPL")
        if pnl is not None:
            pnl_by_trade_id[trade_id] = _safe_float(pnl)

    open_trade_ids = set(summary["open_trade_ids"])
    for item in history:
        if item.get("status") != "FILLED":
            continue
        trade_id = str(item.get("trade_id", "")).strip()
        if not trade_id or trade_id in open_trade_ids:
            continue
        if item.get("realized_pnl_usd") is not None:
            continue

        pnl = pnl_by_trade_id.get(trade_id)
        if pnl is None:
            pnl = trader.get_trade_pnl(trade_id)
        if pnl is not None:
            item["realized_pnl_usd"] = pnl
            # Only mark for backfill if backfill_pnl hasn't already sent the alert
            if not item.get("closed_alert_sent"):
                summary["backfilled_trade_ids"].append(trade_id)
            log.info("Reconciled closed trade %s with realized P&L $%.2f", trade_id, pnl)

    if alert and summary["recovered_trade_ids"]:
        alert.send(
            "♻️ Startup reconciliation recovered open broker trade(s): "
            + ", ".join(summary["recovered_trade_ids"])
        )

    return summary


def startup_oanda_reconcile(
    trader,
    history: list,
    instrument: str,
    today_sgt: str,
    now_sgt,
) -> dict:
    """Reconcile today's closed trades from OANDA before the first bot cycle.

    After a mid-day redeploy history.json may be missing trades that closed
    between the last save and the restart. daily_totals() then under-counts
    losses and the loss cap fails to fire correctly.

    Fetches recently closed trades via get_recent_closed_trades(), filters to
    today (SGT date), then backfills P&L for known trades or injects synthetic
    records for trades missing from local history.

    history is mutated in place. Caller saves history if summary shows changes.
    Returns dict: injected, backfilled, skipped, errors.
    """
    import pytz
    from datetime import datetime

    SGT = pytz.timezone("Asia/Singapore")

    summary = {"injected": [], "backfilled": [], "skipped": 0, "errors": []}

    try:
        all_closed = trader.get_recent_closed_trades(instrument, count=50)
    except Exception as exc:
        msg = f"startup_oanda_reconcile: could not fetch closed trades: {exc}"
        log.warning(msg)
        summary["errors"].append(msg)
        return summary

    # Filter to trades that closed today (SGT date)
    today_closed = []
    for trade in all_closed:
        close_time_raw = trade.get("closeTime", "")
        if not close_time_raw:
            continue
        try:
            dt_utc = datetime.strptime(close_time_raw[:19], "%Y-%m-%dT%H:%M:%S")
            dt_sgt = pytz.utc.localize(dt_utc).astimezone(SGT)
            if dt_sgt.strftime("%Y-%m-%d") != today_sgt:
                continue
            trade["_close_time_sgt"] = dt_sgt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        today_closed.append(trade)

    if not today_closed:
        log.info("startup_oanda_reconcile: no closing trades found for %s on %s",
                 instrument, today_sgt)
        return summary

    history_by_trade_id: dict = {}
    for item in history:
        tid = str(item.get("trade_id", "")).strip()
        if tid:
            history_by_trade_id[tid] = item

    for trade in today_closed:
        trade_id = str(trade.get("id", "")).strip()
        if not trade_id:
            continue

        raw_pnl = trade.get("realizedPL")
        try:
            pnl = float(raw_pnl) if raw_pnl is not None else None
        except (TypeError, ValueError):
            pnl = None

        close_time_str = trade.get("_close_time_sgt", "")

        if trade_id in history_by_trade_id:
            existing = history_by_trade_id[trade_id]
            if existing.get("realized_pnl_usd") is None and pnl is not None:
                existing["realized_pnl_usd"] = pnl
                if close_time_str:
                    existing.setdefault("closed_at_sgt", close_time_str)
                summary["backfilled"].append(trade_id)
                log.info("startup_oanda_reconcile: backfilled pnl=%.2f for trade %s",
                         pnl, trade_id)
            else:
                summary["skipped"] += 1
            continue

        # Closed today but not in local history — inject it
        initial_units = _safe_float(trade.get("initialUnits", 0))
        direction = "SELL" if initial_units < 0 else "BUY"
        record = {
            "timestamp_sgt":        close_time_str or now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            "closed_at_sgt":        close_time_str,
            "mode":                 "BROKER_RECONCILED",
            "instrument":           instrument,
            "direction":            direction,
            "setup":                "broker_reconciled_startup",
            "session":              "Reconciled",
            "macro_session":        "Reconciled",
            "score":                None,
            "threshold":            None,
            "entry":                _safe_float(trade.get("price")),
            "sl_price":             None,
            "tp_price":             None,
            "size":                 abs(initial_units),
            "cpr_width_pct":        None,
            "estimated_risk_usd":   None,
            "estimated_reward_usd": None,
            "spread_pips":          None,
            "stop_pips":            None,
            "tp_pips":              None,
            "levels":               {"source": "startup_oanda_reconcile"},
            "details":              "Injected by startup_oanda_reconcile — missing from history.json after redeploy.",
            "trade_id":             trade_id,
            "status":               "FILLED",
            "realized_pnl_usd":     pnl,
            "breakeven_moved":      False,
            "closed_alert_sent":    True,
        }
        history.append(record)
        history_by_trade_id[trade_id] = record
        summary["injected"].append(trade_id)
        log.warning("startup_oanda_reconcile: injected missing closed trade %s pnl=%.2f direction=%s",
                    trade_id, pnl or 0, direction)

    log.info(
        "startup_oanda_reconcile complete: injected=%d backfilled=%d skipped=%d",
        len(summary["injected"]), len(summary["backfilled"]), summary["skipped"],
    )
    return summary
