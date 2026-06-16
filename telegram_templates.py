"""Telegram message templates for Cable AI Scalp v1.2
AtomicFX-style: clean, state-change only, minimal noise.
"""
from __future__ import annotations

_DIV = "─" * 22


def _dir_icon(d: str) -> str:
    return "📈" if d == "BUY" else ("📉" if d == "SELL" else "")

def _session_icon(s: str) -> str:
    u = s.upper()
    if "LONDON" in u:                        return "🇬🇧"
    if "CONT" in u or "US_CONT" in u:        return "🌙"
    if "US" in u:                            return "🗽"
    if "TOKYO" in u:                         return "🗼"
    if "ASIAN" in u or "PRE" in u:           return "🌐"
    if "EUROPEAN" in u:                      return "☀️"
    if "DEAD" in u:                          return "✈️"
    return "📊"

def _pos_label(p: int) -> str:
    if p >= 30: return f"Full ${p}"
    if p >= 20: return f"Medium ${p}"
    if p >  0:  return f"Partial ${p}"
    return "No trade"

def _pnl_icon(v: float) -> str:
    return "🟢" if v > 0 else ("🔴" if v < 0 else "⬜")

def _mini_stats(s: dict) -> str:
    if s["count"] == 0: return "No closed trades"
    return f"{s['count']} trades  {s['wins']}W/{s['losses']}L  ${s['net_pnl']:+.2f}  WR {s['win_rate']:.0f}%"

def _split_banner(banner: str) -> tuple[str, str]:
    """Extract pair from banner.
    Handles both:
      '🇬🇧 LONDON [GBP/USD]'  → ('🇬🇧 LONDON [GBP/USD]', 'GBP/USD')
      'Cable AI Scalp v1.2 | GBP/USD' → ('Cable AI Scalp v1.2', 'GBP/USD')
    """
    if "[" in banner and "]" in banner:
        pair = banner[banner.index("[")+1 : banner.index("]")]
        return banner.strip(), pair.strip()
    if " | " in banner:
        bot, pair = banner.rsplit(" | ", 1)
        return bot.strip(), pair.strip("[]").strip()
    return banner.strip(), ""

def _clean_pair(s: str) -> str:
    return s.replace("_", "/")

def _clean_session(s: str) -> str:
    mapping = {
        "US":            "US session",
        "US Window":     "US session",
        "US session":    "US session",
        "US Cont.":      "US Cont.",
        "US_Cont":       "US Cont.",
        "London Window": "London Window",
        "Tokyo Window":  "Tokyo Window",
    }
    return mapping.get(s, s)

def _ps(dp: int) -> float:
    return 10 ** -(dp - 1)

def _ascii_bar(v: float, mx: float, w: int = 10) -> str:
    if mx <= 0: return "░" * w
    f = int(round(v / mx * w))
    return "█" * f + "░" * (w - f)


# ── 1. Signal update ──────────────────────────────────────────────────────────

