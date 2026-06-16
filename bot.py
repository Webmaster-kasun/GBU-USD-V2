"""Main orchestrator for Cable Scalp v2.0 — GBP/USD M5 Scalper (No-AI, Rule-Based)

Dedicated GBP/USD (Cable) scalping bot. Single pair, clean data, focused strategy.

Active sessions: London 16–20 SGT (≥4/6), US Cont. 00–03 SGT (≥4/6), Tokyo 08–15 SGT (≥5/6)
Disabled: US session 21–23 SGT (0% WR in live testing)

All configuration lives in settings.json under the top-level "pairs" key.
Per-pair values override global defaults.

Architecture:
  run_bot_cycle() loops over every enabled pair each 5-minute cycle.
  For each pair it runs three phases:
    _guard_phase()      — pre-trade checks (session, caps, cooldown, OANDA)
    _signal_phase()     — EMA + ORB + CPR scoring, sizing, margin guard
    _execution_phase()  — order placement and history persistence

State isolation:
  Trade history (trade_history.json) is shared; queries filter by "instrument".
  Signal cache, ops state, and cooldown state are per-pair files, e.g.:
    score_cache_gbpusd.json / ops_state_gbpusd.json / runtime_pair_gbpusd.json
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from ai_news_guard import ai_news_guard
from ai_guard_tracker import (
    record_ai_decision, link_trade, mark_trade_failed,
    backfill_actual_trade_result, update_blocked_virtual_outcomes,
)
from calendar_fetcher import run_fetch as refresh_calendar
from config_loader import DATA_DIR, get_bool_env, load_settings
from database import Database
from logging_utils import configure_logging, get_logger
from news_filter import NewsFilter
from oanda_trader import OandaTrader
from signals import SignalEngine, score_to_position_usd
from signal_logger import log_signal, backfill_outcome, get_signal_log_path
from startup_checks import run_startup_checks
from state_utils import (
    RUNTIME_STATE_FILE, SCORE_CACHE_FILE, OPS_STATE_FILE, TRADE_HISTORY_FILE,
    update_runtime_state, load_json, save_json, parse_sgt_timestamp,
)
from telegram_alert import TelegramAlert
from telegram_templates import (
    msg_signal_update, msg_trade_opened, msg_breakeven, msg_trade_closed,
    msg_news_block, msg_news_penalty, msg_cooldown_started, msg_daily_cap,
    msg_spread_skip, msg_order_failed, msg_error, msg_friday_cutoff,
    msg_margin_adjustment, msg_new_day_resume, msg_session_open, msg_ai_guard_result,
    _clean_session,
)
from reconcile_state import reconcile_runtime_state, startup_oanda_reconcile

configure_logging()
log = get_logger(__name__)

SGT          = pytz.timezone("Asia/Singapore")
HISTORY_FILE = TRADE_HISTORY_FILE

_startup_reconcile_done: bool = False

SESSION_BANNERS = {
    "London": "🇬🇧 LONDON",
    "US":     "🗽 US",
    "US_Cont": "🌙 US Cont.",
    "Tokyo":  "🗼 TOKYO",
}


def _build_sessions(settings: dict) -> list:
    """Build the SESSIONS list from settings.
    US windows excluded when us_session_start_hour >= 99 (disabled sentinel).
    Tuple format: (name, macro, start_hour, end_hour, fallback_threshold).
    """
    lon_s  = int(settings.get("london_session_start_hour",    16))
    lon_e  = int(settings.get("london_session_end_hour",      20))
    us_s   = int(settings.get("us_session_start_hour",        21))
    us_e   = int(settings.get("us_session_end_hour",          23))
    us_e2  = int(settings.get("us_session_early_end_hour",     3))
    tok_s  = int(settings.get("tokyo_session_start_hour",      8))
    tok_e  = int(settings.get("tokyo_session_end_hour",       15))
    sessions = [
        ("Tokyo Window",  "Tokyo",  tok_s, tok_e, 5),
        ("London Window", "London", lon_s, lon_e, 4),
    ]
    if us_s  < 99: sessions.append(("US session", "US", us_s,  us_e,  4))
    if us_e2 < 99: sessions.append(("US Cont.", "US_Cont", 0,     us_e2, 4))
    return sessions


# ── Pair helpers ──────────────────────────────────────────────────────────────

def get_enabled_pairs(settings: dict) -> list[tuple[str, dict]]:
    """Return [(instrument, pair_cfg), ...] for all enabled pairs in order."""
    pairs = settings.get("pairs", {})
    return [
        (instr, cfg)
        for instr, cfg in pairs.items()
        if isinstance(cfg, dict) and cfg.get("enabled", True)
    ]


def get_effective_settings(global_s: dict, pair_cfg: dict) -> dict:
    """Merge global settings with pair-specific overrides.
    Pair values take priority; everything else falls back to global.
    """
    merged = dict(global_s)
    merged.update(pair_cfg)
    return merged


def _pair_key(instrument: str) -> str:
    """'GBP_USD' → 'gbpusd'  (used as file-name suffix)."""
    return instrument.lower().replace("_", "")


def _pair_state_file(base: Path, instrument: str) -> Path:
    """Return a per-pair variant of a state file.
    e.g. score_cache.json → score_cache_gbpusd.json
    """
    return base.parent / f"{base.stem}_{_pair_key(instrument)}{base.suffix}"


def _pair_runtime_file(instrument: str) -> Path:
    """Per-pair runtime state (cooldown, last SL close)."""
    return RUNTIME_STATE_FILE.parent / f"runtime_pair_{_pair_key(instrument)}.json"


def _pip_size(settings: dict) -> float:
    return float(settings.get("pip_size", 0.0001) or 0.0001)


def _pip_dp(pip: float) -> int:
    """Decimal places for price rounding given pip size."""
    if pip <= 0.0001: return 5   # GBP_USD (Cable)
    if pip <= 0.01:   return 3   # JPY pairs (not used in Cable AI Scalp v1.2)
    return 2


# ── Trading day ───────────────────────────────────────────────────────────────

def get_trading_day(now_sgt: datetime, day_start_hour: int = 8) -> str:
    if now_sgt.hour < day_start_hour:
        return (now_sgt - timedelta(days=1)).strftime("%Y-%m-%d")
    return now_sgt.strftime("%Y-%m-%d")


def _clean_reason(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "No reason available"
    for part in reversed([p.strip() for p in text.split("|") if p.strip()]):
        plain = re.sub(r"^[^A-Za-z0-9]+", "", part).strip()
        if plain:
            return plain[:120]
    return text[:120]


def _build_signal_checks(score, direction, rr_ratio=None, tp_pct=None,
                         spread_pips=None, spread_limit=None, session_ok=True,
                         news_ok=True, open_trade_ok=True, margin_ok=None,
                         cooldown_ok=True, signal_threshold=4, min_rr_ratio=1.6):
    mandatory_checks = [
        (f"Score >= {signal_threshold}",
         score >= signal_threshold and direction != "NONE", f"{score}/6"),
        (f"RR >= {min_rr_ratio:.1f}",
         None if rr_ratio is None else rr_ratio >= min_rr_ratio,
         "n/a" if rr_ratio is None else f"{rr_ratio:.2f}"),
    ]
    quality_checks = [
        ("TP >= 0.35%", None if tp_pct is None else tp_pct >= 0.35,
         "n/a" if tp_pct is None else f"{tp_pct:.4f}%"),
    ]
    execution_checks = [
        ("Session active",  session_ok,    "active"  if session_ok   else "inactive"),
        ("News clear",      news_ok,       "clear"   if news_ok      else "blocked"),
        ("Cooldown clear",  cooldown_ok,   "clear"   if cooldown_ok  else "active"),
        ("No open trade",   open_trade_ok, "ready"   if open_trade_ok else "existing position"),
        ("Spread OK",
         None if spread_pips is None or spread_limit is None
              else spread_pips <= spread_limit,
         "n/a" if spread_pips is None or spread_limit is None
              else f"{spread_pips}/{spread_limit} pips"),
        ("Margin OK", margin_ok,
         "n/a" if margin_ok is None else ("pass" if margin_ok else "insufficient")),
    ]
    return mandatory_checks, quality_checks, execution_checks


def _signal_payload(**kwargs):
    mc, qc, ec = _build_signal_checks(**kwargs)
    return {"mandatory_checks": mc, "quality_checks": qc, "execution_checks": ec}


# ── Settings ──────────────────────────────────────────────────────────────────

def validate_settings(settings: dict) -> dict:
    required = ["pairs"]  # Cable AI Scalp v1.2: pair_sl_tp fixed pips used exclusively
    missing  = [k for k in required if k not in settings]
    if missing:
        raise ValueError(f"Missing required settings keys: {missing}")

    settings.setdefault("signal_threshold",           4)
    # 2.4% risk/trade on $2,000 account (full $48) / 1.5% (partial $30)
    settings.setdefault("position_full_usd",          60)
    settings.setdefault("position_partial_usd",       45)
    settings.setdefault("account_balance_override",   0)
    settings.setdefault("enabled",                    True)
    settings.setdefault("pip_size",                   0.0001)
    # be_trigger_pips: break-even trigger in pips
    settings.setdefault("be_trigger_pips",             20)
    settings.setdefault("trading_day_start_hour_sgt", 8)
    settings.setdefault("max_losing_trades_session",  2)
    settings.setdefault("exhaustion_atr_mult",        3.0)
    settings.setdefault("margin_safety_factor",       0.6)
    settings.setdefault("margin_retry_safety_factor", 0.4)
    settings.setdefault("margin_rate_override",       0.0)
    settings.setdefault("auto_scale_on_margin_reject",True)
    settings.setdefault("telegram_show_margin",       True)
    # Suppress WATCHING alerts for signals below this score (0 = send all)
    settings.setdefault("telegram_min_score_alert",   4)
    settings.setdefault("signal_logging_enabled",     False)
    settings.setdefault("signal_log_min_score",        3)
    settings.setdefault("max_trades_us_cont",          4)
    settings.setdefault("friday_cutoff_hour_sgt",     23)
    settings.setdefault("friday_cutoff_minute_sgt",   0)
    settings.setdefault("news_lookahead_min",         120)
    settings.setdefault("news_medium_penalty_score",  -1)
    settings.setdefault("news_relevant_currencies", ["GBP", "USD"])
    settings.setdefault("news_fail_closed",           True)
    settings.setdefault("calendar_cache_max_age_hours", 24)
    # AI News Guard removed in v2.0 — rule-based NewsFilter handles all news risk.
    settings.setdefault("loss_streak_cooldown_min",   60)
    settings.setdefault("orb_fresh_minutes",          60)
    settings.setdefault("orb_aging_minutes",          120)
    settings.setdefault("min_rr_ratio",               1.3)
    settings.setdefault("rr_ratio",                   1.67)  # fallback only — pair_sl_tp always used
    settings.setdefault("ema_fast_period",            9)
    settings.setdefault("ema_slow_period",            21)
    settings.setdefault("orb_formation_minutes",      15)
    settings.setdefault("calendar_prune_days_ahead",  21)
    settings.setdefault("startup_dedup_seconds",      90)
    settings.setdefault("atr_period",                 14)
    settings.setdefault("m5_candle_count",            40)
    settings.setdefault("spread_limits",              {"London": 5, "US": 5, "US_Cont": 5, "Tokyo": 4})
    settings.setdefault("max_trades_day",             12)
    settings.setdefault("max_losing_trades_day",      4)
    settings.setdefault("max_trades_london",          4)
    settings.setdefault("max_trades_us",              4)
    settings.setdefault("max_trades_us_cont",         4)
    # session window hours
    settings.setdefault("london_session_start_hour",  16)
    settings.setdefault("london_session_end_hour",    20)
    settings.setdefault("us_session_start_hour",      99)  # v1.9: US 21-23 disabled (0% WR)
    settings.setdefault("us_session_end_hour",        99)  # v1.9: US 21-23 disabled
    settings.setdefault("us_session_early_end_hour",   3)  # v1.9: US Cont 00-03 re-enabled
    settings.setdefault("dead_zone_start_hour",        4)   # 04:00 SGT — pre-Tokyo gap
    settings.setdefault("dead_zone_end_hour",           7)   # 07:59 SGT end
    # report schedule times (SGT)
    settings.setdefault("daily_report_hour_sgt",       4)   # 04:00 SGT — dead zone start
    settings.setdefault("daily_report_minute_sgt",     0)
    settings.setdefault("weekly_report_hour_sgt",      8)
    settings.setdefault("weekly_report_minute_sgt",   15)
    settings.setdefault("monthly_report_hour_sgt",     8)
    settings.setdefault("monthly_report_minute_sgt",   0)
    # Tokyo/Asian session
    settings.setdefault("tokyo_session_start_hour",    8)
    settings.setdefault("tokyo_session_end_hour",     15)
    settings.setdefault("max_trades_tokyo",           4)
    # global concurrent-trade cap (0 = per-pair limits only)
    settings.setdefault("max_total_open_trades",       1)
    # TP2 reference RR multiplier for the trade opened Telegram alert
    settings.setdefault("tp2_rr_reference",            3.0)
    # minimum units after margin guard — reject micro-orders gracefully
    settings.setdefault("min_trade_units",           1000)
    # GBP/USD only
    settings.setdefault("pair_sl_tp", {
        "GBP_USD": {"sl_pips": 18, "tp_pips": 30, "pip_value_usd": 10.0, "be_trigger_pips": 20},
    })
    # dead zone = pre-Tokyo gap 04:00–07:59 SGT (overrides any stale setdefault above)
    settings["dead_zone_start_hour"] = int(settings.get("dead_zone_start_hour", 4))
    settings["dead_zone_end_hour"]   = int(settings.get("dead_zone_end_hour",   7))
    # Ensure Tokyo threshold is present in session_thresholds
    st = settings.setdefault("session_thresholds", {})
    st.setdefault("Tokyo", 5)
    st.setdefault("US_Cont", 4)

    if int(settings.get("loss_streak_cooldown_min", 30)) < 0:
        raise ValueError("loss_streak_cooldown_min must be >= 0")

    return settings


def is_friday_cutoff(now_sgt: datetime, settings: dict) -> bool:
    if now_sgt.weekday() != 4:
        return False
    ch = int(settings.get("friday_cutoff_hour_sgt", 23))
    cm = int(settings.get("friday_cutoff_minute_sgt", 0))
    return now_sgt.hour > ch or (now_sgt.hour == ch and now_sgt.minute >= cm)


# ── Trade history helpers ──────────────────────────────────────────────────────

def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history: list):
    save_json(HISTORY_FILE, history)


def prune_old_trades(history: list, settings: dict | None = None) -> list:
    retention_days = int((settings or {}).get("db_retention_days", 90))
    cutoff = datetime.now(SGT) - timedelta(days=retention_days)
    active, pruned = [], 0
    for trade in history:
        ts = trade.get("timestamp_sgt", "")
        try:
            dt = SGT.localize(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            if dt < cutoff:
                pruned += 1
            else:
                active.append(trade)
        except Exception:
            active.append(trade)
    if pruned:
        log.info("Pruned %d trade(s) older than %d days | Active: %d",
                 pruned, retention_days, len(active))
    return active


# ── Session helpers ────────────────────────────────────────────────────────────

def get_session(now: datetime, settings: dict = None):
    h       = now.hour
    s       = settings or {}
    st      = s.get("session_thresholds", {})
    sessions = _build_sessions(s)
    for name, macro, start, end, fallback_thr in sessions:
        if start <= h <= end:
            return name, macro, int(st.get(macro, fallback_thr))
    return None, None, None


def is_dead_zone_time(now_sgt: datetime, settings: dict | None = None) -> bool:
    s        = settings or {}
    dz_start = int(s.get("dead_zone_start_hour", 4))   # 04:00 SGT
    dz_end   = int(s.get("dead_zone_end_hour",   7))   # 07:59 SGT
    return dz_start <= now_sgt.hour <= dz_end


def get_window_key(session_name: str | None) -> str | None:
    if session_name == "London Window": return "London"
    if session_name in ("US Window", "US session"): return "US"
    if session_name == "US Cont.":      return "US_Cont"
    if session_name == "Tokyo Window":  return "Tokyo"
    return None


def get_window_trade_cap(window_key: str | None, settings: dict) -> int | None:
    if window_key == "London": return int(settings.get("max_trades_london", 4))
    if window_key == "US":      return int(settings.get("max_trades_us",      4))
    if window_key == "US_Cont": return int(settings.get("max_trades_us_cont", 4))
    if window_key == "Tokyo":  return int(settings.get("max_trades_tokyo",  4))
    return None


def window_trade_count(history: list, today_str: str,
                       window_key: str, instrument: str) -> int:
    aliases = {
        "London": {"London", "London Window"},
        "US":      {"US", "US Window", "US session"},
        "US_Cont": {"US_Cont", "US Cont."},
        "Tokyo":  {"Tokyo", "Tokyo Window"},
    }
    valid = aliases.get(window_key, {window_key})
    return sum(
        1 for t in history
        if t.get("timestamp_sgt", "").startswith(today_str)
        and t.get("status") == "FILLED"
        and t.get("instrument") == instrument
        and (t.get("window") or t.get("session") or t.get("macro_session")) in valid
    )


def session_losses(history: list, today_str: str,
                   macro: str, instrument: str) -> int:
    aliases = {
        "London": {"London", "London Window"},
        "US":      {"US", "US Window", "US session"},
        "US_Cont": {"US_Cont", "US Cont."},
        "Tokyo":  {"Tokyo", "Tokyo Window"},
    }
    valid = aliases.get(macro, {macro})
    losses = 0
    for t in history:
        if not t.get("timestamp_sgt", "").startswith(today_str): continue
        if t.get("status") != "FILLED":                          continue
        if t.get("instrument") != instrument:                    continue
        tm = t.get("macro_session") or t.get("window") or t.get("session") or ""
        if tm not in valid:                                      continue
        pnl = t.get("realized_pnl_usd")
        if isinstance(pnl, (int, float)) and pnl < 0:
            losses += 1
    return losses


# ── Risk / daily cap helpers ───────────────────────────────────────────────────

def daily_totals(history: list, today_str: str,
                 trader=None, instrument: str = ""):
    """Count P&L / trades / losses for one instrument today."""
    pnl, count, losses = 0.0, 0, 0
    for t in history:
        if not t.get("timestamp_sgt", "").startswith(today_str): continue
        if t.get("status") != "FILLED":                          continue
        if instrument and t.get("instrument") != instrument:     continue
        count += 1
        p = t.get("realized_pnl_usd")
        if isinstance(p, (int, float)):
            pnl += p
            if p < 0: losses += 1
    if trader is not None and instrument:
        try:
            pos = trader.get_position(instrument)
            if pos:
                unrealized = trader.check_pnl(pos)
                pnl += unrealized
                if unrealized < 0: losses += 1
        except Exception as e:
            log.warning("Could not fetch unrealized P&L for %s: %s", instrument, e)
    return pnl, count, losses


def get_closed_trade_records_today(history: list, today_str: str,
                                    instrument: str = "") -> list:
    closed = [
        t for t in history
        if t.get("timestamp_sgt", "").startswith(today_str)
        and t.get("status") == "FILLED"
        and (not instrument or t.get("instrument") == instrument)
        and isinstance(t.get("realized_pnl_usd"), (int, float))
    ]
    closed.sort(key=lambda t: t.get("closed_at_sgt") or t.get("timestamp_sgt") or "")
    return closed


def consecutive_loss_streak_today(history: list, today_str: str,
                                   instrument: str = "") -> int:
    streak = 0
    for t in reversed(get_closed_trade_records_today(history, today_str, instrument)):
        pnl = t.get("realized_pnl_usd")
        if not isinstance(pnl, (int, float)): continue
        if pnl < 0: streak += 1
        else:       break
    return streak


_parse_sgt_timestamp = parse_sgt_timestamp


def maybe_start_loss_cooldown(history: list, today_str: str,
                               now_sgt: datetime, settings: dict,
                               instrument: str = ""):
    cooldown_min = int(settings.get("loss_streak_cooldown_min", 30))
    if cooldown_min <= 0:
        return None, None, 0
    streak = consecutive_loss_streak_today(history, today_str, instrument)
    if streak < 2:
        return None, None, streak
    closed = get_closed_trade_records_today(history, today_str, instrument)
    if len(closed) < 2:
        return None, None, streak
    trigger_trade  = closed[-1]
    trigger_marker = (
        trigger_trade.get("trade_id") or
        trigger_trade.get("closed_at_sgt") or
        trigger_trade.get("timestamp_sgt")
    )
    # Use per-pair runtime file so pairs don't share cooldown state
    rt_file = _pair_runtime_file(instrument) if instrument else RUNTIME_STATE_FILE
    rt      = load_json(rt_file, {})
    if rt.get("loss_cooldown_trigger") == trigger_marker:
        return _parse_sgt_timestamp(rt.get("cooldown_until_sgt")), trigger_marker, streak
    cooldown_until = now_sgt + timedelta(minutes=cooldown_min)
    save_json(rt_file, {
        **rt,
        "loss_cooldown_trigger": trigger_marker,
        "cooldown_until_sgt":    cooldown_until.strftime("%Y-%m-%d %H:%M:%S"),
        "cooldown_reason":       f"{streak} consecutive losses",
        "updated_at_sgt":        now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return cooldown_until, trigger_marker, streak


def active_cooldown_until(now_sgt: datetime, instrument: str = ""):
    rt_file = _pair_runtime_file(instrument) if instrument else RUNTIME_STATE_FILE
    rt = load_json(rt_file, {})
    cu = _parse_sgt_timestamp(rt.get("cooldown_until_sgt"))
    return cu if cu and now_sgt < cu else None


# ── Position sizing ───────────────────────────────────────────────────────────

def compute_sl_price_dist(levels: dict, settings: dict) -> float:
    """SL price-distance for order placement. pair_sl_tp always provides sl_price_dist."""
    dist = levels.get("sl_price_dist")
    if dist is not None:
        try:
            v = float(dist)
            if v > 0:
                log.debug("Signal SL (price_dist): %.6f", v)
                return v
        except (TypeError, ValueError):
            pass
    rec = levels.get("sl_price_dist_rec")
    if rec is not None:
        try:
            v = float(rec)
            if v > 0:
                log.debug("Signal SL (price_dist_rec): %.6f", v)
                return v
        except (TypeError, ValueError):
            pass
    pip = float(levels.get("pip_size", 0.0001))
    log.warning("compute_sl_price_dist: no valid SL in levels — using 18p emergency fallback")
    return round(18 * pip, 7)


def compute_tp_price_dist(levels: dict, sl_price_dist: float, settings: dict) -> float:
    """TP price-distance for order placement. pair_sl_tp always provides tp_price_dist."""
    dist = levels.get("tp_price_dist")
    if dist is not None:
        try:
            v = float(dist)
            if v > 0: return v
        except (TypeError, ValueError):
            pass
    rec = levels.get("tp_price_dist_rec")
    if rec is not None:
        try:
            v = float(rec)
            if v > 0: return v
        except (TypeError, ValueError):
            pass
    return round(sl_price_dist * 1.67, 8)  # 1.67x RR emergency fallback


def derive_rr_ratio(levels: dict, sl_price_dist: float, tp_price_dist: float, settings: dict) -> float:
    try:
        rr = float(levels.get("rr_ratio"))
        if rr > 0: return rr
    except (TypeError, ValueError):
        pass
    if sl_price_dist > 0 and tp_price_dist > 0:
        return round(tp_price_dist / sl_price_dist, 2)
    return float(settings.get("rr_ratio", 2.5))


def calculate_units_from_position(position_usd: int, sl_price_dist: float) -> float:
    """units = position_usd / sl_price_distance. Exact USD risk for GBP/USD."""
    if sl_price_dist <= 0 or position_usd <= 0:
        return 0.0
    return round(position_usd / sl_price_dist, 2)


def apply_margin_guard(trader, instrument: str, requested_units: float,
                       entry_price: float, free_margin: float,
                       settings: dict) -> tuple[float, dict]:
    """Floor requested units against available margin."""
    margin_safety       = float(settings.get("margin_safety_factor",       0.6))
    margin_retry_safety = float(settings.get("margin_retry_safety_factor", 0.4))
    specs = trader.get_instrument_specs(instrument)

    # Use per-pair margin_rate_override (0.0 = use broker's own rate)
    override    = float(settings.get("margin_rate_override", 0.0) or 0.0)
    broker_rate = float(specs.get("marginRate", 0.02) or 0.02)
    margin_rate = max(broker_rate, override)

    norm_req = trader.normalize_units(instrument, requested_units)
    req_marg = trader.estimate_required_margin(instrument, norm_req, entry_price)

    if free_margin <= 0 or entry_price <= 0 or margin_rate <= 0:
        return 0.0, {"status": "SKIP", "reason": "invalid_margin_context",
                     "free_margin": float(free_margin or 0),
                     "required_margin": req_marg,
                     "requested_units": norm_req, "final_units": 0.0}

    max_units   = (free_margin * margin_safety) / (entry_price * margin_rate)
    norm_capped = trader.normalize_units(instrument, min(norm_req, max_units))
    fin_marg    = trader.estimate_required_margin(instrument, norm_capped, entry_price)
    status = "NORMAL" if abs(norm_capped - norm_req) < 1e-9 else "ADJUSTED"

    if norm_capped <= 0:
        retry = trader.normalize_units(
            instrument,
            (free_margin * margin_retry_safety) / (entry_price * margin_rate))
        ret_m = trader.estimate_required_margin(instrument, retry, entry_price)
        if retry > 0:
            return retry, {"status": "ADJUSTED", "reason": "margin_retry_floor",
                           "free_margin": float(free_margin), "required_margin": ret_m,
                           "requested_units": norm_req, "final_units": retry}
        return 0.0, {"status": "SKIP", "reason": "insufficient_margin",
                     "free_margin": float(free_margin), "required_margin": req_marg,
                     "requested_units": norm_req, "final_units": 0.0}

    return norm_capped, {"status": status, "reason": "margin_guard" if status == "ADJUSTED" else "ok",
                         "free_margin": float(free_margin), "required_margin": fin_marg,
                         "requested_units": norm_req, "final_units": norm_capped}


def compute_sl_tp_pips(sl_price_dist: float, tp_price_dist: float, pip_size: float = 0.0001):
    """Convert price-distance SL/TP to pips for the OANDA place_order API."""
    return round(sl_price_dist / pip_size), round(tp_price_dist / pip_size)


def compute_sl_tp_prices(entry: float, direction: str,
                          sl_price_dist: float, tp_price_dist: float,
                          dp: int = 5):
    """Return (sl_price, tp_price) rounded to the correct decimal places."""
    if direction == "BUY":
        return round(entry - sl_price_dist, dp), round(entry + tp_price_dist, dp)
    return round(entry + sl_price_dist, dp), round(entry - tp_price_dist, dp)


def get_effective_balance(balance: float | None, settings: dict) -> float:
    override = settings.get("account_balance_override")
    if override is not None:
        try:
            v = float(override)
            if v > 0: return v
        except (TypeError, ValueError):
            pass
    return float(balance or 0)


# ── Per-pair cache helpers ────────────────────────────────────────────────────

def load_signal_cache(instrument: str) -> dict:
    f = _pair_state_file(SCORE_CACHE_FILE, instrument)
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        return {}


def save_signal_cache(cache: dict, instrument: str):
    save_json(_pair_state_file(SCORE_CACHE_FILE, instrument), cache)


def load_ops_state(instrument: str) -> dict:
    f = _pair_state_file(OPS_STATE_FILE, instrument)
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:
        return {}


def save_ops_state(state: dict, instrument: str):
    save_json(_pair_state_file(OPS_STATE_FILE, instrument), state)


def send_once_per_state(alert, cache: dict, key: str, value: str,
                        message: str, instrument: str):
    if cache.get(key) != value:
        alert.send(message)
        cache[key] = value
        save_ops_state(cache, instrument)


# ── Break-even management ──────────────────────────────────────────────────────

def check_breakeven(history: list, trader, alert, settings: dict, instrument: str):
    """Move SL to break-even when trade profit reaches be_trigger_pips.

    v2.1: trigger derived from per-pair be_trigger_pips (a pip count) converted
    to a price distance using pip_size.  Replaces the old breakeven_trigger_usd
    which was a quote-currency price offset — correct for USD pairs but almost
    zero for JPY pairs (0.002 yen ≈ 0.2 pips), making breakeven fire at entry.
    """
    demo   = settings.get("demo_mode", True)
    _ps    = _pip_size(settings)                          # pip size for this pair
    _dp    = _pip_dp(_ps)

    # Resolve be_trigger_pips: pair-specific → global → hard default 20p
    _pair_sl_tp  = settings.get("pair_sl_tp", {})
    _pair_cfg    = _pair_sl_tp.get(instrument, {})
    _be_pips     = int(_pair_cfg.get("be_trigger_pips",
                       settings.get("be_trigger_pips", 20)))
    trigger_dist = _be_pips * _ps                         # price distance in quote ccy

    changed = False

    for trade in history:
        if trade.get("status")      != "FILLED":     continue
        if trade.get("instrument")  != instrument:   continue
        if trade.get("breakeven_moved"):              continue
        trade_id  = trade.get("trade_id")
        entry     = trade.get("entry")
        direction = trade.get("direction", "")
        if not trade_id or not entry or direction not in ("BUY", "SELL"):
            continue

        open_trade = trader.get_open_trade(str(trade_id))
        if open_trade is None: continue

        mid, bid, ask = trader.get_price(instrument)
        if mid is None: continue

        current_price = bid if direction == "BUY" else ask
        trigger_price = (entry + trigger_dist if direction == "BUY"
                         else entry - trigger_dist)
        triggered = (
            (direction == "BUY"  and current_price >= trigger_price) or
            (direction == "SELL" and current_price <= trigger_price)
        )
        if not triggered: continue

        result = trader.modify_sl(str(trade_id), float(entry))
        if result.get("success"):
            trade["breakeven_moved"] = True
            changed = True
            try:
                unrealized_pnl = float(open_trade.get("unrealizedPL", 0))
            except Exception:
                unrealized_pnl = 0
            log.info("Break-even moved | %s trade=%s entry=%.5f trigger=%.5f (+%dp)",
                     instrument, trade_id, entry, trigger_price, _be_pips)
            alert.send(msg_breakeven(
                trade_id=trade_id, direction=direction, entry=entry,
                trigger_price=trigger_price, trigger_dist=trigger_dist,
                current_price=current_price, unrealized_pnl=unrealized_pnl,
                demo=demo, price_dp=_dp,
            ))
        else:
            log.warning("Break-even failed for %s trade %s: %s",
                        instrument, trade_id, result.get("error"))

    if changed:
        save_history(history)


# ── Max pips tracker ────────────────────────────────────────────────────

def track_max_pips(history: list, trader, settings: dict, instrument: str) -> bool:
    """Track the maximum pip distance reached by each open trade.

    Called every cycle. For each open trade, fetches current price and
    computes pips moved in trade direction. Stores the peak in
    trade["max_pips_reached"] — survives until trade closes.

    Used post-trade to compare actual TP hit vs maximum reachable profit,
    informing TP level calibration decisions.
    """
    _pip_size_val = _pip_size(settings)
    _dp           = _pip_dp(_pip_size_val)
    changed       = False

    for trade in history:
        if trade.get("status")     != "FILLED":   continue
        if trade.get("instrument") != instrument: continue
        if trade.get("realized_pnl_usd") is not None: continue  # already closed

        entry     = trade.get("entry")
        direction = trade.get("direction", "")
        if not entry or direction not in ("BUY", "SELL"):
            continue

        try:
            mid, bid, ask = trader.get_price(instrument)
            if mid is None:
                continue
            current_price = bid if direction == "BUY" else ask
            if direction == "BUY":
                pips_now = round((current_price - entry) / _pip_size_val, 1)
            else:
                pips_now = round((entry - current_price) / _pip_size_val, 1)

            prev_max = trade.get("max_pips_reached", -999)
            if pips_now > prev_max:
                trade["max_pips_reached"] = round(pips_now, 1)
                changed = True
        except Exception as exc:
            log.debug("track_max_pips error for %s: %s", instrument, exc)

    return changed




# ── PnL backfill ───────────────────────────────────────────────────────────────

def backfill_pnl(history: list, trader, alert, settings: dict,
                 instrument: str) -> list:
    changed = False
    demo    = settings.get("demo_mode", True)
    for trade in history:
        if trade.get("instrument") != instrument: continue
        if trade.get("status") != "FILLED":       continue
        if trade.get("realized_pnl_usd") is not None: continue
        trade_id = trade.get("trade_id")
        if not trade_id: continue
        pnl = trader.get_trade_pnl(str(trade_id))
        if pnl is None: continue

        trade["realized_pnl_usd"] = pnl
        trade["closed_at_sgt"]    = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S")
        changed = True
        log.info("Back-filled P&L %s trade %s: $%.4f", instrument, trade_id, pnl)
        if pnl < 0:
            rt_file = _pair_runtime_file(instrument)
            rt = load_json(rt_file, {})
            rt["last_sl_closed_at_sgt"] = trade["closed_at_sgt"]
            save_json(rt_file, rt)

        if not trade.get("closed_alert_sent"):
            try:
                _cp  = trade.get("tp_price") if pnl > 0 else trade.get("sl_price")
                _dur = ""
                _t1s = trade.get("timestamp_sgt", "")
                _t2s = trade.get("closed_at_sgt", "")
                if _t1s and _t2s:
                    _d = int(
                        (datetime.strptime(_t2s, "%Y-%m-%d %H:%M:%S") -
                         datetime.strptime(_t1s, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60
                    )
                    _dur = f"{_d // 60}h {_d % 60}m" if _d >= 60 else f"{_d}m"
                alert.send(msg_trade_closed(
                    trade_id=trade_id,
                    direction=trade.get("direction", ""),
                    setup=trade.get("setup", ""),
                    entry=float(trade.get("entry", 0)),
                    close_price=float(_cp or 0),
                    pnl=float(pnl),
                    session=trade.get("session", ""),
                    demo=demo,
                    duration_str=_dur,
                    price_dp=_pip_dp(float(trade.get("pip_size", 0.0001) or 0.0001)),
                    max_pips_reached=trade.get("max_pips_reached"),
                ))
                trade["closed_alert_sent"] = True
            except Exception as _e:
                log.warning("Could not send trade_closed alert: %s", _e)

    if changed:
        save_history(history)
    return history


# ── Logging helper ─────────────────────────────────────────────────────────────

def log_event(code: str, message: str, level: str = "info", **extra):
    getattr(log, level, log.info)(f"[{code}] {message}", extra={"event": code, **extra})


def _next_day_reset_sgt(now_sgt: datetime, day_start_hour: int = 8) -> str:
    if now_sgt.hour < day_start_hour:
        reset = now_sgt.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    else:
        reset = (now_sgt + timedelta(days=1)).replace(
            hour=day_start_hour, minute=0, second=0, microsecond=0)
    return reset.strftime("%Y-%m-%d %H:%M SGT")


# ── Guard phase ────────────────────────────────────────────────────────────────

def _guard_phase(db, run_id, settings, alert, history, now_sgt, today, demo,
                 instrument: str) -> dict | None:
    """All pre-trade guards for one instrument. Returns context dict or None."""

    ops = load_ops_state(instrument)

    for w in run_startup_checks():
        log.warning(w, extra={"run_id": run_id})

    log.info("=== %s | %s | %s SGT ===",
             settings.get("bot_name", "Cable Scalp v2.0"), instrument,
             now_sgt.strftime("%Y-%m-%d %H:%M"),
             extra={"run_id": run_id, "pair": instrument})
    update_runtime_state(
        last_cycle_started=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        last_run_id=run_id, status="RUNNING",
    )

    if not settings.get("enabled", True) or get_bool_env("TRADING_DISABLED", False):
        log.warning("[%s] Trading disabled.", instrument, extra={"run_id": run_id})
        send_once_per_state(alert, ops, "ops_state", "disabled",
                            "⏸️ Trading disabled by configuration.", instrument)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_DISABLED")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "enabled_check", "reason": "disabled"})
        return None

    history[:] = prune_old_trades(history, settings)
    save_history(history)

    weekday = now_sgt.weekday()
    if weekday == 5:
        log.info("Saturday — market closed.", extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_MARKET_CLOSED")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "market_guard", "reason": "Saturday"})
        return None
    if weekday == 6:
        log.info("Sunday — waiting for Monday open.", extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_MARKET_CLOSED")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "market_guard", "reason": "Sunday"})
        return None
    if weekday == 0 and now_sgt.hour < int(settings.get("trading_day_start_hour_sgt", 8)):
        log.info("Monday pre-open — skipping.", extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_MARKET_CLOSED")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "market_guard", "reason": "Monday pre-open"})
        return None


    if settings.get("news_filter_enabled", True):
        try:
            refresh_calendar()
        except Exception as e:
            log.warning("Calendar refresh failed (using cached): %s", e,
                        extra={"run_id": run_id})

    # Loss cooldown check (per-instrument)
    cooldown_started_until, _, cooldown_streak = maybe_start_loss_cooldown(
        history, today, now_sgt, settings, instrument)
    if cooldown_started_until and now_sgt < cooldown_started_until:
        _cd_sess = get_session(now_sgt, settings)[0] or ""
        _, _, _cd_losses = daily_totals(history, today, instrument=instrument)
        send_once_per_state(
            alert, ops, "cooldown_started_state",
            f"cooldown_started:{cooldown_started_until.strftime('%Y-%m-%d %H:%M:%S')}",
            msg_cooldown_started(
                streak=cooldown_streak,
                cooldown_until_sgt=cooldown_started_until.strftime("%H:%M"),
                session_name=_cd_sess,
                day_losses=_cd_losses,
                day_limit=int(settings.get("max_losing_trades_day", 4)),
            ), instrument)
        log_event("COOLDOWN_STARTED",
                  f"[{instrument.replace('_', '/')}] Cooldown until "
                  f"{cooldown_started_until.strftime('%Y-%m-%d %H:%M:%S')} SGT.",
                  run_id=run_id)

    session, macro, threshold = get_session(now_sgt, settings)

    if is_friday_cutoff(now_sgt, settings):
        log_event("FRIDAY_CUTOFF", f"[{instrument.replace('_', '/')}] Friday cutoff.", run_id=run_id)
        send_once_per_state(
            alert, ops, "ops_state",
            f"friday_cutoff:{now_sgt.strftime('%Y-%m-%d')}",
            msg_friday_cutoff(int(settings.get("friday_cutoff_hour_sgt", 23))),
            instrument)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_FRIDAY_CUTOFF")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "friday_cutoff"})
        return None

    if settings.get("session_only", True):
        if session is None:
            if is_dead_zone_time(now_sgt, settings):
                log_event("DEAD_ZONE_SKIP",
                          f"[{instrument.replace('_', '/')}] Dead zone.",
                          run_id=run_id)
            else:
                log.info("[%s] Outside all sessions.", instrument,
                         extra={"run_id": run_id})
            if ops.get("last_session") is not None:
                send_once_per_state(alert, ops, "ops_state", "outside_session",
                                    f"⏸️ [{instrument.replace(chr(95),chr(47))}] Outside session.", instrument)
                ops["last_session"] = None
                save_ops_state(ops, instrument)
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                                 status="SKIPPED_OUTSIDE_SESSION")
            db.finish_cycle(run_id, status="SKIPPED",
                            summary={"stage": "session_check",
                                     "reason": "outside_session"})
            return None
    else:
        if session is None:
            session, macro = "All Hours", "London"
        threshold = int(settings.get("signal_threshold", 4))

    threshold = threshold or int(settings.get("signal_threshold", 4))
    banner    = SESSION_BANNERS.get(macro, "📊") + f" [{instrument.replace('_', '/')}]"
    log.info("[%s] Session: %s (%s)", instrument, session, macro,
             extra={"run_id": run_id})

    if ops.get("last_session") != session:
        if session is not None:
            _lon_s = int(settings.get("london_session_start_hour", 16))
            _lon_e = int(settings.get("london_session_end_hour",   20))
            _us_s  = int(settings.get("us_session_start_hour",     21))
            _us_e  = int(settings.get("us_session_end_hour",       23))
            _us_e2 = int(settings.get("us_session_early_end_hour",  3))
            _tok_s = int(settings.get("tokyo_session_start_hour",   8))
            _tok_e = int(settings.get("tokyo_session_end_hour",    15))
            _hours_map = {
                "US session": f"{_us_s:02d}:00–{_us_e:02d}:59",
                "US Cont.":   f"00:00–{_us_e2:02d}:59",
                "London Window": f"{_lon_s:02d}:00–{_lon_e:02d}:59",
                "Tokyo Window":  f"{_tok_s:02d}:00–{_tok_e:02d}:59",
            }
            _sess_hours = _hours_map.get(session, "")
            if _sess_hours:
                _dp, _dc, _ = daily_totals(history, today, instrument=instrument)
                _wk   = get_window_key(session)
                _wcap = get_window_trade_cap(_wk, settings) or 0
                send_once_per_state(
                    alert, ops,
                    "session_open_state", f"session_open:{session}:{today}",
                    msg_session_open(
                        session_name=f"{instrument.replace('_', '/')} {_clean_session(session)}",
                        session_hours_sgt=_sess_hours,
                        trade_cap=_wcap,
                        trades_today=_dc,
                        daily_pnl=_dp,
                    ), instrument)
        ops["last_session"] = session
        ops.pop("ops_state", None)
        save_ops_state(ops, instrument)

    # ── News filter ────────────────────────────────────────────────────────────
    news_penalty = 0
    news_status  = {}
    if settings.get("news_filter_enabled", True):
        nf = NewsFilter(
            before_minutes  =int(settings.get("news_block_before_min",    30)),
            after_minutes   =int(settings.get("news_block_after_min",     30)),
            lookahead_minutes=int(settings.get("news_lookahead_min",      120)),
            medium_penalty  =int(settings.get("news_medium_penalty_score", -1)),
            relevant_currencies=settings.get("news_relevant_currencies", ["GBP", "USD"]),
            fail_closed=bool(settings.get("news_fail_closed", True)),
            max_cache_age_hours=int(settings.get("calendar_cache_max_age_hours", 24)),
        )
        news_status  = nf.get_status_now()
        blocked      = bool(news_status.get("blocked"))
        reason       = str(news_status.get("reason", "No blocking news"))
        news_penalty = int(news_status.get("penalty", 0))
        lookahead    = news_status.get("lookahead", [])
        if lookahead:
            log.info("[%s] Upcoming: %s", instrument,
                     " | ".join(f"{e['name']} in {e['mins_away']}min"
                                for e in lookahead[:3]),
                     extra={"run_id": run_id})
        if blocked:
            _evt = news_status.get("event", {})
            send_once_per_state(alert, ops, "ops_state", f"news:{reason}",
                                msg_news_block(
                                    event_name=_evt.get("name", reason),
                                    event_time_sgt=_evt.get("time_sgt", ""),
                                    before_min=int(settings.get("news_block_before_min", 30)),
                                    after_min =int(settings.get("news_block_after_min",  30)),
                                ), instrument)
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                                 status="SKIPPED_NEWS_BLOCK", reason=reason)
            db.finish_cycle(run_id, status="SKIPPED",
                            summary={"stage": "news_filter", "reason": reason})
            return None

    # ── Early cap guards (no OANDA call needed) ────────────────────────────
    _dp_pre, _dt_pre, _dl_pre = daily_totals(history, today, instrument=instrument)

    _max_day_losses = int(settings.get("max_losing_trades_day", 4))
    if _max_day_losses > 0 and _dl_pre >= _max_day_losses:
        msg = (f"🛑 [{instrument.replace(chr(95),chr(47))}] Daily loss max ({_dl_pre}/{_max_day_losses}) — "
               f"no new entries today.")
        send_once_per_state(alert, ops, "ops_state",
                            f"day_loss_cap:{today}:{_dl_pre}", msg, instrument)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_DAILY_LOSS_CAP")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "daily_loss_cap", "instrument": instrument,
                                 "losses": _dl_pre, "cap": _max_day_losses})
        return None

    _max_day_trades = int(settings.get("max_trades_day", 12))
    if _max_day_trades > 0 and _dt_pre >= _max_day_trades:
        msg = (f"🛑 [{instrument.replace(chr(95),chr(47))}] Daily trade max ({_dt_pre}/{_max_day_trades}) — "
               f"no new entries today.")
        send_once_per_state(alert, ops, "ops_state",
                            f"day_trade_cap:{today}:{_dt_pre}", msg, instrument)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_DAILY_TRADE_CAP")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "daily_trade_cap", "instrument": instrument,
                                 "trades": _dt_pre, "cap": _max_day_trades})
        return None

    _window_key = get_window_key(session)
    _window_cap = get_window_trade_cap(_window_key, settings)
    if _window_key and _window_cap is not None:
        _wt = window_trade_count(history, today, _window_key, instrument)
        if _wt >= _window_cap:
            msg = (f"⏸️ [{instrument.replace(chr(95),chr(47))}] {session} max ({_wt}/{_window_cap}) — "
                   f"no more entries this window.")
            send_once_per_state(alert, ops, "window_cap_state",
                                f"window_cap:{_window_key}:{today}:{_wt}",
                                msg, instrument)
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                                 status="SKIPPED_WINDOW_CAP")
            db.finish_cycle(run_id, status="SKIPPED",
                            summary={"stage": "window_cap", "instrument": instrument,
                                     "window": _window_key,
                                     "trades": _wt, "cap": _window_cap})
            return None

    if macro:
        _sl_cap  = int(settings.get("max_losing_trades_session", 2))
        _sl_sess = session_losses(history, today, macro, instrument)
        if _sl_cap > 0 and _sl_sess >= _sl_cap:
            msg = (f"🛑 [{instrument.replace(chr(95),chr(47))}] {session or macro} session loss max "
                   f"({_sl_sess}/{_sl_cap}) — no more entries.")
            send_once_per_state(alert, ops, "session_loss_cap_state",
                                f"sess_loss_cap:{macro}:{today}:{_sl_sess}",
                                msg, instrument)
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                                 status="SKIPPED_SESSION_LOSS_CAP")
            db.finish_cycle(run_id, status="SKIPPED",
                            summary={"stage": "session_loss_cap",
                                     "instrument": instrument,
                                     "session": macro,
                                     "losses": _sl_sess, "cap": _sl_cap})
            return None

    # ── OANDA login ───────────────────────────────────────────────────────────
    trader          = OandaTrader(demo=demo)
    account_summary = trader.login_with_summary()
    _cb             = load_json(RUNTIME_STATE_FILE, {})
    _cb_fail        = int(_cb.get("oanda_consecutive_failures", 0))

    if account_summary is None:
        _cb_fail += 1
        save_json(RUNTIME_STATE_FILE, {**_cb, "oanda_consecutive_failures": _cb_fail})
        if _cb_fail == 1 or _cb_fail % 12 == 0:
            alert.send(msg_error("OANDA login failed",
                                 f"Consecutive failures: {_cb_fail}."))
        log.warning("OANDA login failed (consecutive=%d)", _cb_fail)
        db.finish_cycle(run_id, status="FAILED",
                        summary={"stage": "oanda_login", "reason": "login_failed",
                                 "consecutive_failures": _cb_fail})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="FAILED_LOGIN")
        return None

    if _cb_fail > 0:
        save_json(RUNTIME_STATE_FILE, {**_cb, "oanda_consecutive_failures": 0})
        if _cb_fail >= 3:
            alert.send(f"✅ OANDA restored after {_cb_fail} failure(s).")

    balance = account_summary["balance"]
    if balance <= 0:
        alert.send(msg_error("Cannot fetch balance", "OANDA returned $0"))
        db.finish_cycle(run_id, status="FAILED",
                        summary={"stage": "oanda_login", "reason": "invalid_balance"})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="FAILED_LOGIN")
        return None

    reconcile = reconcile_runtime_state(trader, history, instrument, now_sgt, alert=alert)
    if reconcile.get("recovered_trade_ids") or reconcile.get("backfilled_trade_ids"):
        save_history(history)
    db.upsert_state(f"last_reconciliation_{instrument}",
                    {**reconcile,
                     "checked_at_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S")})

    history[:] = backfill_pnl(history, trader, alert, settings, instrument)
    # AI virtual outcome tracking removed in v2.0.
    if settings.get("breakeven_enabled", False):
        check_breakeven(history, trader, alert, settings, instrument)

    # Track peak pip distance on every open trade
    if track_max_pips(history, trader, settings, instrument):
        save_history(history)

    # ── SL re-entry gap ───────────────────────────────────────────────────────
    _sl_gap_min = int(settings.get("sl_reentry_gap_min", 5))
    if _sl_gap_min > 0:
        _rt = load_json(_pair_runtime_file(instrument), {})
        _last_sl_at = _rt.get("last_sl_closed_at_sgt")
        if _last_sl_at:
            _last_sl_dt = _parse_sgt_timestamp(_last_sl_at)
            if _last_sl_dt and (now_sgt - _last_sl_dt).total_seconds() < _sl_gap_min * 60:
                _rem = max(1, int(
                    (_sl_gap_min * 60 - (now_sgt - _last_sl_dt).total_seconds()) // 60))
                send_once_per_state(
                    alert, ops, "sl_reentry_state", f"sl_gap:{_last_sl_at}",
                    f"⏳ [{instrument.replace(chr(95),chr(47))}] SL cooldown — {_rem} more min.",
                    instrument)
                update_runtime_state(
                    last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                    status="SKIPPED_SL_REENTRY_GAP")
                db.finish_cycle(run_id, status="SKIPPED",
                                summary={"stage": "sl_reentry_gap",
                                         "instrument": instrument})
                return None

    _, _, daily_losses = daily_totals(history, today, trader=trader,
                                      instrument=instrument)

    cooldown_until = active_cooldown_until(now_sgt, instrument)
    if cooldown_until:
        rem = max(1, int((cooldown_until - now_sgt).total_seconds() // 60))
        send_once_per_state(
            alert, ops, "cooldown_guard_state",
            f"cooldown:{cooldown_until.strftime('%Y-%m-%d %H:%M:%S')}",
            f"🧊 [{instrument.replace(chr(95),chr(47))}] Cooldown — {rem} more min.", instrument)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_COOLDOWN")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "cooldown_guard", "instrument": instrument})
        return None

    open_count     = trader.get_open_trades_count(instrument)
    max_concurrent = int(settings.get("max_concurrent_trades", 1))
    if open_count >= max_concurrent:
        send_once_per_state(
            alert, ops, "open_cap_state",
            f"open_cap:{open_count}:{max_concurrent}",
            f"⏸️ [{instrument.replace('_', '/')}] Max concurrent trades ({open_count}/{max_concurrent}).",
            instrument)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_OPEN_TRADE_CAP")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "open_trade_guard", "instrument": instrument})
        return None

    # ── Global concurrent-trade cap ───────────────────────────────────────────
    max_total = int(settings.get("max_total_open_trades", 0))
    if max_total > 0:
        total_open = len(trader.get_open_trades())   # all instruments, broker truth
        if total_open >= max_total:
            send_once_per_state(
                alert, ops, "global_cap_state",
                f"global_cap:{total_open}:{max_total}",
                f"⏸️ [{instrument.replace(chr(95),chr(47))}] Global trade max ({total_open}/{max_total} open across all pairs).",
                instrument)
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                                 status="SKIPPED_GLOBAL_TRADE_CAP")
            db.finish_cycle(run_id, status="SKIPPED",
                            summary={"stage": "global_trade_cap", "instrument": instrument,
                                     "total_open": total_open, "cap": max_total})
            return None

    return {
        "trader": trader,
        "balance": balance, "account_summary": account_summary,
        "session": session, "macro": macro, "threshold": threshold,
        "banner": banner, "ops": ops,
        "news_penalty": news_penalty, "news_status": news_status,
        "effective_balance": get_effective_balance(balance, settings),
    }


# ── Signal phase ───────────────────────────────────────────────────────────────

def _signal_phase(db, run_id, settings, alert, trader, history,
                  now_sgt, today, demo, ctx, instrument: str) -> dict | None:

    session         = ctx["session"]
    macro           = ctx["macro"]
    banner          = ctx["banner"]
    ops             = ctx["ops"]
    sig_cache       = load_signal_cache(instrument)
    news_penalty    = ctx["news_penalty"]
    news_status     = ctx["news_status"]
    balance         = ctx["balance"]
    account_summary = ctx["account_summary"]
    pip             = _pip_size(settings)
    dp              = _pip_dp(pip)

    engine = SignalEngine(demo=demo)
    score, direction, details, levels, position_usd = engine.analyze(
        instrument=instrument, settings=settings)

    raw_score        = score
    raw_position_usd = position_usd

    if news_penalty:
        score        = max(score + news_penalty, 0)
        position_usd = score_to_position_usd(score, settings)
        details      = details + f" | ⚠️ News penalty ({news_penalty:+d})"
        _nev = news_status.get("events", [])
        if not _nev and news_status.get("event"):
            _nev = [news_status["event"]]
        send_once_per_state(
            alert, ops, "ops_state", f"news_penalty:{news_penalty}:{today}",
            msg_news_penalty(
                event_names=[e.get("name", "") for e in _nev],
                penalty=news_penalty, score_after=score, score_before=raw_score,
                position_after=position_usd, position_before=raw_position_usd,
            ), instrument)

    db.record_signal(
        {"pair": instrument, "timeframe": "M5", "side": direction,
         "score": score, "raw_score": raw_score,
         "news_penalty": news_penalty, "details": details, "levels": levels},
        timeframe="M5", run_id=run_id,
    )

    cpr_w = levels.get("cpr_width_pct", 0)
    setup = levels.get("setup", "")

    # CPR width filter — skip on high-volatility days where CPR is too wide to be useful
    _cpr_max_width = float(settings.get("cpr_max_width_pct", 0.0))
    if _cpr_max_width > 0 and cpr_w > _cpr_max_width:
        log.info("[%s] CPR too wide (%.3f%% > %.3f%%) — skipping.", instrument, cpr_w, _cpr_max_width)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_CPR_TOO_WIDE")
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "cpr_width_filter", "cpr_width_pct": cpr_w,
                                 "instrument": instrument})
        return None

    # Resolve per-session threshold BEFORE the closure so all _send_signal_update
    # calls display the correct session threshold (not the global fallback).
    _thr = int(ctx.get("threshold", settings.get("signal_threshold", 4)))

    # Tokyo fresh-cross override: allow Fresh Cross Up/Down at ≥4 (trend setups still need ≥5)
    # Rationale: a fresh EMA cross in Tokyo already has ORB + CPR alignment baked in (4/6).
    # H1 STRICT still protects against counter-trend entries.
    if (macro == "Tokyo"
            and "Fresh Cross" in setup
            and _thr > 4
            and bool(settings.get("tokyo_fresh_cross_min_score", 4)) >= 0):
        _fresh_cross_thr = int(settings.get("tokyo_fresh_cross_min_score", 4))
        if _fresh_cross_thr < _thr:
            log.debug("[%s] Tokyo fresh-cross threshold override: %d → %d",
                      instrument, _thr, _fresh_cross_thr)
            _thr = _fresh_cross_thr

    def _send_signal_update(decision, reason, extra_payload=None):
        payload = _signal_payload(score=score, direction=direction,
                                  signal_threshold=_thr,
                                  min_rr_ratio=float(settings.get("min_rr_ratio", 1.6)),
                                  **(extra_payload or {}))
        msg = msg_signal_update(
            banner=banner, session=session, direction=direction,
            score=score, position_usd=position_usd, cpr_width_pct=cpr_w,
            detail_lines=details.split(" | "), news_penalty=news_penalty,
            raw_score=raw_score, decision=decision, reason=reason,
            cycle_minutes=int(settings.get("cycle_minutes", 5)),
            signal_threshold=_thr,
            setup=levels.get("setup", ""),
            orb_age_min=levels.get("orb_age_min"),
            orb_formed=levels.get("orb_formed", False),
            h1_trend=levels.get("h1_trend", "UNKNOWN"),
            h1_aligned=levels.get("h1_aligned", True),
            h1_filter_mode=settings.get("h1_filter_mode", "soft"),
            **payload,
        )
        if msg != sig_cache.get("last_signal_msg", ""):
            alert.send(msg)
            sig_cache.update({"score": score, "direction": direction,
                              "last_signal_msg": msg})
            save_signal_cache(sig_cache, instrument)

    _tg_min_score = int(settings.get("telegram_min_score_alert", 3))
    if direction == "NONE" or position_usd <= 0:
        _sig_action = "WATCHED" if score >= int(settings.get("signal_log_min_score", 3)) else "NOISE"
        log_signal(score, direction, macro, levels, _sig_action, settings=settings)
        if score >= _tg_min_score or _tg_min_score == 0:
            _send_signal_update("WATCHING", _clean_reason(details),
                                {"session_ok": True, "news_ok": True,
                                 "open_trade_ok": True})
        update_runtime_state(
            last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            status="COMPLETED_NO_SIGNAL", score=score, direction=direction)
        db.finish_cycle(run_id, status="COMPLETED",
                        summary={"signals": 1, "trades_placed": 0,
                                 "score": score, "direction": direction,
                                 "instrument": instrument})
        return None

    if score < _thr:
        if score >= _tg_min_score or _tg_min_score == 0:
            _send_signal_update(
                "WATCHING", f"Score {score}/6 below threshold ({_thr})",
                {"session_ok": True, "news_ok": True, "open_trade_ok": True})
        update_runtime_state(
            last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            status="COMPLETED_BELOW_THRESHOLD", score=score, direction=direction)
        db.finish_cycle(run_id, status="COMPLETED",
                        summary={"signals": 1, "trades_placed": 0,
                                 "score": score, "direction": direction,
                                 "reason": "below_threshold",
                                 "instrument": instrument})
        return None

    # ── Position sizing ───────────────────────────────────────────────────────
    entry = levels.get("entry", 0)
    if entry <= 0:
        _, _, ask = trader.get_price(instrument)
        entry = ask or 0

    sl_price_dist = compute_sl_price_dist(levels, settings)
    tp_price_dist = compute_tp_price_dist(levels, sl_price_dist, settings)
    rr_ratio = derive_rr_ratio(levels, sl_price_dist, tp_price_dist, settings)
    units    = calculate_units_from_position(position_usd, sl_price_dist)
    tp_pct   = (tp_price_dist / entry * 100) if entry > 0 else None

    if units <= 0:
        alert.send(msg_error(f"[{instrument.replace('_', '/')}] Position size = 0",
                             f"position_usd=${position_usd} sl={sl_price_dist:.6f}"))
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "position_sizing", "reason": "zero_units",
                                 "instrument": instrument})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_ZERO_UNITS")
        return None

    signal_blockers = list(levels.get("signal_blockers") or [])

    # ORB max age guard — block entry if ORB is too old to be meaningful
    _orb_max_age = int(settings.get("orb_max_age_minutes", 120))
    _orb_age     = int(levels.get("orb_age_min", 0))
    _orb_formed  = bool(levels.get("orb_formed", False))
    if _orb_formed and _orb_max_age > 0 and _orb_age > _orb_max_age:
        signal_blockers.append(
            f"ORB too old ({_orb_age}min > {_orb_max_age}min cap) — breakout no longer valid"
        )

    # H1 strict block — only when h1_filter_mode = "strict"
    _h1_mode    = settings.get("h1_filter_mode", "soft")
    _h1_enabled = bool(settings.get("h1_filter_enabled", True))
    _h1_trend   = levels.get("h1_trend", "UNKNOWN")
    _h1_aligned = levels.get("h1_aligned", True)
    if (_h1_enabled and _h1_mode == "strict" and
            not _h1_aligned and _h1_trend not in ("UNKNOWN", "DISABLED", "FLAT")):
        _h1_dir    = "bullish" if direction == "BUY" else "bearish"
        _h1_reason = f"H1 {_h1_trend} — no {direction} until H1 turns {_h1_dir}"
        _send_signal_update("BLOCKED", _h1_reason)
        update_runtime_state(
            last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            status="SKIPPED_H1_BLOCK", score=score, direction=direction)
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "h1_filter", "reason": _h1_reason,
                                 "instrument": instrument})
        log_signal(score, direction, macro, levels, "BLOCKED_H1",
                   block_reason=_h1_reason, settings=settings)
        return None

    if signal_blockers:
        log_signal(score, direction, macro, levels, "BLOCKED_RR",
                   block_reason=signal_blockers[0], settings=settings)
        _send_signal_update("BLOCKED", signal_blockers[0],
                            {"rr_ratio": rr_ratio, "tp_pct": tp_pct,
                             "session_ok": True, "news_ok": True,
                             "open_trade_ok": True, "margin_ok": None})
        update_runtime_state(
            last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
            status="SKIPPED_SIGNAL_BLOCKED", reason=signal_blockers[0])
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "signal_validation",
                                 "reason": signal_blockers[0],
                                 "instrument": instrument})
        return None

    # ── Margin guard ──────────────────────────────────────────────────────────
    margin_available = float(
        account_summary.get("margin_available", balance or 0) or 0)
    price_for_margin = entry or float(levels.get("current_price", entry) or 0)
    units, margin_info = apply_margin_guard(
        trader=trader, instrument=instrument,
        requested_units=units, entry_price=price_for_margin,
        free_margin=margin_available, settings=settings)
    if margin_info.get("status") == "ADJUSTED":
        log.warning("[%s] Margin adjusted %.2f → %.2f | free=%.2f required=%.2f",
                    instrument,
                    float(margin_info.get("requested_units", 0)),
                    float(margin_info.get("final_units",     0)),
                    float(margin_info.get("free_margin",     0)),
                    float(margin_info.get("required_margin", 0)))
        alert.send(msg_margin_adjustment(
            instrument=instrument,
            requested_units=float(margin_info.get("requested_units", 0)),
            adjusted_units =float(margin_info.get("final_units",     0)),
            free_margin    =float(margin_info.get("free_margin",     0)),
            required_margin=float(margin_info.get("required_margin", 0)),
            reason=str(margin_info.get("reason", "margin_guard")),
        ))
    if units <= 0:
        _send_signal_update(
            "BLOCKED", "Insufficient margin",
            {"rr_ratio": rr_ratio, "tp_pct": tp_pct,
             "session_ok": True, "news_ok": True,
             "open_trade_ok": True, "margin_ok": False})
        alert.send(msg_error(
            f"[{instrument.replace('_', '/')}] Insufficient margin",
            f"free=${margin_available:.2f} "
            f"required=${float(margin_info.get('required_margin', 0)):.2f}"))
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "margin_cap",
                                 "reason": "insufficient_margin",
                                 "instrument": instrument})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_MARGIN")
        return None

    # Reject margin-adjusted micro-positions that are too small to be meaningful
    _min_units = int(settings.get("min_trade_units", 1000))
    if units < _min_units:
        reason = f"Units {units:.0f} < min_trade_units {_min_units} after margin guard"
        log.warning("[%s] Trade blocked — %s", instrument, reason)
        _send_signal_update(
            "BLOCKED", f"Margin reduced units to {units:.0f} (min {_min_units})",
            {"rr_ratio": rr_ratio, "tp_pct": tp_pct,
             "session_ok": True, "news_ok": True,
             "open_trade_ok": True, "margin_ok": False})
        alert.send(msg_error(
            f"[{instrument.replace('_', '/')}] Units too small after margin guard",
            f"adjusted={units:.0f} min={_min_units} free=${margin_available:.2f}"))
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "margin_cap",
                                 "reason": "min_units_not_met",
                                 "instrument": instrument})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_MIN_UNITS")
        return None

    # stop_pips / tp_pips must use this pair's pip_size for the OANDA order
    stop_pips, tp_pips = compute_sl_tp_pips(sl_price_dist, tp_price_dist, pip)
    reward_usd = round(units * tp_price_dist, 6)

    # ── Spread guard ──────────────────────────────────────────────────────────
    mid, bid, ask = trader.get_price(instrument)
    if mid is None:
        alert.send(msg_error(f"[{instrument.replace('_', '/')}] Cannot fetch price", "OANDA returned None"))
        db.finish_cycle(run_id, status="FAILED",
                        summary={"stage": "pricing", "instrument": instrument})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="FAILED_PRICING")
        return None

    spread_pips  = round(abs(ask - bid) / pip)
    spread_limit = int(settings.get("spread_limits", {}).get(
        macro, settings.get("max_spread_pips", 5)))

    if spread_pips > spread_limit:
        _send_signal_update(
            "BLOCKED", f"Spread {spread_pips} > {spread_limit} pips",
            {"rr_ratio": rr_ratio, "tp_pct": tp_pct,
             "spread_pips": spread_pips, "spread_limit": spread_limit,
             "session_ok": True, "news_ok": True,
             "open_trade_ok": True, "margin_ok": True})
        send_once_per_state(
            alert, ops, "spread_state", f"spread:{macro}:{spread_pips}",
            msg_spread_skip(banner, session, spread_pips, spread_limit),
            instrument)
        log_signal(score, direction, macro, levels, "BLOCKED_SPREAD",
                   block_reason=f"Spread {spread_pips}p > {spread_limit}p",
                   settings=settings)
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "spread_guard", "instrument": instrument})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                             status="SKIPPED_SPREAD_GUARD")
        return None

    # AI News Guard removed in v2.0.
    # Rule-based NewsFilter (hard-block + score penalty) already ran in
    # _guard_phase above.  All checks passed — trade is READY.
    ai_guard_result = {
        "enabled":     False,
        "risk_level":  "LOW",
        "action":      "ALLOW",
        "reason":      "Rule-based mode — AI News Guard removed in v2.0.",
        "model":       None,
        "decision_id": None,
    }


    _send_signal_update(
        "READY", "All checks passed",
        {"rr_ratio": rr_ratio, "tp_pct": tp_pct,
         "spread_pips": spread_pips, "spread_limit": spread_limit,
         "session_ok": True, "news_ok": True,
         "open_trade_ok": True, "margin_ok": True})

    ctx.update({
        "score": score, "raw_score": raw_score, "direction": direction,
        "details": details, "levels": levels, "position_usd": position_usd,
        "entry": entry, "sl_price_dist": sl_price_dist, "tp_price_dist": tp_price_dist,
        "rr_ratio": rr_ratio, "units": units,
        "stop_pips": stop_pips, "tp_pips": tp_pips,
        "reward_usd": reward_usd, "cpr_w": cpr_w,
        "spread_pips": spread_pips, "bid": bid, "ask": ask,
        "ai_guard_result": ai_guard_result,
        "margin_available": margin_available, "price_for_margin": price_for_margin,
        "margin_info": margin_info, "pip": pip, "dp": dp,
    })
    return ctx


# ── Execution phase ────────────────────────────────────────────────────────────

def _execution_phase(db, run_id, settings, alert, trader, history,
                     now_sgt, today, demo, ctx, instrument: str):

    session          = ctx["session"]
    macro            = ctx["macro"]
    banner           = ctx["banner"]
    score            = ctx["score"]
    raw_score        = ctx["raw_score"]
    direction        = ctx["direction"]
    details          = ctx["details"]
    levels           = ctx["levels"]
    position_usd     = ctx["position_usd"]
    entry            = ctx["entry"]
    sl_price_dist    = ctx["sl_price_dist"]
    tp_price_dist    = ctx["tp_price_dist"]
    rr_ratio         = ctx["rr_ratio"]
    units            = ctx["units"]
    stop_pips        = ctx["stop_pips"]
    tp_pips          = ctx["tp_pips"]
    reward_usd       = ctx["reward_usd"]
    cpr_w            = ctx["cpr_w"]
    spread_pips      = ctx["spread_pips"]
    bid              = ctx["bid"]
    ask              = ctx["ask"]
    margin_available = ctx["margin_available"]
    price_for_margin = ctx["price_for_margin"]
    margin_info      = ctx["margin_info"]
    eff_balance      = ctx["effective_balance"]
    news_penalty     = ctx["news_penalty"]
    pip              = ctx["pip"]
    dp               = ctx["dp"]
    ai_guard_result  = ctx.get("ai_guard_result") or {}

    # ── Hard dead zone execution block (defense in depth) ──────────────────
    # Even if session logic somehow passes, no new order can be placed
    # during 04:00–07:59 SGT. This is a final hard stop before any OANDA call.
    if is_dead_zone_time(now_sgt, settings):
        log.warning("[%s] Hard dead zone block fired at execution phase (%s SGT) — "
                    "order suppressed. Investigate guard chain.",
                    instrument, now_sgt.strftime("%H:%M"), extra={"run_id": run_id})
        db.finish_cycle(run_id, status="SKIPPED",
                        summary={"stage": "dead_zone_hard_block", "instrument": instrument})
        return

    sl_price, tp_price = compute_sl_tp_prices(entry, direction, sl_price_dist, tp_price_dist, dp)

    record = {
        "timestamp_sgt":        now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        "mode":                 "DEMO" if demo else "LIVE",
        "instrument":           instrument,
        "direction":            direction,
        "setup":                levels.get("setup", ""),
        "session":              session,
        "window":               get_window_key(session),
        "macro_session":        macro,
        "score":                score,
        "raw_score":            raw_score,
        "news_penalty":         news_penalty,
        "position_usd":         position_usd,
        "entry":                round(entry, dp),
        "sl_price":             sl_price,
        "tp_price":             tp_price,
        "size":                 units,
        "cpr_width_pct":        cpr_w,
        "h1_trend":             levels.get("h1_trend", "UNKNOWN"),
        "h1_aligned":           levels.get("h1_aligned", True),
        "max_pips_reached":     None,
        "sl_price_dist":        round(sl_price_dist, dp + 2),
        "tp_price_dist":        round(tp_price_dist, dp + 2),
        "pip_size":             pip,
        "estimated_risk_usd":   round(position_usd, 2),
        "estimated_reward_usd": round(reward_usd, 6),
        "spread_pips":          spread_pips,
        "stop_pips":            stop_pips,
        "tp_pips":              tp_pips,
        "levels":               levels,
        "details":              details,
        "trade_id":             None,
        "status":               "FAILED",
        "realized_pnl_usd":     None,
    }

    result = trader.place_order(
        instrument=instrument, direction=direction,
        size=units, stop_distance=stop_pips, limit_distance=tp_pips,
        bid=bid, ask=ask,
    )

    if not result.get("success"):
        err             = result.get("error", "Unknown")
        retry_attempted = False
        if settings.get("auto_scale_on_margin_reject", True) and "MARGIN" in str(err).upper():
            retry_attempted  = True
            retry_safety     = float(settings.get("margin_retry_safety_factor", 0.4))
            retry_specs      = trader.get_instrument_specs(instrument)
            retry_override   = float(settings.get("margin_rate_override", 0.0) or 0.0)
            retry_broker     = float(retry_specs.get("marginRate", 0.02) or 0.02)
            retry_margin     = max(retry_broker, retry_override)
            retry_units      = trader.normalize_units(
                instrument,
                (margin_available * retry_safety) / max(
                    price_for_margin * retry_margin, 1e-9))
            if 0 < retry_units < units:
                alert.send(msg_margin_adjustment(
                    instrument=instrument,
                    requested_units=units, adjusted_units=retry_units,
                    free_margin=margin_available,
                    required_margin=trader.estimate_required_margin(
                        instrument, retry_units, price_for_margin),
                    reason="broker_margin_reject_retry",
                ))
                retry_result = trader.place_order(
                    instrument=instrument, direction=direction,
                    size=retry_units, stop_distance=stop_pips,
                    limit_distance=tp_pips, bid=bid, ask=ask,
                )
                if retry_result.get("success"):
                    result = retry_result
                    units  = retry_units
                    record["size"] = units
                    record["estimated_reward_usd"] = round(units * tp_price_dist, 6)

        if not result.get("success"):
            alert.send(msg_order_failed(
                direction, instrument, units, result.get("error", "Unknown"),
                free_margin=margin_available,
                required_margin=trader.estimate_required_margin(
                    instrument, units, price_for_margin),
                retry_attempted=retry_attempted,
            ))
            log.error("[%s] Order failed: %s", instrument,
                      result.get("error"), extra={"run_id": run_id})
            # AI tracking removed in v2.0.

    if result.get("success"):
        record["trade_id"] = result.get("trade_id")
        record["status"]   = "FILLED"
        # AI tracking removed in v2.0.
        fill_price = result.get("fill_price")
        if fill_price and fill_price > 0:
            ae                     = fill_price
            record["entry"]        = round(ae, dp)
            record["signal_entry"] = round(entry, dp)
            record["sl_price"]     = round(ae - sl_price_dist if direction == "BUY"
                                           else ae + sl_price_dist, dp)
            record["tp_price"]     = round(ae + tp_price_dist if direction == "BUY"
                                           else ae - tp_price_dist, dp)

        alert.send(msg_trade_opened(
            banner=banner, direction=direction, setup=levels.get("setup", ""),
            session=session, fill_price=record["entry"], signal_price=entry,
            sl_price=record["sl_price"], tp_price=record["tp_price"],
            sl_price_dist=sl_price_dist, tp_price_dist=tp_price_dist, units=units, position_usd=position_usd,
            rr_ratio=rr_ratio, cpr_width_pct=cpr_w, spread_pips=spread_pips,
            score=score, balance=eff_balance, demo=demo,
            news_penalty=news_penalty, raw_score=raw_score,
            free_margin=margin_info.get("free_margin"),
            required_margin=trader.estimate_required_margin(
                instrument, units, price_for_margin),
            margin_mode=(
                "RETRIED"
                if record["size"] != float(margin_info.get("final_units", record["size"]))
                else margin_info.get("status", "NORMAL")),
            margin_usage_pct=(
                (trader.estimate_required_margin(instrument, units, price_for_margin) /
                 float(margin_info.get("free_margin", 0)) * 100)
                if float(margin_info.get("free_margin", 0)) > 0 else None),
            price_dp=dp,
            tp2_rr=float(settings.get("tp2_rr_reference", 3.0)),
        ))
        log_signal(score, direction, macro, levels, "FIRED",
                   trade_id=str(record.get("trade_id", "")), settings=settings)
        log.info("[%s] Trade placed: %s", instrument, record,
                 extra={"run_id": run_id})

    history.append(record)
    save_history(history)
    db.record_trade_attempt(
        {"pair": instrument, "timeframe": "M5", "side": direction,
         "score": score, **record},
        ok=bool(result.get("success")),
        note=result.get("error", "trade placed"),
        broker_trade_id=record.get("trade_id"), run_id=run_id,
    )
    db.upsert_state(f"last_trade_attempt_{instrument}", {
        "run_id": run_id, "success": bool(result.get("success")),
        "trade_id": record.get("trade_id"),
        "timestamp_sgt": record["timestamp_sgt"],
        "instrument": instrument,
    })
    update_runtime_state(
        last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        status="COMPLETED", score=score, direction=direction,
        trade_status=record["status"],
    )
    db.finish_cycle(run_id, status="COMPLETED", summary={
        "signals": 1,
        "trades_placed": int(bool(result.get("success"))),
        "score": score, "direction": direction,
        "trade_status": record["status"],
        "instrument": instrument,
    })


# ── Main cycle ─────────────────────────────────────────────────────────────────

def run_bot_cycle(alert: "TelegramAlert | None" = None):
    """Orchestrator — runs guard → signal → execution for every enabled pair.

    alert — optional pre-constructed TelegramAlert injected by scheduler.
    """
    global _startup_reconcile_done

    settings      = validate_settings(load_settings())
    db            = Database()
    demo          = settings.get("demo_mode", True)
    alert         = alert or TelegramAlert()
    history       = load_history()
    now_sgt       = datetime.now(SGT)
    _day_start    = int(settings.get("trading_day_start_hour_sgt", 8))
    today         = get_trading_day(now_sgt, _day_start)
    enabled_pairs = get_enabled_pairs(settings)

    if not enabled_pairs:
        log.warning("No pairs enabled in settings['pairs'] — nothing to trade.")
        return

    # ── Startup OANDA reconcile (once per process) ────────────────────────────
    if not _startup_reconcile_done:
        _startup_reconcile_done = True
        try:
            _rt = OandaTrader(demo=demo)
            for instr, _ in enabled_pairs:
                recon = startup_oanda_reconcile(_rt, history, instr, today, now_sgt)
                if recon["injected"] or recon["backfilled"]:
                    save_history(history)
                    log.info("[%s] Startup reconcile: injected=%s backfilled=%s",
                             instr, recon["injected"], recon["backfilled"])
                    if recon["injected"]:
                        alert.send(
                            f"♻️ [{instr}] Reconcile injected "
                            f"{len(recon['injected'])} trade(s): "
                            f"{', '.join(recon['injected'])}"
                        )
        except Exception as _e:
            log.warning("Startup reconcile failed (non-fatal): %s", _e)

    # ── Per-pair cycle ─────────────────────────────────────────────────────────
    for instrument, pair_cfg in enabled_pairs:
        # Merge: pair_cfg overrides global settings for this pair's cycle
        eff = get_effective_settings(settings, pair_cfg)

        with db.cycle() as run_id:
            try:
                ctx = _guard_phase(
                    db, run_id, eff, alert, history,
                    now_sgt, today, demo, instrument)
                if ctx is None:
                    continue

                ctx = _signal_phase(
                    db, run_id, eff, alert, ctx["trader"],
                    history, now_sgt, today, demo, ctx, instrument)
                if ctx is None:
                    continue

                _execution_phase(
                    db, run_id, eff, alert, ctx["trader"],
                    history, now_sgt, today, demo, ctx, instrument)

            except Exception as exc:
                update_runtime_state(
                    last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
                    status="FAILED", error=str(exc))
                log.exception("[%s] Unhandled error in cycle: %s", instrument, exc)
                raise


def main():
    return run_bot_cycle()


if __name__ == "__main__":
    main()
