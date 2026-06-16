from __future__ import annotations

import signal
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot import run_bot_cycle
from oanda_trader import OandaTrader
from reporting import send_daily_report, send_weekly_report, send_monthly_report, send_weekly_export, send_monthly_csv_export, send_monthly_signal_export
from telegram_alert import TelegramAlert
from telegram_templates import msg_startup
from config_loader import DATA_DIR, load_settings
from database import Database
from logging_utils import configure_logging, get_logger
from startup_checks import run_startup_checks

configure_logging()
logger = get_logger(__name__)
SG_TZ = pytz.timezone('Asia/Singapore')

# ── Health-check HTTP server ───────────────────────────────────────────────────
# Railway (and other PaaS platforms) can poll GET /health to confirm the process
# is alive. Returns 200 with a rich JSON body so health means "actually trading",
# not just "process is running". GET /metrics returns Prometheus-style counters.

_scheduler_ref: BlockingScheduler | None = None
_process_start: float = 0.0


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        import json as _json
        import time as _time
        from state_utils import load_json, RUNTIME_STATE_FILE

        if self.path in ("/health", "/healthz"):
            try:
                state    = load_json(RUNTIME_STATE_FILE, {})
                running  = bool(_scheduler_ref and _scheduler_ref.running)
                uptime_s = int(_time.time() - _process_start) if _process_start else 0
                body = _json.dumps({
                    "status":             "ok" if running else "starting",
                    "scheduler_running":  running,
                    "last_cycle_started": state.get("last_cycle_started"),
                    "last_cycle_status":  state.get("status"),
                    "oanda_failures":     int(state.get("oanda_consecutive_failures", 0)),
                    "uptime_s":           uptime_s,
                }, separators=(",", ":")).encode()
                # Always 200 while alive — scheduler warmup is not a failure
                code = 200
            except Exception as exc:
                body = _json.dumps({"status": "error", "detail": str(exc)}).encode()
                code = 500

            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/metrics":
            try:
                from state_utils import load_json, RUNTIME_STATE_FILE
                import time as _time
                state   = load_json(RUNTIME_STATE_FILE, {})
                uptime  = int(_time.time() - _process_start) if _process_start else 0
                lines   = [
                    f'bot_uptime_seconds {uptime}',
                    f'bot_scheduler_running {1 if (_scheduler_ref and _scheduler_ref.running) else 0}',
                    f'bot_oanda_consecutive_failures {int(state.get("oanda_consecutive_failures", 0))}',
                ]
                body = "\n".join(lines).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_response(500)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):  # silence access logs
        pass


def _start_health_server(port: int = 8080) -> None:
    """Start the health-check HTTP server in a background daemon thread."""
    import os
    port = int(os.environ.get("PORT", port))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
        t.start()
        logger.info("Health-check server listening on port %d — GET /health", port)
    except Exception as exc:
        logger.warning("Could not start health-check server on port %d: %s", port, exc)


def run_db_retention_cleanup():
    settings = load_settings()
    retention_days = int(settings.get('db_retention_days', 90))
    vacuum_weekly = bool(settings.get('db_vacuum_weekly', True))
    is_weekly_vacuum_day = datetime.now(SG_TZ).weekday() == 6

    logger.info('Starting DB retention cleanup | retention_days=%s | weekly_vacuum=%s', retention_days, vacuum_weekly)
    try:
        db = Database()
        summary = db.purge_old_data(retention_days=retention_days, vacuum=bool(vacuum_weekly and is_weekly_vacuum_day))
        logger.info('DB retention cleanup complete: %s', summary)
    except Exception as exc:
        logger.exception('DB retention cleanup failed: %s', exc)