def msg_signal_update(
    banner, session, direction, score, position_usd, cpr_width_pct,
    detail_lines, news_penalty=0, raw_score=None, decision="WATCHING",
    reason="", mandatory_checks=None, quality_checks=None,
    execution_checks=None, cycle_minutes=5, signal_threshold=4,
    setup="", orb_age_min=None, orb_formed=False,
    h1_trend="UNKNOWN", h1_aligned=True, h1_filter_mode="strict",
) -> str:
    bot, pair = _split_banner(banner)
    s_str = f"{score}/6"
    if raw_score is not None and news_penalty:
        s_str += f" (raw {raw_score}, news {news_penalty:+d})"
    di    = _dir_icon(direction)
    nline = f"⚠️  News penalty: {news_penalty:+d}\n" if news_penalty else ""

    # H1 trend line — shows on all cards when filter is enabled
    def _h1_line():
        if h1_trend in ("UNKNOWN", "DISABLED"): return ""
        icon   = "🟢" if h1_trend == "BULLISH" else ("🔴" if h1_trend == "BEARISH" else "⬜")
        align  = "aligned" if h1_aligned else "counter-trend ⚠️"
        mode   = f" [{h1_filter_mode}]" if h1_filter_mode in ("soft", "strict") else ""
        return f"H1: {icon} {h1_trend}  ({align}){mode}\n"

    if decision == "WATCHING":
        orb = ""
        if orb_formed and orb_age_min is not None:
            lbl = "fresh" if orb_age_min < 60 else ("aging" if orb_age_min < 120 else "stale")
            orb = f"ORB: {orb_age_min}min ({lbl})  |  "
        return (
            f"{banner}\n{_DIV}\n"
            f"{pair}  {di} {direction}  Score {s_str}  👁 Watching\n"
            f"Reason: {reason or 'Watching for setup'}\n"
            f"{_h1_line()}"
            f"{nline}"
            f"{_DIV}\n"
            f"{orb}CPR: {cpr_width_pct:.2f}% width\n"
            f"Next cycle in {cycle_minutes} min"
        )

    if decision == "BLOCKED":
        return (
            f"{banner}\n{_DIV}\n"
            f"{pair}  {di} {direction}  Score {s_str}  ❌ Blocked\n"
            f"Reason: {reason}\n"
            f"{_h1_line()}"
            f"{nline}"
            f"Next cycle in {cycle_minutes} min"
        )

    # READY
    spread = margin = ""
    if execution_checks:
        for lbl, ok, det in execution_checks:
            if "Spread" in lbl: spread = f"Spread: {det}  |  "
            elif "Margin" in lbl: margin = f"Margin: {det}\n"
    return (
        f"{banner}\n{_DIV}\n"
        f"{pair}  {di} {direction}  Score {s_str}  ✅ Ready\n"
        f"Window: {session}  |  CPR: {cpr_width_pct:.2f}% width\n"
        f"{_h1_line()}"
        f"{nline}"
        f"{_DIV}\n"
        f"{spread}{margin}"
        f"Next cycle in {cycle_minutes} min"
    )


# ── 2. Trade opened ───────────────────────────────────────────────────────────

def msg_trade_opened(
    banner, direction, setup, session, fill_price, signal_price,
    sl_price, tp_price, sl_price_dist, tp_price_dist, units, position_usd,
    rr_ratio, cpr_width_pct, spread_pips, score, balance, demo,
    news_penalty=0, raw_score=None, free_margin=None,
    required_margin=None, margin_mode="NORMAL", margin_usage_pct=None,
    price_dp=5, tp2_rr=3.0,
    h1_trend="UNKNOWN", h1_aligned=True,
) -> str:
    bot, pair = _split_banner(banner)
    pair    = _clean_pair(pair)
    session = _clean_session(session)
    mode = "DEMO" if demo else "LIVE"
    di   = _dir_icon(direction)
    si   = _session_icon(session)
    s_str = f"{score}/6"
    if raw_score is not None and news_penalty:
        s_str += f" (raw {raw_score})"

    pip = _ps(price_dp)
    sl_p  = round(sl_price_dist / pip)
    tp_p  = round(tp_price_dist / pip)
    tp2_p = round(sl_price_dist * tp2_rr / pip)
    tp2_price = round(
        fill_price + sl_price_dist * tp2_rr if direction == "BUY"
        else fill_price - sl_price_dist * tp2_rr, price_dp
    )
    units_fmt = f"{int(units):,}" if units >= 1000 else str(int(units))

    return (
        f"{banner}\n{_DIV}\n"
        f"{di} {direction} {pair} — {si} {session}\n"
        f"{_DIV}\n"
        f"◆ Entry:  {fill_price:.{price_dp}f}\n\n"
        f"✅ TP1:   {tp_price:.{price_dp}f}  (+{tp_p}p | {rr_ratio:.1f}xRR)  ← bot target\n"
        f"◻  TP2:   {tp2_price:.{price_dp}f}  (+{tp2_p}p | {tp2_rr:.1f}xRR)  ← reference\n"
        f"✗  SL:    {sl_price:.{price_dp}f}  (-{sl_p}p)\n"
        f"{_DIV}\n"
        f"Setup:   {setup}\n"
        f"Score:   {s_str}  |  Spread: {spread_pips}p\n"
        + (f"H1:      {'🟢' if h1_aligned else '🔴'} {h1_trend}  ({'aligned' if h1_aligned else 'counter-trend ⚠️'})\n"
           if h1_trend not in ('UNKNOWN', 'DISABLED') else "")
        + f"Units:   {units_fmt}  |  Risk: {_pos_label(position_usd)}  |  Mode: {mode}"
    )


# ── 3. Breakeven ──────────────────────────────────────────────────────────────

