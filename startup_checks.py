from __future__ import annotations

from pathlib import Path

from config_loader import DATA_DIR, SETTINGS_FILE, load_secrets, load_settings
from state_utils import CALENDAR_CACHE_FILE


def run_startup_checks() -> list[str]:
    settings = load_settings()
    secrets  = load_secrets()
    warnings: list[str] = []

    if not Path(DATA_DIR).exists():
        warnings.append(f"DATA_DIR missing: {DATA_DIR}")
    if not Path(SETTINGS_FILE).exists():
        warnings.append(f"settings file missing: {SETTINGS_FILE}")
    if not secrets.get("OANDA_ACCOUNT_ID"):
        warnings.append("OANDA_ACCOUNT_ID not set; broker calls will fail until configured")
    if not secrets.get("OANDA_API_KEY"):
        warnings.append("OANDA_API_KEY not set; broker calls will fail until configured")
    if not secrets.get("TELEGRAM_TOKEN") or not secrets.get("TELEGRAM_CHAT_ID"):
        warnings.append("Telegram not fully configured; alerts will be skipped")
    if int(settings.get("cycle_minutes", 5)) <= 0:
        warnings.append("cycle_minutes must be > 0")

    margin_safety = float(settings.get("margin_safety_factor", 0.6))
    if not 0 < margin_safety <= 1:
        warnings.append("margin_safety_factor must be between 0 and 1")
    retry_safety = float(settings.get("margin_retry_safety_factor", 0.4))
    if not 0 < retry_safety <= 1:
        warnings.append("margin_retry_safety_factor must be between 0 and 1")
    if retry_safety > margin_safety:
        warnings.append("margin_retry_safety_factor should not exceed margin_safety_factor")

    # Validate pairs configuration
    pairs = settings.get("pairs", {})
    if not pairs:
        warnings.append("No pairs defined in settings[\"pairs\"] — bot will not trade")
    else:
        enabled = [k for k, v in pairs.items() if isinstance(v, dict) and v.get("enabled", True)]
        if not enabled:
            warnings.append("All pairs are disabled in settings[\"pairs\"] — bot will not trade")
        for pair_name, pair_cfg in pairs.items():
            if not isinstance(pair_cfg, dict) or not pair_cfg.get("enabled", True):
                continue
            pip = float(pair_cfg.get("pip_size", 0) or 0)
            if pip <= 0:
                warnings.append(f"pairs.{pair_name}.pip_size must be > 0")
            # SL/TP validated via pair_sl_tp config (fixed-pip mode)
            pass

    if not CALENDAR_CACHE_FILE.exists():
        if bool(settings.get("news_fail_closed", True)):
            warnings.append(
                "calendar_cache.json not found — fail-closed news filter will block new entries until "
                "the first successful calendar fetch completes. Resolves on the first cycle."
            )
        else:
            warnings.append(
                "calendar_cache.json not found — news filter will pass all trades until "
                "the first successful calendar fetch completes. Resolves on the first cycle."
            )

    # global concurrent-trade cap sanity
    max_total = int(settings.get("max_total_open_trades", 1))
    if max_total < 0:
        warnings.append("max_total_open_trades must be >= 0 (0 = disabled)")

    # Tokyo session hour ordering
    tok_s = int(settings.get("tokyo_session_start_hour", 8))
    tok_e = int(settings.get("tokyo_session_end_hour",  15))
    if tok_s >= tok_e:
        warnings.append(
            f"tokyo_session_start_hour ({tok_s}) must be < tokyo_session_end_hour ({tok_e})"
        )

    return warnings