def main():
    global _scheduler_ref, _process_start
    import time as _time
    _process_start = _time.time()

    settings       = load_settings()
    cycle_minutes  = int(settings.get('cycle_minutes', 5))
    cleanup_hour   = int(settings.get('db_cleanup_hour_sgt', 0))
    cleanup_minute = int(settings.get('db_cleanup_minute_sgt', 15))
    retention_days = int(settings.get('db_retention_days', 90))

    # Report schedule — configurable via settings.json
    daily_report_hour    = int(settings.get('daily_report_hour_sgt',     7))  # 07:50 SGT — after US Cont. closes
    daily_report_minute  = int(settings.get('daily_report_minute_sgt',   0))
    weekly_report_hour   = int(settings.get('weekly_report_hour_sgt',    8))
    weekly_report_minute = int(settings.get('weekly_report_minute_sgt', 15))
    monthly_report_hour  = int(settings.get('monthly_report_hour_sgt',   8))
    monthly_report_minute= int(settings.get('monthly_report_minute_sgt', 0))

    # Singleton alert — constructed once, shared across all cycles.
    # Avoids re-reading secrets + creating new HTTP sessions every 5 minutes.
    _alert = TelegramAlert()

    logger.info('%s — Scheduler starting', settings.get('bot_name', 'Cable AI Scalp v1.2'))
    logger.info('DATA_DIR : %s', DATA_DIR)
    logger.info('Python   : %s', sys.version.split()[0])
    for warning in run_startup_checks():
        logger.warning(warning)

    scheduler = BlockingScheduler(timezone=SG_TZ)
    scheduler.add_job(
        lambda: run_bot_cycle(alert=_alert),
        IntervalTrigger(minutes=cycle_minutes),
        id='trade_cycle',
        name=f'{cycle_minutes}-min trade cycle',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60  # skip if > 60s late — prevents burst catch-up cycles,
    )

    scheduler.add_job(
        run_db_retention_cleanup,
        CronTrigger(hour=cleanup_hour, minute=cleanup_minute, timezone=SG_TZ),
        id='db_retention_cleanup',
        name=f'DB retention cleanup ({retention_days}-day rolling)',
        max_instances=1,
        coalesce=True,
    )

    # ── Telegram performance reports ───────────────────────────────────────────
    # Monthly: first Monday of each month at monthly_report_hour SGT
    # The first-Monday guard is enforced inside send_monthly_report() itself,
    # so this job fires every Monday but only sends on the first Monday.
    scheduler.add_job(
        send_monthly_report,
        CronTrigger(day_of_week='mon', hour=monthly_report_hour,
                    minute=monthly_report_minute, timezone=SG_TZ),
        id='monthly_report',
        name='Monthly performance report (first Monday)',
        max_instances=1,
        coalesce=True,
    )

    # Weekly: every Monday at weekly_report_hour SGT (covers prior Mon–Fri)
    scheduler.add_job(
        send_weekly_report,
        CronTrigger(day_of_week='mon', hour=weekly_report_hour,
                    minute=weekly_report_minute, timezone=SG_TZ),
        id='weekly_report',
        name='Weekly performance report',
        max_instances=1,
        coalesce=True,
    )

    # Weekly export: Monday 08:05 SGT — 5 min after weekly report
    scheduler.add_job(
        send_weekly_export,
        CronTrigger(day_of_week='mon', hour=weekly_report_hour,
                    minute=weekly_report_minute + 5, timezone=SG_TZ),
        id='weekly_export',
        name='Weekly trade history export',
        max_instances=1,
        coalesce=True,
    )

    # Monthly CSV export: last day of month at 08:30 SGT — cumulative v1.9 trade log
    scheduler.add_job(
        send_monthly_csv_export,
        CronTrigger(day='last', hour=8, minute=30, timezone=SG_TZ),
        id='monthly_csv_export',
        name='Monthly cumulative CSV export',
        max_instances=1,
        coalesce=True,
    )

    # Monthly signal log export: last day of month at 08:35 SGT
    # Only sends if signal_logging_enabled = true in settings.json
    scheduler.add_job(
        send_monthly_signal_export,
        CronTrigger(day='last', hour=8, minute=35, timezone=SG_TZ),
        id='monthly_signal_export',
        name='Monthly signal log CSV export',
        max_instances=1,
        coalesce=True,
    )

    # Daily: Mon–Fri at daily_report_hour SGT (07:50 = after US Cont. closes, full night captured)
    scheduler.add_job(
        send_daily_report,
        CronTrigger(day_of_week='mon-fri', hour=daily_report_hour,
                    minute=daily_report_minute, timezone=SG_TZ),
        id='daily_report',
        name='Daily performance report',
        max_instances=1,
        coalesce=True,
    )

    def _graceful_shutdown(signum, frame):
        logger.info('Received signal %s — waiting for active cycle to finish (max 120 s)...', signum)
        # wait=True lets any running trade cycle complete before exit,
        # preventing a half-placed order that is never recorded locally.
        # The thread + join(timeout) provides a hard 120 s safety cap.
        t = threading.Thread(
            target=lambda: scheduler.shutdown(wait=True),
            daemon=True,
            name="scheduler-shutdown",
        )
        t.start()
        t.join(timeout=120)
        if t.is_alive():
            logger.warning('Shutdown timeout reached (120 s) — forcing exit.')
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    logger.info('Jobs scheduled:')
    logger.info('  Trade cycle    — every %s minutes', cycle_minutes)
    logger.info('  DB cleanup     — daily at %02d:%02d Asia/Singapore', cleanup_hour, cleanup_minute)
    logger.info('  DB retention   — rolling %s days', retention_days)
    logger.info('  Monthly report — first Monday of month at %02d:%02d SGT',
                monthly_report_hour, monthly_report_minute)
    logger.info('  Weekly report  — every Monday at %02d:%02d SGT',
                weekly_report_hour, weekly_report_minute)
    logger.info('  Weekly export  — every Monday at %02d:%02d SGT (trade_history.csv)',
                weekly_report_hour, weekly_report_minute + 5)
    logger.info('  Daily report   — Mon–Fri at %02d:%02d SGT',
                daily_report_hour, daily_report_minute)

    logger.info('Running startup cycle...')
    try:
        _trader  = OandaTrader(demo=bool(settings.get('demo_mode', True)))
        _summary = _trader.login_with_summary()
        _balance = _summary["balance"] if _summary else 0.0
        _threshold = int(settings.get('signal_threshold', 4))
        _mode    = 'DEMO' if settings.get('demo_mode', True) else 'LIVE'
        _version = settings.get('bot_name', 'Cable AI Scalp v1.2')

        # ── Startup message deduplication ──────────────────────────────────
        # Suppress duplicate startup alerts when Railway restarts the container
        # rapidly (health-check blip, rolling deploy, etc.).  Only send if we
        # have not already sent a startup message in the last 90 seconds.
        from state_utils import load_json, save_json, RUNTIME_STATE_FILE
        import time as _time_mod
        _state      = load_json(RUNTIME_STATE_FILE, {})
        _last_start = float(_state.get("last_startup_ts", 0))
        _now_ts     = _time_mod.time()
        _suppress_secs = int(settings.get('startup_dedup_seconds', 90))
        _suppress   = (_now_ts - _last_start) < _suppress_secs

        if not _suppress:
            _alert.send(msg_startup(
                _version, _mode, _balance, _threshold,
                cycle_minutes=int(settings.get('cycle_minutes', 5)),
                max_trades_london=int(settings.get('max_trades_london', 4)),
                max_trades_us=int(settings.get('max_trades_us', 4)),
                max_trades_us_cont=int(settings.get('max_trades_us_cont', 4)),
                max_trades_tokyo=int(settings.get('max_trades_tokyo', 4)),
                max_losing_day=int(settings.get('max_losing_trades_day', 4)),
                trading_day_start_hour=int(settings.get('trading_day_start_hour_sgt', 8)),
                us_early_end=int(settings.get('us_session_early_end_hour', 3)),
                dead_zone_start=int(settings.get('dead_zone_start_hour', 4)),
                dead_zone_end=int(settings.get('dead_zone_end_hour', 7)),
                tokyo_start=int(settings.get('tokyo_session_start_hour', 8)),
                tokyo_end=int(settings.get('tokyo_session_end_hour', 15)),
                london_start=int(settings.get('london_session_start_hour', 16)),
                london_end=int(settings.get('london_session_end_hour', 20)),
                us_start=int(settings.get('us_session_start_hour', 21)),
                us_end=int(settings.get('us_session_end_hour', 23)),
                max_total_open=int(settings.get('max_total_open_trades', 1)),
                tg_min_score=int(settings.get('telegram_min_score_alert', 3)),
                h1_filter_enabled=bool(settings.get('h1_filter_enabled', True)),
                h1_filter_mode=settings.get('h1_filter_mode', 'soft'),
                position_full_usd=int(settings.get('position_full_usd', 48)),
                position_partial_usd=int(settings.get('position_partial_usd', 30)),
                session_thresholds=settings.get('session_thresholds', {}),
                tokyo_fresh_cross_min_score=settings.get('tokyo_fresh_cross_min_score'),
            ))
            _state["last_startup_ts"] = _now_ts
            save_json(RUNTIME_STATE_FILE, _state)
            logger.info("Startup Telegram sent.")
            # weekly export runs via scheduler (Monday 08:05 SGT)
        else:
            logger.info(
                "Startup Telegram suppressed — last sent %.0fs ago (dedup window 90s).",
                _now_ts - _last_start,
            )
    except Exception as _e:
        logger.warning('Could not send startup Telegram alert: %s', _e)

    _scheduler_ref = scheduler
    run_bot_cycle(alert=_alert)
    scheduler.start()


if __name__ == '__main__':
    import time as _crash_time
    # Start health server IMMEDIATELY — before main() loads settings or connects
    # to OANDA. Ensures Railway's healthcheck at /health always gets a 200
    # response even during startup warmup.
    _start_health_server()
    # Crash-loop guard: if the process dies and restarts in under 30 seconds
    # repeatedly, sleep before exiting so Railway's restart backoff has time
    # to work and we don't burn through retries in a burst.
    _boot_ts = _crash_time.time()
    try:
        main()
    except SystemExit:
        raise   # clean SIGTERM shutdown — let it through immediately
    except Exception as _fatal:
        _uptime = _crash_time.time() - _boot_ts
        logger.critical('Unhandled fatal exception after %.0fs uptime: %s', _uptime, _fatal, exc_info=True)
        if _uptime < 30:
            logger.warning('Fast crash detected (%.0fs) — sleeping 15s before exit to prevent restart burst', _uptime)
            _crash_time.sleep(15)
        raise