def msg_breakeven(trade_id, direction, entry, trigger_price, trigger_dist,
                  current_price, unrealized_pnl, demo, price_dp=5) -> str:
    mode = "DEMO" if demo else "LIVE"
    return (
        f"🔒 Break-Even Activated\n{_DIV}\n"
        f"{direction}  Trade #{trade_id}\n"
        f"Entry:   {entry:.{price_dp}f}  →  SL moved to entry\n"
        f"Trigger: {trigger_price:.{price_dp}f}  (now: {current_price:.{price_dp}f})\n"
        f"PnL now: ${unrealized_pnl:+.2f}  |  Mode: {mode}"
    )


# ── 4. Trade closed ───────────────────────────────────────────────────────────

def msg_trade_closed(trade_id, direction, setup, entry, close_price,
                     pnl, session, demo, duration_str="", price_dp=5,
                     max_pips_reached=None) -> str:
    mode = "DEMO" if demo else "LIVE"
    di   = _dir_icon(direction)
    pip  = _ps(price_dp)
    pips = abs(close_price - entry) / pip

    if pnl > 0:
        outcome, pip_str = "TP ✅", f"+{pips:.0f} pips"
    elif pnl < 0:
        outcome, pip_str = "SL ✗",  f"-{pips:.0f} pips"
    else:
        outcome, pip_str = "BE ➡️", "0 pips"

    dur      = f"  |  {duration_str}" if duration_str else ""
    max_line = (f"Peak:    +{max_pips_reached:.1f} pips reached\n"
                if max_pips_reached is not None and max_pips_reached > 0 else "")
    return (
        f"{di} {direction} {outcome}\n{_DIV}\n"
        f"Entry:   {entry:.{price_dp}f}  →  Close: {close_price:.{price_dp}f}\n"
        f"Move:    {pip_str}\n"
        f"PnL:     ${pnl:+.2f}{dur}\n"
        f"{max_line}"
        f"Session: {_clean_session(session)}  |  Mode: {mode}"
    )


# ── 5. News block ─────────────────────────────────────────────────────────────

def msg_news_block(event_name, event_time_sgt, before_min, after_min) -> str:
    return (
        f"📰 News Block\n{_DIV}\n"
        f"Event:  {event_name}\n"
        f"Time:   {event_time_sgt} SGT\n"
        f"Window: -{before_min}min → +{after_min}min\n"
        f"No new entries — resuming after event"
    )


# ── AI News Guard ─────────────────────────────────────────────────────────────

def msg_ai_guard_result(action, risk_level, reason, score=None, direction="", pair="GBP/USD", mode="TRADE") -> str:
    """Telegram card for optional AI News Guard.

    AI is a risk guard only. It does not create signals, change score,
    change position size, or override the fixed calendar hard-lock.
    """
    action = str(action or "ALLOW").upper()
    risk = str(risk_level or "LOW").upper()
    reason = str(reason or "No elevated news risk detected.").strip()[:240]
    direction = str(direction or "").upper()
    score_line = f"Signal: Score {score}/6 {direction}\n" if score is not None else ""

    if action == "BLOCK":
        title = "🚫 AI News Guard BLOCKED trade"
        final = "Action: No trade placed"
    elif action == "CAUTION":
        title = "🧠 AI News Guard: CAUTION"
        final = "Action: Trade allowed with caution" if str(mode).upper() != "ALERT_ONLY" else "Action: Watch only"
    else:
        title = "🧠 AI News Guard: ALLOW"
        final = "Action: Trade allowed" if str(mode).upper() != "ALERT_ONLY" else "Action: Watch only"

    return (
        f"{title}\n{_DIV}\n"
        f"Pair: {pair}\n"
        f"{score_line}"
        f"Risk: {risk}\n"
        f"Reason: {reason}\n"
        f"{final}"
    )


# ── 6. News penalty ───────────────────────────────────────────────────────────

def msg_news_penalty(event_names, penalty, score_after, score_before,
                     position_after, position_before) -> str:
    names = ", ".join(event_names) if event_names else "Medium event"
    pos   = (f"${position_before} → ${position_after}"
             if position_before != position_after else f"${position_after} (unchanged)")
    status = "Trading with reduced size" if position_after > 0 else "Score below threshold — watching"
    return (
        f"📰 News Penalty\n{_DIV}\n"
        f"Event:    {names}\n"
        f"Score:    {score_before}/6 → {score_after}/6  (penalty {penalty:+d})\n"
        f"Position: {pos}\n"
        f"{status}"
    )


# ── 7. Cooldown ───────────────────────────────────────────────────────────────

