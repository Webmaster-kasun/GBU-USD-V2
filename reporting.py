"""reporting.py — Cable AI Scalp v1.2 Telegram Performance Reports

Three scheduled reports, all reading directly from /data/trade_history.json
on the Railway persistent volume. No archive file needed — the 90-day rolling
window covers all report periods.

Schedule (Asia/Singapore timezone, managed by scheduler.py):
  Daily    — Mon–Fri at 07:50 SGT       (covers prior trading day)
  Weekly   — Every Monday at 08:00 SGT  (covers Mon–Fri prior week)
  Monthly  — First Monday of each month at 08:10 SGT

Usage (called by scheduler.py):
    from reporting import send_daily_report, send_weekly_report, send_monthly_report
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from state_utils import TRADE_HISTORY_FILE
from telegram_alert import TelegramAlert
from telegram_templates import msg_daily_report, msg_weekly_report, msg_monthly_report
from ai_guard_tracker import summarize_ai_tracking, get_ai_guard_csv_path

log = logging.getLogger(__name__)
SGT = pytz.timezone("Asia/Singapore")


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_history() -> list:
    """Load trade_history.json from /data. Returns [] on any error."""
    if not TRADE_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(TRADE_HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("reporting: could not read trade_history.json: %s", exc)
        return []


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse a SGT timestamp string to an aware datetime, or None."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return SGT.localize(datetime.strptime(ts, fmt))
        except Exception:
            pass
    return None


def _filled(history: list) -> list:
    """Return only FILLED trades with a realized PnL."""
    return [
        t for t in history
        if t.get("status") == "FILLED" and isinstance(t.get("realized_pnl_usd"), (int, float))
    ]


def _trades_in_window(filled: list, start: datetime, end: datetime) -> list:
    """Filter filled trades whose timestamp_sgt falls within [start, end)."""
    result = []
    for t in filled:
        dt = _parse_ts(t.get("timestamp_sgt"))
        if dt and start <= dt < end:
            result.append(t)
    return result


# ── Stats builders ─────────────────────────────────────────────────────────────

def _stats(trades: list) -> dict:
    """Compute standard stats dict from a list of filled trades."""
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0,
            "net_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "win_rate": 0.0, "profit_factor": None,
            "avg_r": None, "max_win_streak": 0, "max_loss_streak": 0,
            "best_trade": None, "worst_trade": None,
            "instant_sl_count": 0,
        }

    wins   = [t for t in trades if t["realized_pnl_usd"] > 0]
    losses = [t for t in trades if t["realized_pnl_usd"] < 0]

    gross_profit = sum(t["realized_pnl_usd"] for t in wins)
    gross_loss   = abs(sum(t["realized_pnl_usd"] for t in losses))
    net_pnl      = gross_profit - gross_loss
    win_rate     = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    pf           = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    # R-multiple (uses estimated_risk_usd added by C-01 fix)
    r_vals = []
    for t in trades:
        risk = t.get("estimated_risk_usd")
        if risk and risk > 0:
            r_vals.append(round(t["realized_pnl_usd"] / risk, 2))
    avg_r = round(sum(r_vals) / len(r_vals), 2) if r_vals else None

    # Streaks
    outcomes = ["W" if t["realized_pnl_usd"] > 0 else "L" for t in trades]
    max_win_s = max_loss_s = cur = 0
    prev = None
    for o in outcomes:
        if o == prev:
            cur += 1
        else:
            cur = 1
            prev = o
        if o == "W":
            max_win_s = max(max_win_s, cur)
        else:
            max_loss_s = max(max_loss_s, cur)

    # Best and worst individual trade
    def _trade_summary(t):
        raw_time = t.get("closed_at_sgt") or t.get("timestamp_sgt") or ""
        hhmm = raw_time[11:16] if len(raw_time) >= 16 else raw_time
        return {"pnl": round(t["realized_pnl_usd"], 2), "time": hhmm}

    best_trade  = _trade_summary(max(trades, key=lambda t: t["realized_pnl_usd"]))
    worst_trade = _trade_summary(min(trades, key=lambda t: t["realized_pnl_usd"]))

    # Instant SL: a losing trade that closed within one candle (≤ cycle_minutes, ~5 min)
    def _trade_duration_min(t) -> int | None:
        open_ts  = t.get("timestamp_sgt", "")
        close_ts = t.get("closed_at_sgt", "")
        if not open_ts or not close_ts:
            return None
        try:
            from datetime import datetime
            fmt = "%Y-%m-%d %H:%M:%S"
            return int((datetime.strptime(close_ts[:19], fmt) -
                        datetime.strptime(open_ts[:19], fmt)).total_seconds() / 60)
        except Exception:
            return None

    instant_sl_count = sum(
        1 for t in losses
        if (_trade_duration_min(t) or 999) <= 5
    )

    return {
        "count":          len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "net_pnl":        round(net_pnl, 2),
        "gross_profit":   round(gross_profit, 2),
        "gross_loss":     round(gross_loss, 2),
        "win_rate":       win_rate,
        "profit_factor":  pf,
        "avg_r":          avg_r,
        "max_win_streak": max_win_s,
        "max_loss_streak":max_loss_s,
        "best_trade":     best_trade,
        "worst_trade":    worst_trade,
        "instant_sl_count": instant_sl_count,
    }


def _session_breakdown(trades: list) -> dict[str, dict]:
    """Win rate + PnL per macro session."""
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        sess = t.get("macro_session") or t.get("session") or "Unknown"
        buckets[sess].append(t)
    result = {}
    for sess, ts in sorted(buckets.items()):
        wins = [t for t in ts if t["realized_pnl_usd"] > 0]
        losses = [t for t in ts if t["realized_pnl_usd"] <= 0]
        result[sess] = {
            "count":    len(ts),
            "wins":     len(wins),
            "losses":   len(losses),
            "win_rate": round(len(wins) / len(ts) * 100, 1),
            "net_pnl":  round(sum(t["realized_pnl_usd"] for t in ts), 2),
        }
    return result


def _setup_breakdown(trades: list) -> dict[str, dict]:
    """Win rate + PnL per setup type."""
    buckets: dict[str, list] = defaultdict(list)
    for t in trades:
        setup = t.get("setup") or "Unknown"
        buckets[setup].append(t)
    result = {}
    for setup, ts in sorted(buckets.items()):
        wins = [t for t in ts if t["realized_pnl_usd"] > 0]
        result[setup] = {
            "count":    len(ts),
            "win_rate": round(len(wins) / len(ts) * 100, 1),
            "net_pnl":  round(sum(t["realized_pnl_usd"] for t in ts), 2),
        }
    return result


def _score_breakdown(trades: list) -> dict[int, dict]:
    """Win rate per signal score."""
    buckets: dict[int, list] = defaultdict(list)
    for t in trades:
        score = t.get("score")
        if score is not None:
            buckets[int(score)].append(t)
    result = {}
    for score in sorted(buckets.keys()):
        ts   = buckets[score]
        wins = [t for t in ts if t["realized_pnl_usd"] > 0]
        result[score] = {
            "count":    len(ts),
            "win_rate": round(len(wins) / len(ts) * 100, 1),
        }
    return result


# ── Window helpers ─────────────────────────────────────────────────────────────


def _h1_breakdown(trades: list) -> dict | None:
    """Return H1 filter split: aligned vs counter-trend stats.
    Returns None if no trades have h1_aligned field recorded.
    """
    aligned_trades  = [t for t in trades if t.get("h1_aligned") is True]
    counter_trades  = [t for t in trades if t.get("h1_aligned") is False]

    if not aligned_trades and not counter_trades:
        return None  # h1 data not recorded (old trades)

    def _grp(grp):
        wins   = sum(1 for t in grp if (t.get("realized_pnl_usd") or 0) > 0)
        losses = sum(1 for t in grp if (t.get("realized_pnl_usd") or 0) < 0)
        net    = round(sum(t.get("realized_pnl_usd") or 0 for t in grp), 2)
        wr     = round(wins / len(grp) * 100, 1) if grp else 0.0
        return {"count": len(grp), "wins": wins, "losses": losses,
                "net_pnl": net, "win_rate": wr}

    return {
        "aligned": _grp(aligned_trades),
        "counter": _grp(counter_trades),
    }


def _prior_trading_day(now: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) for the prior trading day in SGT.
    On Monday, looks back to Friday. Skips Saturday/Sunday.
    """
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day -= timedelta(days=1)
    # Step back over weekend
    while day.weekday() in (5, 6):
        day -= timedelta(days=1)
    return day, day + timedelta(days=1)


