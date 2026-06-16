"""signal_logger.py — Cable AI Scalp v1.2

Logs every signal evaluation (score >= signal_log_min_score) to
/data/signal_log.csv for future AI/ML training data collection.

Captures both fired and non-fired signals with all scoring features.
When a trade closes, the outcome (TP/SL) is back-filled into the matching
signal row by matching on timestamp_sgt.

Controlled by settings:
  signal_logging_enabled: false  — toggle on/off without code changes
  signal_log_min_score:   3      — minimum score to capture (0-6)

File: /data/signal_log.csv  (cumulative, never auto-pruned)
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

log = logging.getLogger(__name__)

_SGT      = pytz.timezone("Asia/Singapore")
_DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
_LOG_FILE = _DATA_DIR / "signal_log.csv"

_FIELDNAMES = [
    "timestamp_sgt",
    "day_of_week",
    "session",
    "hour_sgt",
    "direction",
    "score",
    "ema_pts",
    "orb_pts",
    "cpr_pts",
    "setup",
    "h1_trend",
    "h1_aligned",
    "orb_age_min",
    "orb_formed",
    "cpr_width_pct",
    "atr",
    "spread_pips",
    "action",        # FIRED | WATCHED | BLOCKED_H1 | BLOCKED_NEWS | BLOCKED_SPREAD | BLOCKED_RR | NOISE
    "block_reason",  # detail when blocked
    "outcome",       # TP | SL | BE | (blank until trade closes)
    "pl_usd",        # filled when trade closes
    "trade_id",      # links to trade_history.json
]


def _ensure_header() -> None:
    """Write CSV header if file does not exist or is empty."""
    if not _LOG_FILE.exists() or _LOG_FILE.stat().st_size == 0:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
            writer.writeheader()


def log_signal(
    score: int,
    direction: str,
    session: str,
    levels: dict,
    action: str,
    block_reason: str = "",
    settings: dict | None = None,
    trade_id: str = "",
) -> None:
    """Append one signal evaluation row to signal_log.csv.

    Args:
        score:        Signal score (0–6)
        direction:    BUY | SELL | NONE
        session:      London | Tokyo | US | US_Cont | None
        levels:       Full levels dict from SignalEngine.analyze()
        action:       FIRED | WATCHED | BLOCKED_H1 | BLOCKED_NEWS |
                      BLOCKED_SPREAD | BLOCKED_RR | NOISE
        block_reason: Human-readable reason when action is BLOCKED_*
        settings:     Bot settings dict
        trade_id:     Trade ID when action=FIRED (for outcome back-fill)
    """
    if settings is None:
        settings = {}

    enabled   = bool(settings.get("signal_logging_enabled", False))
    min_score = int(settings.get("signal_log_min_score", 3))

    if not enabled:
        return

    if score < min_score and action == "NOISE":
        return  # below capture threshold — skip

    try:
        _ensure_header()

        now_sgt = datetime.now(_SGT)

        # Parse scoring breakdown from levels
        # ema_pts: fresh cross = 3, aligned = 1, none = 0
        # orb_pts: derived from score - ema_pts - cpr_pts
        ema_pts  = levels.get("ema_pts",  "")
        orb_pts  = levels.get("orb_pts",  "")
        cpr_pts  = levels.get("cpr_pts",  "")

        # Fallback: estimate ema_pts from setup name if not stored
        if ema_pts == "":
            setup = levels.get("setup", "")
            if "Fresh Cross" in setup:
                ema_pts = 3
            elif "Trend" in setup:
                ema_pts = 1
            else:
                ema_pts = 0

        row = {
            "timestamp_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            "day_of_week":   now_sgt.strftime("%A"),
            "session":       session or "",
            "hour_sgt":      now_sgt.hour,
            "direction":     direction,
            "score":         score,
            "ema_pts":       ema_pts,
            "orb_pts":       orb_pts,
            "cpr_pts":       cpr_pts,
            "setup":         levels.get("setup", ""),
            "h1_trend":      levels.get("h1_trend", ""),
            "h1_aligned":    levels.get("h1_aligned", ""),
            "orb_age_min":   levels.get("orb_age_min", ""),
            "orb_formed":    levels.get("orb_formed", ""),
            "cpr_width_pct": levels.get("cpr_width_pct", ""),
            "atr":           levels.get("atr", ""),
            "spread_pips":   levels.get("spread_pips", ""),
            "action":        action,
            "block_reason":  block_reason,
            "outcome":       "",   # back-filled when trade closes
            "pl_usd":        "",   # back-filled when trade closes
            "trade_id":      trade_id,
        }

        with open(_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES, extrasaction="ignore")
            writer.writerow(row)

        log.debug("Signal logged: %s score=%s action=%s", direction, score, action)

    except Exception as exc:
        log.warning("signal_logger: failed to write row: %s", exc)


def backfill_outcome(
    timestamp_sgt: str,
    outcome: str,
    pl_usd: float,
    trade_id: str,
    settings: dict | None = None,
) -> None:
    """Back-fill outcome (TP/SL/BE) and pl_usd into the matching FIRED row.

    Matches on timestamp_sgt and trade_id. Updates in-place by rewriting file.
    Only called when signal_logging_enabled = true.
    """
    if settings and not settings.get("signal_logging_enabled", False):
        return

    if not _LOG_FILE.exists():
        return

    try:
        rows = []
        updated = 0
        with open(_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if (row.get("action") == "FIRED" and
                        row.get("outcome") == "" and
                        (row.get("trade_id") == trade_id or
                         row.get("timestamp_sgt", "")[:16] == timestamp_sgt[:16])):
                    row["outcome"] = outcome
                    row["pl_usd"]  = round(pl_usd, 2)
                    row["trade_id"] = trade_id
                    updated += 1
                rows.append(row)

        if updated:
            with open(_LOG_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
                writer.writeheader()
                writer.writerows(rows)
            log.info("signal_logger: backfilled outcome=%s pl=$%.2f for trade %s",
                     outcome, pl_usd, trade_id)

    except Exception as exc:
        log.warning("signal_logger: backfill_outcome failed: %s", exc)


def get_signal_log_path() -> Path:
    """Return path to signal_log.csv for export."""
    return _LOG_FILE