def msg_cooldown_started(streak, cooldown_until_sgt, session_name="",
                         day_losses=0, day_limit=3) -> str:
    remaining = max(0, day_limit - day_losses)
    sline = f"Session: {session_name}\n" if session_name else ""
    return (
        f"🧊 Cooldown Started\n{_DIV}\n"
        f"Reason:  {streak} consecutive losses\n"
        f"{sline}"
        f"Resumes: {cooldown_until_sgt} SGT\n"
        f"Day:     {day_losses}/{day_limit} losses  ({remaining} remaining)"
    )


# ── 8. Daily max ──────────────────────────────────────────────────────────────

def msg_daily_cap(cap_type, count, limit, window="", daily_pnl=None,
                  session_name="", last_loss_time_sgt="", reset_time_sgt="") -> str:
    label  = ("Max losing trades" if cap_type == "losing_trades"
              else ("Max trades/day" if cap_type == "total_trades" else f"{window} max"))
    footer = "Resuming next trading day" if cap_type in ("losing_trades","total_trades") else "Resuming next window"
    pline  = f"Day P&L: ${daily_pnl:+.2f}\n" if daily_pnl is not None else ""
    rline  = f"Resets:  {reset_time_sgt}\n"   if reset_time_sgt else ""
    return (
        f"🛑 Cap Reached\n{_DIV}\n"
        f"Type:  {label}\n"
        f"Count: {count}/{limit}\n"
        f"{pline}{rline}"
        f"{footer}"
    )


# ── 8b. New day ───────────────────────────────────────────────────────────────

def msg_new_day_resume(prev_day_pnl=None, prev_day_trades=0, london_open_sgt="16:00") -> str:
    prev = (f"Yesterday: {prev_day_trades} trade(s)  ${prev_day_pnl:+.2f}\n"
            if prev_day_trades > 0 and prev_day_pnl is not None else "")
    return (
        f"✅ New Trading Day\n{_DIV}\n"
        f"Daily limits reset\n"
        f"{prev}"
        f"Next session: London {london_open_sgt} SGT"
    )


# ── 8c. Session max ───────────────────────────────────────────────────────────


# ── 9. Session open ───────────────────────────────────────────────────────────

def msg_session_open(session_name, session_hours_sgt, trade_cap,
                     trades_today, daily_pnl) -> str:
    icon    = _session_icon(session_name)
    pnl_str = f"${daily_pnl:+.2f}" if trades_today > 0 else "—"
    return (
        f"{icon} {session_name} Open  {session_hours_sgt} SGT\n"
        f"{_DIV}\n"
        f"Today:  {trades_today} trade(s)  {pnl_str}  |  max {trade_cap}\n"
        f"Scanning for EMA + ORB setups..."
    )


# ── 10. Spread skip ───────────────────────────────────────────────────────────

def msg_spread_skip(banner, session_label, spread_pips, limit_pips) -> str:
    _, pair = _split_banner(banner)
    return (
        f"⚠️  Spread Too Wide\n{_DIV}\n"
        f"{pair}  |  {session_label}\n"
        f"Spread: {spread_pips}p  |  Limit: {limit_pips}p  (+{spread_pips - limit_pips} over)\n"
        f"Waiting for spread to normalise"
    )


# ── 11. Order failed ─────────────────────────────────────────────────────────

def msg_order_failed(direction, instrument, units, error,
                     free_margin=None, required_margin=None, retry_attempted=False) -> str:
    mline = (f"Margin: free=${free_margin:.2f}  req=${required_margin:.2f}\n"
             if free_margin is not None and required_margin is not None else "")
    return (
        f"❌ Order Failed\n{_DIV}\n"
        f"{direction}  {_clean_pair(instrument)}  {int(units):,} units\n"
        f"Error:  {error}\n"
        f"{mline}"
        f"Retry:  {'attempted' if retry_attempted else 'not attempted'}\n"
        f"Check OANDA account and logs"
    )


# ── 11b. Margin adjustment ────────────────────────────────────────────────────

def msg_margin_adjustment(instrument, requested_units, adjusted_units,
                          free_margin, required_margin, reason) -> str:
    action = "Skipping trade" if adjusted_units <= 0 else "Using smaller size"
    return (
        f"⚠️  Margin Protection\n{_DIV}\n"
        f"Pair:      {_clean_pair(instrument)}\n"
        f"Requested: {int(requested_units):,}\n"
        f"Adjusted:  {int(adjusted_units):,}\n"
        f"Free Mgn:  ${free_margin:.2f}\n"
        f"Req Mgn:   ${required_margin:.2f}\n"
        f"{_DIV}\n"
        f"{action}"
    )