def _current_week_window(now: datetime) -> tuple[datetime, datetime]:
    """Return (Mon 00:00, now) for the current week."""
    days_since_mon = now.weekday()
    week_start = (now - timedelta(days=days_since_mon)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return week_start, now


def _prior_week_window(now: datetime) -> tuple[datetime, datetime, str]:
    """Return (Mon 00:00, Fri 23:59:59, label) for the prior Mon–Fri week."""
    days_since_mon = now.weekday()
    this_mon = (now - timedelta(days=days_since_mon)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prior_mon = this_mon - timedelta(days=7)
    prior_fri = this_mon - timedelta(seconds=1)
    label = f"{prior_mon.strftime('%d %b')} – {prior_fri.strftime('%d %b %Y')}"
    return prior_mon, this_mon, label


def _current_month_window(now: datetime) -> tuple[datetime, datetime]:
    """Return (1st of current month 00:00, now)."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start, now


def _prior_month_window(now: datetime) -> tuple[datetime, datetime, str]:
    """Return (1st of prior month, 1st of current month, label)."""
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prior = first_this - timedelta(seconds=1)
    first_prior = last_prior.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    label = first_prior.strftime("%B %Y")
    return first_prior, first_this, label


def _is_first_monday_of_month(now: datetime) -> bool:
    """True if today (SGT) is the first Monday of the calendar month."""
    return now.weekday() == 0 and now.day <= 7


# ── Report senders ─────────────────────────────────────────────────────────────

def send_daily_report() -> None:
    """Send daily performance summary at 04:00 SGT — dead zone start.

    Fires after US Continuation closes (03:59 SGT), capturing the full
    London + US trading day. Covers:
      - Current trading day  (16:00 yesterday → 03:59 today)
      - Session breakdown    (Tokyo / London / US merged)
      - Month-to-date        (1st → now)
      - Blocked cycles breakdown
    """
    try:
        from database import Database  # local import avoids circular at module level
        now    = datetime.now(SGT)
        filled = _filled(_load_history())

        # Prior day
        pd_start, pd_end   = _prior_trading_day(now)
        pd_trades          = _trades_in_window(filled, pd_start, pd_end)
        pd_stats           = _stats(pd_trades)
        pd_label           = pd_start.strftime("%A %d %b")

        # Week-to-date
        wtd_start, wtd_end = _current_week_window(now)
        wtd_trades         = _trades_in_window(filled, wtd_start, wtd_end)
        wtd_stats          = _stats(wtd_trades)

        # Month-to-date
        mtd_start, mtd_end = _current_month_window(now)
        mtd_trades         = _trades_in_window(filled, mtd_start, mtd_end)
        mtd_stats          = _stats(mtd_trades)

        # Open positions count (trades with no realized_pnl yet)
        open_count = sum(
            1 for t in _load_history()
            if t.get("status") == "FILLED" and t.get("realized_pnl_usd") is None
        )

        # Blocked cycles from DB — use UTC date prefix matching prior trading day
        blocked_spread = blocked_news = blocked_signal = 0
        try:
            db             = Database()
            utc_prefix     = pd_start.astimezone(pytz.utc).strftime("%Y-%m-%d")
            blocked_counts = db.query_blocked_cycles(utc_prefix)
            blocked_spread  = blocked_counts.get("spread_guard", 0)
            blocked_news    = blocked_counts.get("news_filter", 0)
            blocked_signal  = blocked_counts.get("signal_blocked", 0)
        except Exception as exc:
            log.warning("Could not query blocked cycles: %s", exc)

        # Previous day loss-cap flag
        try:
            from state_utils import load_json, OPS_STATE_FILE
            ops = load_json(OPS_STATE_FILE, {})
            yesterday_str = pd_start.strftime("%Y-%m-%d")
            pd_stats["ended_on_loss_cap"] = (
                ops.get("loss_cap_state") == f"loss_cap:{yesterday_str}"
            )
        except Exception:
            pass

        # Session breakdown — group by separate macro_session keys.
        # US and US_Cont are intentionally separate for reporting.
        session_order = [
            ("🗼 Tokyo",   "Tokyo"),
            ("🇬🇧 London", "London"),
            ("🗽 US",      "US"),
            ("🌙 US Cont.", "US_Cont"),
        ]
        session_stats = {}
        for label, macro_key in session_order:
            sess_trades = [t for t in pd_trades
                           if (t.get("macro_session") or t.get("window") or "") == macro_key]
            if sess_trades:
                session_stats[label] = _stats(sess_trades)

        # Day total — use pd_trades (same window as session breakdown)
        # pd_trades = full prior trading day (00:00 → 24:00 SGT)
        # Fixes: day total was using 16:00 SGT start, missing Tokyo trades
        today_stats = _stats(pd_trades)
        today_label = pd_start.strftime("%a %d %b %Y")

        ai_stats = summarize_ai_tracking(pd_start, pd_end)

        msg = msg_daily_report(
            day_label       = today_label,
            day_stats       = today_stats,
            wtd_stats       = wtd_stats,
            mtd_stats       = mtd_stats,
            open_count      = open_count,
            report_time     = now.strftime("%H:%M SGT"),
            blocked_spread  = blocked_spread,
            blocked_news    = blocked_news,
            blocked_signal  = blocked_signal,
            session_stats   = session_stats,
            ai_stats        = ai_stats,
        )
        ok = TelegramAlert().send(msg)
        if ok:
            log.info("Daily report sent.")
        else:
            log.warning("Daily report send failed.")
    except Exception as exc:
        log.exception("send_daily_report error: %s", exc)


def send_weekly_report() -> None:
    """Send weekly performance report every Monday at 08:00 SGT.

    Covers the prior Mon–Fri trading week with full breakdown.
    """
    try:
        now    = datetime.now(SGT)
        filled = _filled(_load_history())

        pw_start, pw_end, pw_label = _prior_week_window(now)
        pw_trades                  = _trades_in_window(filled, pw_start, pw_end)
        pw_stats                   = _stats(pw_trades)
        sessions                   = _session_breakdown(pw_trades)
        setups                     = _setup_breakdown(pw_trades)

        # By Pair breakdown
        pw_pairs: dict = {}
        for t in pw_trades:
            instr = (t.get("instrument") or "").replace("_", "/")
            if instr not in pw_pairs:
                pw_pairs[instr] = []
            pw_pairs[instr].append(t)
        pair_stats = {k: _stats(v) for k, v in pw_pairs.items()}

        h1_stats = _h1_breakdown(pw_trades)
        ai_stats = summarize_ai_tracking(pw_start, pw_end)

        msg = msg_weekly_report(
            week_label = pw_label,
            stats      = pw_stats,
            sessions   = sessions,
            setups     = setups,
            pairs      = pair_stats,
            h1_stats   = h1_stats,
            ai_stats   = ai_stats,
            report_time= now.strftime("%H:%M SGT"),
        )
        ok = TelegramAlert().send(msg)
        if ok:
            log.info("Weekly report sent.")
        else:
            log.warning("Weekly report send failed.")
    except Exception as exc:
        log.exception("send_weekly_report error: %s", exc)



def send_weekly_export() -> None:
    """Send trade history as a CSV file via Telegram every Monday 08:05 SGT.

    Fires 5 minutes after the weekly performance report (08:00 SGT) so the
    text report arrives first, then the CSV follows.

    Columns mirror the monthly CSV export for consistency. All filled trades
    are included (not just the prior week) so the file is always a complete
    cumulative record. The prior-week count is shown in the caption.
    """
    import csv
    import io
    import os
    from pathlib import Path

    try:
        now    = datetime.now(SGT)
        alert  = TelegramAlert()
        filled = _filled(_load_history())

        if not filled:
            alert.send("📎 Weekly CSV export: no filled trades on record yet.")
            log.warning("send_weekly_export: no filled trades found.")
            return

        pw_start, pw_end, pw_label = _prior_week_window(now)
        pw_trades  = _trades_in_window(filled, pw_start, pw_end)
        h1_counter = sum(1 for t in pw_trades if not t.get("h1_aligned", True))
        h1_aligned = sum(1 for t in pw_trades if t.get("h1_aligned", True))

        fieldnames = [
            "date_sgt", "time_sgt", "day_of_week",
            "session", "direction", "score", "setup",
            "entry_price", "sl_price", "tp_price",
            "result", "pl_usd", "balance",
            "h1_trend", "h1_aligned",
            "ema_pts", "orb_pts", "cpr_pts",
            "duration_min", "spread_pips",
            "units", "position_usd",
        ]

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for t in sorted(filled, key=lambda x: x.get("timestamp_sgt", "")):
            ts = t.get("timestamp_sgt", "")
            try:
                dt_obj   = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                date_str = dt_obj.strftime("%Y-%m-%d")
                time_str = dt_obj.strftime("%H:%M")
                dow_str  = dt_obj.strftime("%A")
            except Exception:
                date_str = ts[:10]
                time_str = ts[11:16]
                dow_str  = ""

            dur = None
            try:
                d1 = datetime.strptime(t.get("timestamp_sgt", "")[:19], "%Y-%m-%d %H:%M:%S")
                d2 = datetime.strptime(t.get("closed_at_sgt", "")[:19], "%Y-%m-%d %H:%M:%S")
                dur = int((d2 - d1).total_seconds() / 60)
            except Exception:
                pass

            writer.writerow({
                "date_sgt":     date_str,
                "time_sgt":     time_str,
                "day_of_week":  dow_str,
                "session":      t.get("macro_session") or t.get("session", ""),
                "direction":    t.get("direction", ""),
                "score":        t.get("score", ""),
                "setup":        t.get("setup", ""),
                "entry_price":  t.get("entry_price") or t.get("fill_price", ""),
                "sl_price":     t.get("sl_price", ""),
                "tp_price":     t.get("tp_price", ""),
                "result":       "TP" if (t.get("realized_pnl_usd") or 0) > 0 else (
                                "SL" if (t.get("realized_pnl_usd") or 0) < 0 else "BE"),
                "pl_usd":       round(t.get("realized_pnl_usd") or 0, 2),
                "balance":      round(t.get("balance_after") or 0, 2),
                "h1_trend":     t.get("h1_trend", ""),
                "h1_aligned":   t.get("h1_aligned", ""),
                "ema_pts":      t.get("ema_pts", ""),
                "orb_pts":      t.get("orb_pts", ""),
                "cpr_pts":      t.get("cpr_pts", ""),
                "duration_min": dur or "",
                "spread_pips":  t.get("spread_pips", ""),
                "units":        t.get("units", ""),
                "position_usd": t.get("position_usd", ""),
            })

        csv_bytes = buf.getvalue().encode("utf-8")
        data_dir  = Path(os.getenv("DATA_DIR", "/data"))
        filename  = f"cable_ai_scalp_trades_to_{now.strftime('%Y-%m-%d')}.csv"
        tmp_path  = data_dir / filename
        tmp_path.write_bytes(csv_bytes)

        caption = (
            f"📎 trade history CSV — {pw_label}\n"
            f"{len(filled)} total filled trades\n"
            f"This week: {len(pw_trades)} trades  "
            f"({h1_aligned} H1-aligned  /  {h1_counter} counter-trend)"
        )

        try:
            ok = alert.send_document(tmp_path, caption=caption)
            if ok:
                log.info("Weekly CSV export sent: %d total records, %d this week.",
                         len(filled), len(pw_trades))
            else:
                log.warning("Weekly CSV export: send_document failed.")
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    except Exception as exc:
        log.exception("send_weekly_export error: %s", exc)


def send_monthly_report() -> None:
    """Send monthly performance report on the first Monday of each month at 08:00 SGT.

    Covers the prior full calendar month with session, setup, and score breakdown.
    Also shows month-over-month PnL delta when prior-prior month data exists.
    The first-Monday guard is enforced here so the scheduler can use a simple
    weekly cron without needing a complex calendar trigger.
    """
    try:
        now = datetime.now(SGT)

        if not _is_first_monday_of_month(now):
            log.info("Monthly report skipped — not first Monday of month (%s)", now.strftime("%d %b"))
            return

        filled = _filled(_load_history())

        pm_start, pm_end, pm_label = _prior_month_window(now)
        pm_trades                  = _trades_in_window(filled, pm_start, pm_end)
        pm_stats                   = _stats(pm_trades)
        sessions                   = _session_breakdown(pm_trades)
        setups                     = _setup_breakdown(pm_trades)
        scores                     = _score_breakdown(pm_trades)

        # Month-over-month delta: compare prior month PnL vs the month before that
        ppm_start = (pm_start.replace(day=1) - timedelta(days=1)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        ppm_trades = _trades_in_window(filled, ppm_start, pm_start)
        ppm_pnl    = round(sum(t["realized_pnl_usd"] for t in ppm_trades), 2) if ppm_trades else None
        mom_delta  = round(pm_stats["net_pnl"] - ppm_pnl, 2) if ppm_pnl is not None else None

        h1_stats = _h1_breakdown(pm_trades)
        ai_stats = summarize_ai_tracking(pm_start, pm_end)

        msg = msg_monthly_report(
            month_label = pm_label,
            stats       = pm_stats,
            sessions    = sessions,
            setups      = setups,
            scores      = scores,
            h1_stats    = h1_stats,
            ai_stats    = ai_stats,
            mom_delta   = mom_delta,
            prior_month_pnl = ppm_pnl,
            report_time = now.strftime("%H:%M SGT"),
        )
        ok = TelegramAlert().send(msg)
        if ok:
            log.info("Monthly report sent for %s.", pm_label)
        else:
            log.warning("Monthly report send failed.")
    except Exception as exc:
        log.exception("send_monthly_report error: %s", exc)


# ── Cumulative monthly CSV export ──────────────────────────────────────────────

# deployment start date — cumulative CSV always covers from this date forward
_DEPLOY_START = "2026-04-26"

def send_monthly_csv_export() -> None:
    """Send cumulative CSV of all Cable AI Scalp trades on the last day of each month at 08:30 SGT.

    The CSV grows every month:
      Apr 30  → April trades only
      May 31  → April + May trades
      Jun 30  → April + May + June trades
      ...

    All trades from deployment start date (2026-04-26) to today are included.
    File is sent via Telegram sendDocument and deleted after sending.
    """
    import csv
    import io
    import os
    import tempfile
    from pathlib import Path

    try:
        now    = datetime.now(SGT)
        filled = _filled(_load_history())

        # Filter from deployment start date to now
        deploy_start = SGT.localize(datetime.strptime(_DEPLOY_START, "%Y-%m-%d"))
        trades = _trades_in_window(filled, deploy_start, now)

        if not trades:
            TelegramAlert().send(
                f"📎 Monthly CSV Export — {now.strftime('%b %Y')}\n"
                f"No trades found since {_DEPLOY_START}."
            )
            log.warning("send_monthly_csv_export: no trades found since %s", _DEPLOY_START)
            return

        # Build CSV in memory
        fieldnames = [
            "date_sgt", "time_sgt", "day_of_week",
            "session", "direction", "score", "setup",
            "entry_price", "sl_price", "tp_price",
            "result", "pl_usd", "balance",
            "h1_trend", "h1_aligned",
            "ema_pts", "orb_pts", "cpr_pts",
            "duration_min", "spread_pips",
            "units", "position_usd",
        ]

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        for t in sorted(trades, key=lambda x: x.get("timestamp_sgt", "")):
            ts = t.get("timestamp_sgt", "")
            try:
                dt_obj = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                date_str = dt_obj.strftime("%Y-%m-%d")
                time_str = dt_obj.strftime("%H:%M")
                dow_str  = dt_obj.strftime("%A")
            except Exception:
                date_str = ts[:10]
                time_str = ts[11:16]
                dow_str  = ""

            # Duration in minutes
            dur = None
            try:
                open_ts  = t.get("timestamp_sgt", "")
                close_ts = t.get("closed_at_sgt", "")
                if open_ts and close_ts:
                    d1 = datetime.strptime(open_ts[:19], "%Y-%m-%d %H:%M:%S")
                    d2 = datetime.strptime(close_ts[:19], "%Y-%m-%d %H:%M:%S")
                    dur = int((d2 - d1).total_seconds() / 60)
            except Exception:
                pass

            writer.writerow({
                "date_sgt":    date_str,
                "time_sgt":    time_str,
                "day_of_week": dow_str,
                "session":     t.get("macro_session") or t.get("session", ""),
                "direction":   t.get("direction", ""),
                "score":       t.get("score", ""),
                "setup":       t.get("setup", ""),
                "entry_price": t.get("entry_price") or t.get("fill_price", ""),
                "sl_price":    t.get("sl_price", ""),
                "tp_price":    t.get("tp_price", ""),
                "result":      "TP" if (t.get("realized_pnl_usd") or 0) > 0 else (
                               "SL" if (t.get("realized_pnl_usd") or 0) < 0 else "BE"),
                "pl_usd":      round(t.get("realized_pnl_usd") or 0, 2),
                "balance":     round(t.get("balance_after") or 0, 2),
                "h1_trend":    t.get("h1_trend", ""),
                "h1_aligned":  t.get("h1_aligned", ""),
                "ema_pts":     t.get("ema_pts", ""),
                "orb_pts":     t.get("orb_pts", ""),
                "cpr_pts":     t.get("cpr_pts", ""),
                "duration_min": dur or "",
                "spread_pips": t.get("spread_pips", ""),
                "units":       t.get("units", ""),
                "position_usd": t.get("position_usd", ""),
            })

        csv_bytes = buf.getvalue().encode("utf-8")

        # Write to temp file in /data
        data_dir  = Path(os.getenv("DATA_DIR", "/data"))
        filename  = f"cable_ai_scalp_trades_to_{now.strftime('%Y-%m-%d')}.csv"
        tmp_path  = data_dir / filename

        tmp_path.write_bytes(csv_bytes)

        # Calculate stats for caption
        wins    = sum(1 for t in trades if (t.get("realized_pnl_usd") or 0) > 0)
        losses  = sum(1 for t in trades if (t.get("realized_pnl_usd") or 0) < 0)
        net_pnl = round(sum(t.get("realized_pnl_usd") or 0 for t in trades), 2)
        wr      = round(wins / len(trades) * 100, 1) if trades else 0
        months  = sorted(set(
            (t.get("timestamp_sgt") or "")[:7]
            for t in trades
            if len(t.get("timestamp_sgt") or "") >= 7
        ))
        period = f"{months[0]} → {months[-1]}" if months else _DEPLOY_START

        caption = (
            f"📊 Cable AI Scalp v1.2 — Cumulative Trade Log\n"
            f"Period: {period}\n"
            f"Trades: {len(trades)}  ({wins}W / {losses}L)  WR {wr}%\n"
            f"Net P&L: ${net_pnl:+.2f}\n"
            f"Generated: {now.strftime('%d %b %Y %H:%M SGT')}"
        )

        alert = TelegramAlert()

        # Update mime type for CSV
        if not tmp_path.exists():
            alert.send("📎 Monthly CSV: file write failed.")
            return

        # Send using sendDocument with CSV mime type
        import requests
        from config_loader import load_secrets
        secrets  = load_secrets()
        token    = secrets.get("TELEGRAM_TOKEN", "")
        chat_id  = secrets.get("TELEGRAM_CHAT_ID", "")
        url      = f"https://api.telegram.org/bot{token}/sendDocument"

        with open(tmp_path, "rb") as fh:
            r = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, fh, "text/csv")},
                timeout=30,
            )

        if r.status_code == 200:
            log.info("Monthly CSV export sent: %s (%d trades)", filename, len(trades))
        else:
            log.warning("Monthly CSV export failed: HTTP %s: %s",
                        r.status_code, r.text[:200])

        # Clean up temp file
        try:
            tmp_path.unlink()
        except Exception:
            pass

    except Exception as exc:
        log.exception("send_monthly_csv_export error: %s", exc)


def send_monthly_signal_export() -> None:
    """Send signal_log.csv as Telegram file on last day of month at 08:35 SGT.

    Only fires if signal_logging_enabled = true and signal_log.csv exists.
    Sent 5 minutes after the trade CSV export (08:30 SGT).
    """
    try:
        from config_loader import load_settings
        import requests

        settings = load_settings()
        if not settings.get("signal_logging_enabled", False):
            log.info("send_monthly_signal_export: signal logging disabled — skipping.")
            return

        from signal_logger import get_signal_log_path
        sig_path = get_signal_log_path()

        if not sig_path.exists() or sig_path.stat().st_size == 0:
            log.warning("send_monthly_signal_export: signal_log.csv not found or empty.")
            TelegramAlert().send("📎 Monthly Signal Export: no signal log found.")
            return

        now = datetime.now(SGT)

        # Count rows
        import csv as _csv
        total_rows = 0
        fired = watched = blocked = 0
        with open(sig_path, "r", newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                total_rows += 1
                action = row.get("action", "")
                if action == "FIRED":      fired   += 1
                elif action == "WATCHED":  watched += 1
                elif action.startswith("BLOCKED"): blocked += 1

        filename = f"cable_scalp_v19_signals_to_{now.strftime('%Y-%m-%d')}.csv"
        caption = (
            f"📡 Cable AI Scalp v1.2 — Signal Log\n"
            f"Period: 2026-04-26 → {now.strftime('%d %b %Y')}\n"
            f"Rows: {total_rows}  |  Fired: {fired}  |  Watched: {watched}  |  Blocked: {blocked}\n"
            f"Generated: {now.strftime('%d %b %Y %H:%M SGT')}"
        )

        from config_loader import load_secrets
        secrets = load_secrets()
        token   = secrets.get("TELEGRAM_TOKEN", "")
        chat_id = secrets.get("TELEGRAM_CHAT_ID", "")
        url     = f"https://api.telegram.org/bot{token}/sendDocument"

        with open(sig_path, "rb") as fh:
            r = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, fh, "text/csv")},
                timeout=30,
            )

        if r.status_code == 200:
            log.info("Monthly signal export sent: %d rows", total_rows)
        else:
            log.warning("Monthly signal export failed: HTTP %s: %s",
                        r.status_code, r.text[:200])

    except Exception as exc:
        log.exception("send_monthly_signal_export error: %s", exc)