# ── 12. Error ─────────────────────────────────────────────────────────────────

def msg_error(error_type, detail="") -> str:
    dline = f"Detail: {detail}\n" if detail else ""
    return f"❌ Error\n{_DIV}\n{error_type}\n{dline}Check logs"


# ── 13. Friday cutoff ─────────────────────────────────────────────────────────

def msg_friday_cutoff(cutoff_hour_sgt) -> str:
    return (
        f"📅 Friday Cutoff\n{_DIV}\n"
        f"After {cutoff_hour_sgt:02d}:00 SGT — no new entries\n"
        f"Resuming Monday 16:00 SGT"
    )


# ── 14. Startup ───────────────────────────────────────────────────────────────

def msg_startup(
    version, mode, balance, min_score, cycle_minutes=5,
    max_trades_london=4, max_trades_us=4, max_trades_us_cont=4, max_trades_tokyo=4,
    max_losing_day=4, trading_day_start_hour=8,
    us_early_end=3, dead_zone_start=4, dead_zone_end=7,
    tokyo_start=8, tokyo_end=15, london_start=16, london_end=20,
    us_start=21, us_end=23, max_total_open=1,
    position_full_usd=48, position_partial_usd=30, session_thresholds=None,
    tg_min_score=4, h1_filter_enabled=True, h1_filter_mode="strict",
    # ai_news_guard removed in v2.0
    tokyo_fresh_cross_min_score=None,
) -> str:
    thr     = session_thresholds or {}
    lon_thr = thr.get("London", min_score)
    us_thr       = thr.get("US",      min_score)
    us_cont_thr  = thr.get("US_Cont", min_score)
    tok_thr      = thr.get("Tokyo",   min_score + 1)
    h1_line = (f"H1 filter: {'✅' if h1_filter_enabled else '⬜'} "
               f"{h1_filter_mode.upper() if h1_filter_enabled else 'OFF'}\n")
    ai_line = "AI guard: OFF (rule-based NewsFilter active)\n"
    return (
        f"🚀 {version} started\n{_DIV}\n"
        f"Mode:      {mode}  |  Balance: ${balance:,.2f}\n"
        f"Pair:      GBP/USD (Cable)\n"
        f"Strategy:  M5 EMA + ORB + CPR (rule-based)  |  Cycle: {cycle_minutes} min\n"
        f"Min score: {min_score}/6  |  Alerts: score ≥{tg_min_score} only\n"
        f"Sizes:     ${position_partial_usd} (score 4)  |  ${position_full_usd} (score 5–6)\n"
        f"{h1_line}"
        f"{ai_line}"
        f"{_DIV}\n"
        f"Sessions (SGT = UTC+8)\n"
        f"  ✈️  {dead_zone_start:02d}:00–{dead_zone_end:02d}:59  Dead zone\n"
        f"  🗼 {tokyo_start:02d}:00–{tokyo_end:02d}:59  Tokyo      max {max_trades_tokyo}  score≥{tok_thr} (fresh cross ≥{tokyo_fresh_cross_min_score or tok_thr})\n"
        f"  🇬🇧 {london_start:02d}:00–{london_end:02d}:59  London     max {max_trades_london}  score≥{lon_thr}\n"
        + (f"  🚫 21:00–23:59  US session   disabled\n" if us_start >= 99 else
           f"  🗽 {us_start:02d}:00–{us_end:02d}:59  US session max {max_trades_us}  score≥{us_thr}\n")
        + (f"  🚫 US Cont.    disabled\n" if us_early_end >= 99 else
           f"  🌙 00:00–{us_early_end:02d}:59  US Cont.   max {max_trades_us_cont}  score≥{us_cont_thr}\n")
        + f"{_DIV}\n"
        + f"Day reset: {trading_day_start_hour:02d}:00 SGT  |  Loss max: {max_losing_day}/day\n"
        f"Global max: {max_total_open} open trade" if int(max_total_open) == 1 else f"Global max: {max_total_open} open trades"
    )



def _ai_stats_section(ai_stats: dict | None) -> str:
    """Render compact AI Guard tracking summary."""
    if not ai_stats or ai_stats.get("total_decisions", 0) == 0:
        return ""
    impact = float(ai_stats.get("estimated_ai_impact_usd") or 0)
    impact_icon = "🟢" if impact > 0 else ("🔴" if impact < 0 else "⚪")
    return (
        f"{_DIV}\nAI Guard Tracking\n"
        f"  Decisions: {ai_stats.get('total_decisions', 0)}  "
        f"Allow {ai_stats.get('allowed', 0)} | Caution {ai_stats.get('cautioned', 0)} | Block {ai_stats.get('blocked', 0)}\n"
        f"  Blocked:   {ai_stats.get('blocked_losers', 0)} losers avoided | "
        f"{ai_stats.get('blocked_winners', 0)} winners missed | "
        f"{ai_stats.get('blocked_open', 0)} open | {ai_stats.get('blocked_expired', 0)} expired\n"
        f"  Est impact: ${impact:+.2f} {impact_icon}\n"
    )

# ── 15. Daily report ─────────────────────────────────────────────────────────

def msg_daily_report(
    day_label, day_stats, wtd_stats, mtd_stats, open_count, report_time,
    blocked_spread=0, blocked_news=0, blocked_signal=0,
    session_stats=None, ai_stats=None,
) -> str:
    # No trades today
    if day_stats["count"] == 0:
        oline = f"Open now: {open_count} position(s)\n" if open_count > 0 else ""
        return (
            f"📊 Daily Summary — {day_label}\n{_DIV}\n"
            f"No trades closed today\n"
            f"{_DIV}\n"
            f"Month-to-date\n  {_mini_stats(mtd_stats)}\n"
            f"{_ai_stats_section(ai_stats)}"
            f"{_DIV}\n"
            f"{oline}"
            f"Report: {report_time}"
        )

    icon  = _pnl_icon(day_stats["net_pnl"])
    oline = f"Open now: {open_count} position(s)\n" if open_count > 0 else ""
    parts = []
    if blocked_spread:  parts.append(f"{blocked_spread} spread")
    if blocked_news:    parts.append(f"{blocked_news} news")
    if blocked_signal:  parts.append(f"{blocked_signal} signal")
    bline = f"Blocked:  {', '.join(parts)}\n" if parts else ""

    best  = day_stats.get("best_trade")
    worst = day_stats.get("worst_trade")
    bst   = f"  Best:     ${best['pnl']:+.2f}  ({best['time']} SGT)\n"   if best  else ""
    wst   = f"  Worst:    ${worst['pnl']:+.2f}  ({worst['time']} SGT)\n" if worst else ""
    isl   = day_stats.get("instant_sl_count", 0)
    islline = f"  ⚡ Instant SL: {isl} trade(s) ≤5min\n" if isl > 0 else ""
    fire  = " 🔥" if day_stats.get("wins", 0) >= 3 else ""

    # Session breakdown (Tokyo / London / US / US Cont.)
    sess_block = ""
    if session_stats:
        sess_block = f"{_DIV}\nSession breakdown\n"
        for name, s in session_stats.items():
            if s["count"] == 0:
                continue
            wr_str   = f"{s['win_rate']:.0f}%"
            wl_str   = f"{s['wins']}W/{s['losses']}L"
            pnl_str  = f"${s['net_pnl']:+.2f}"
            sess_icon = _session_icon(name)
            sess_block += f"  {sess_icon} {name:<12} {wl_str:<8} {wr_str:<6} {pnl_str}\n"

    return (
        f"📊 Daily Summary — {day_label}\n"
        f"{sess_block}"
        f"{_DIV}\n"
        f"Day total\n"
        f"  Trades:   {day_stats['count']}  ({day_stats['wins']}W{fire} / {day_stats['losses']}L)\n"
        f"  Win rate: {day_stats['win_rate']:.0f}%\n"
        f"  Net P&L:  ${day_stats['net_pnl']:+.2f}  {icon}\n"
        f"{bst}{wst}{islline}{bline}"
        f"{_DIV}\n"
        f"Month-to-date\n  {_mini_stats(mtd_stats)}\n"
        f"{_ai_stats_section(ai_stats)}"
        f"{_DIV}\n"
        f"{oline}"
        f"Report: {report_time}"
    )


# ── 16. Weekly report ─────────────────────────────────────────────────────────



def _h1_section(h1_stats: dict | None, h1_filter_mode: str = "strict") -> str:
    """Render H1 filter aligned vs counter-trend breakdown block."""
    if not h1_stats:
        return ""
    a  = h1_stats.get("aligned", {})
    ct = h1_stats.get("counter", {})
    if not a and not ct:
        return ""

    lines = [f"{_DIV}\nH1 Filter [{h1_filter_mode}]\n"]

    mx = max(a.get("win_rate", 0), ct.get("win_rate", 0)) or 1

    if a.get("count", 0) > 0:
        bar = _ascii_bar(a["win_rate"], mx)
        lines.append(
            f"  Aligned    {bar} {a['win_rate']:>5.1f}%  "
            f"{a['wins']}W/{a['losses']}L  ${a['net_pnl']:+.2f}\n"
        )
    else:
        lines.append("  Aligned    — no trades\n")

    if ct.get("count", 0) > 0:
        bar = _ascii_bar(ct["win_rate"], mx)
        lines.append(
            f"  Counter ⚠️  {bar} {ct['win_rate']:>5.1f}%  "
            f"{ct['wins']}W/{ct['losses']}L  ${ct['net_pnl']:+.2f}\n"
        )
    else:
        lines.append("  Counter ⚠️  — no trades\n")

    # Recommendation line
    a_wr  = a.get("win_rate", 0)
    ct_wr = ct.get("win_rate", 0)
    ct_n  = ct.get("count", 0)
    diff  = round(a_wr - ct_wr, 1)

    if h1_filter_mode == "strict":
        if ct_n == 0:
            rec = "  → Strict mode active — all counter-trend entries blocked"
        elif ct_n < 5:
            rec = f"  → {ct_n} counter-trend trades slipped through — check filter"
        else:
            rec = f"  → {ct_n} counter-trend trades in strict mode — investigate"
    elif ct_n < 5:
        rec = f"  → {ct_n} counter-trend trades — need more data"
    elif diff >= 20:
        rec = f"  → Counter-trend {diff}pts lower — switch to strict mode"
    elif diff >= 10:
        rec = f"  → Counter-trend {diff}pts lower — monitor closely"
    else:
        rec = f"  → H1 split similar ({diff}pts) — soft mode justified"

    lines.append(rec + "\n")
    return "".join(lines)


def msg_weekly_report(week_label, stats, sessions, setups, report_time, pairs=None, h1_stats=None, h1_filter_mode="strict", ai_stats=None) -> str:
    if stats["count"] == 0:
        return f"📅 Weekly Report — {week_label}\n{_DIV}\nNo closed trades.\nReport: {report_time}"

    icon   = _pnl_icon(stats["net_pnl"])
    pf_str = f"{stats['profit_factor']}" if stats["profit_factor"] is not None else "n/a"
    rline  = f"Avg R:       {stats['avg_r']}R\n" if stats.get("avg_r") is not None else ""
    bline  = (f"Best:        ${stats['best_trade']['pnl']:+.2f}  ({stats['best_trade']['time']} SGT)\n"
              if stats.get("best_trade") else "")
    wline  = (f"Worst:       ${stats['worst_trade']['pnl']:+.2f}  ({stats['worst_trade']['time']} SGT)\n"
              if stats.get("worst_trade") else "")

    def _sec(data):
        if not data: return ""
        mx = max(s["win_rate"] for s in data.values()) or 1
        return "".join(
            f"  {n:<10} {_ascii_bar(s['win_rate'],mx)} {s['win_rate']:>5.1f}%  {s['wins']}W/{s['losses']}L  ${s['net_pnl']:+.2f}\n"
            for n, s in data.items()
        )

    def _setup_sec(data):
        if not data: return ""
        mx = max(s["win_rate"] for s in data.values()) or 1
        return "".join(
            f"  {n[:18]:<18} {_ascii_bar(s['win_rate'],mx)} {s['win_rate']:>5.1f}%\n"
            for n, s in data.items()
        )

    pf, wr, n = stats["profit_factor"] or 0, stats["win_rate"], stats["count"]
    if n < 10:     verdict = f"⚠️ Small sample ({n} trades)"
    elif pf >= 1.3 and wr >= 48: verdict = f"✅ Healthy — PF {pf}  WR {wr}%"
    elif pf >= 1.0: verdict = f"🟡 Marginal — PF {pf}  WR {wr}%  Monitor"
    else:           verdict = f"🔴 Negative — PF {pf}  WR {wr}%  Review"

    return (
        f"📅 Weekly Report — {week_label}\n{_DIV}\n"
        f"{icon} Trades: {stats['count']}  ({stats['wins']}W / {stats['losses']}L)\n"
        f"Net P&L:     ${stats['net_pnl']:+.2f}\n"
        f"Win rate:    {wr}%\n"
        f"P.Factor:    {pf_str}\n"
        f"{rline}Streaks:     {stats['max_win_streak']}W / {stats['max_loss_streak']}L max\n"
        f"{bline}{wline}"
        f"{_DIV}\nBy Session\n{_sec(sessions)}"
        f"{_DIV}\nBy Pair\n{_sec(pairs) if pairs else ''}"
        f"{_DIV}\nBy Setup\n{_setup_sec(setups)}"
        + _h1_section(h1_stats, h1_filter_mode=h1_filter_mode)
        + _ai_stats_section(ai_stats)
        + f"{_DIV}\n{verdict}\nReport: {report_time}"
    )


# ── 17. Monthly report ────────────────────────────────────────────────────────

def msg_monthly_report(month_label, stats, sessions, setups, scores,
                       mom_delta, prior_month_pnl, report_time,
                       h1_stats=None, h1_filter_mode="strict", ai_stats=None) -> str:
    if stats["count"] == 0:
        return f"📆 Monthly Report — {month_label}\n{_DIV}\nNo closed trades.\nReport: {report_time}"

    icon   = _pnl_icon(stats["net_pnl"])
    pf_str = f"{stats['profit_factor']}" if stats["profit_factor"] is not None else "n/a"
    rline  = f"Avg R:         {stats['avg_r']}R\n" if stats.get("avg_r") is not None else ""
    mline  = ""
    if mom_delta is not None and prior_month_pnl is not None:
        di    = "🟢" if mom_delta >= 0 else "🔴"
        mline = f"vs prior:      ${prior_month_pnl:+.2f}  →  {di} {mom_delta:+.2f}\n"
    bline  = (f"Best trade:    ${stats['best_trade']['pnl']:+.2f}  ({stats['best_trade']['time']} SGT)\n"
              if stats.get("best_trade") else "")
    wline  = (f"Worst trade:   ${stats['worst_trade']['pnl']:+.2f}  ({stats['worst_trade']['time']} SGT)\n"
              if stats.get("worst_trade") else "")

    def _sec(data, w=18):
        if not data: return ""
        mx = max(s["win_rate"] for s in data.values()) or 1
        return "".join(
            f"  {n[:w]:<{w}} {_ascii_bar(s['win_rate'],mx)} {s['win_rate']:>5.1f}%  {s['wins']}W/{s['losses']}L\n"
            for n, s in data.items()
        )

    pf, wr, n = stats["profit_factor"] or 0, stats["win_rate"], stats["count"]
    if n < 20:
        verdict, rec = f"⚠️ Small sample ({n} trades)", "Collect more data before any changes."
    elif pf >= 1.3 and wr >= 48:
        verdict, rec = f"✅ Healthy — PF {pf}  WR {wr}%", "System performing well. No changes needed."
    elif pf >= 1.0:
        verdict, rec = f"🟡 Marginal — PF {pf}  WR {wr}%", "Consider raising signal_threshold by +1."
    else:
        verdict, rec = f"🔴 Negative — PF {pf}  WR {wr}%", "Review session breakdown. Pause worst session."

    return (
        f"📆 Monthly Report — {month_label}\n{_DIV}\n"
        f"{icon} Trades: {stats['count']}  ({stats['wins']}W / {stats['losses']}L)\n"
        f"Net P&L:       ${stats['net_pnl']:+.2f}\n"
        f"{mline}"
        f"Win rate:      {wr}%\n"
        f"P.Factor:      {pf_str}\n"
        f"{rline}"
        f"Gross P:       ${stats['gross_profit']:.2f}\n"
        f"Gross L:       ${stats['gross_loss']:.2f}\n"
        f"Streaks:       {stats['max_win_streak']}W / {stats['max_loss_streak']}L max\n"
        f"{bline}{wline}"
        f"{_DIV}\nBy Session\n{_sec(sessions)}"
        f"{_DIV}\nBy Setup\n{_sec(setups)}"
        f"{_DIV}\nBy Score\n{_sec(scores, w=8)}"
        + _h1_section(h1_stats, h1_filter_mode=h1_filter_mode)
        + _ai_stats_section(ai_stats)
        + f"{_DIV}\n{verdict}\n💡 {rec}\nReport: {report_time}"
    )
