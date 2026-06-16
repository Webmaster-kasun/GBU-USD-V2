"""
signals.py — Cable Smart v3.0  (GBP/USD only)
==============================================

Built from forensic analysis of 17 real trades (Apr–Jun 2026).

ROOT CAUSE OF LOSSES (from real data):
  13/17 trades: SL swept first → THEN price moved to TP.
  Classic retail liquidity sweep. Market dips below support,
  hits all the retail stop losses, then reverses up.
  Old bot entered BEFORE the sweep. New bot waits AFTER.

THREE-LAYER INTELLIGENCE:

  LAYER 1 — MARKET REGIME DETECTION (H4)
    TRENDING_UP:   H4 EMA20 slope > threshold, price > H4 EMA50
    TRENDING_DOWN: H4 EMA20 slope < -threshold, price < H4 EMA50
    RANGING:       H4 EMA20 flat — oscillating market
    
    In RANGING: only trade near range extremes (support/resistance).
    In TRENDING_UP: only BUY. Block all SELL.
    In TRENDING_DOWN: only SELL. Block all BUY.
    Real data showed SELL WR=11% because market was in RANGING/UP regime.

  LAYER 2 — SWEEP + RECLAIM ENTRY (M15)
    For BUY: price must sweep BELOW the 20-bar low, then CLOSE BACK ABOVE it.
    For SELL: price must sweep ABOVE the 20-bar high, then CLOSE BACK BELOW it.
    This confirms the liquidity sweep happened BEFORE we enter.
    Old bot entered at EMA cross → got swept. New bot enters AFTER the sweep.

  LAYER 3 — CONFIRMATION FILTERS
    EMA13/34 alignment on M15 (trend direction)
    H1 EMA21 direction
    ATR gate (minimum volatility)
    News filter (external)
    Ranging market size reduction (50% position in ranging)

SCORING (0–6):
  Regime:   TRENDING ±3 (strong), RANGING near-extreme ±2, RANGING mid = BLOCK
  Sweep:    confirmed +2 | not yet +0
  EMA:      aligned +1
  H1:       aligned with direction +1 (bonus)

SL/TP: Fixed from settings. Default SL=15p TP=25p.
"""

import time
import logging
from datetime import datetime as _dt
import pytz as _pytz
from config_loader import load_secrets, load_settings, DATA_DIR
from state_utils    import load_json, save_json
from oanda_trader   import make_oanda_session

log = logging.getLogger(__name__)

_CPR_CACHE_FILE = DATA_DIR / "cpr_cache.json"
_ORB_CACHE_FILE = DATA_DIR / "orb_cache.json"
_SGT = _pytz.timezone("Asia/Singapore")
_UTC = _pytz.utc

# Constants
MIN_TRADE_SCORE     = 5
EMA_FAST            = 13
EMA_SLOW            = 34
H4_EMA_FAST         = 20
H4_EMA_SLOW         = 50
H4_SLOPE_THRESHOLD  = 0.00005   # H4 EMA20 must move this much per bar to be "trending"
SWEEP_LOOKBACK      = 20        # M15 bars for support/resistance level
RANGING_SIZE_MULT   = 0.5       # 50% position in ranging market


def score_to_position_usd(score: int, settings: dict | None = None) -> int:
    s = settings or {}
    sr = s.get("score_risk_usd", {})
    for k in (str(score), score):
        if k in sr:
            try: return max(int(sr[k]), 0)
            except: break
    if score >= 6: return int(s.get("position_full_usd", 40))
    if score >= 5: return int(s.get("position_partial_usd", 30))
    return 0


def _ema(closes: list, period: int) -> list:
    if len(closes) < period: return [sum(closes)/max(len(closes),1)]*len(closes)
    k = 2.0/(period+1); e = sum(closes[:period])/period; out=[e]
    for p in closes[period:]: e=p*k+e*(1-k); out.append(e)
    return out


def _atr(highs, lows, closes, period=14):
    n = len(closes)
    if n < period+2: return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1])) for i in range(1,n)]
    a = sum(trs[:period])/period
    for tr in trs[period:]: a=(a*(period-1)+tr)/period
    return a


class SignalEngine:

    def __init__(self, demo: bool = True):
        secrets         = load_secrets()
        self.api_key    = secrets.get("OANDA_API_KEY","")
        self.account_id = secrets.get("OANDA_ACCOUNT_ID","")
        self.base_url   = ("https://api-fxpractice.oanda.com" if demo
                           else "https://api-fxtrade.oanda.com")
        self.headers    = {"Authorization": f"Bearer {self.api_key}",
                           "Content-Type": "application/json"}
        self.session    = make_oanda_session(allowed_methods=["GET"])

    # ── Main entry ────────────────────────────────────────────────────────────

    def analyze(self, instrument: str = "GBP_USD",
                settings: dict | None = None):
        """
        Cable Smart v3.0 signal engine.
        Returns: (score, direction, details, levels, position_usd)
        """
        if settings is None:
            settings = load_settings()

        PIP = float(settings.get("pip_size", 0.0001))
        DP  = 5 if PIP <= 0.0001 else 3

        reasons = []
        levels  = {"pip_size": PIP}

        # ── LAYER 1: Market Regime Detection (H4) ────────────────────────────
        regime, h4_info = self._detect_regime(instrument, DP, settings)
        levels.update(h4_info)
        reasons.append(f"Regime: {regime} | H4 EMA{H4_EMA_FAST}={h4_info.get('h4_ema20',0):.{DP}f} "
                       f"EMA{H4_EMA_SLOW}={h4_info.get('h4_ema50',0):.{DP}f} "
                       f"slope={h4_info.get('h4_slope',0)*10000:+.2f}p/bar")

        # ── LAYER 2: M15 data ────────────────────────────────────────────────
        m15_n = int(settings.get("m5_candle_count", 60))
        m15_c, m15_h, m15_l = self._candles(instrument, "M15", max(m15_n, 60))
        if len(m15_c) < 35:
            return 0, "NONE", "Not enough M15 data", levels, 0

        price   = m15_c[-1]
        atr_val = _atr(m15_h, m15_l, m15_c, int(settings.get("atr_period",14)))
        atr_pip = (atr_val/PIP) if atr_val else 0

        levels["current_price"] = round(price, DP)
        levels["atr_pips_m15"]  = round(atr_pip, 1)

        # ATR gate: minimum 5 pips volatility
        if atr_pip < 5.0:
            return 0, "NONE", f"ATR {atr_pip:.1f}p < 5p (market too flat)", levels, 0

        # ── EMA 13/34 on M15 ─────────────────────────────────────────────────
        fast_p = int(settings.get("ema_fast_period", EMA_FAST))
        slow_p = int(settings.get("ema_slow_period", EMA_SLOW))
        ema_f  = _ema(m15_c, fast_p)
        ema_s  = _ema(m15_c, slow_p)
        ef_now = ema_f[-1]; ef_prv = ema_f[-2]
        es_now = ema_s[-1]; es_prv = ema_s[-2]

        levels[f"ema{fast_p}"] = round(ef_now, DP)
        levels[f"ema{slow_p}"] = round(es_now, DP)

        # EMA direction
        if ef_now > es_now:   ema_dir = "BUY"
        elif ef_now < es_now: ema_dir = "SELL"
        else:                 ema_dir = "NONE"

        fresh_cross = (
            (ef_now > es_now and ef_prv <= es_prv) or
            (ef_now < es_now and ef_prv >= es_prv)
        )

        # ── LAYER 2: Sweep + Reclaim Detection ───────────────────────────────
        sweep_dir, sweep_info = self._detect_sweep(
            m15_c, m15_h, m15_l, price, PIP, atr_pip, DP)
        levels.update(sweep_info)

        # ── Determine signal direction ────────────────────────────────────────
        # Priority: sweep > EMA alignment
        if sweep_dir != "NONE":
            direction = sweep_dir
            score     = 4  # base: sweep confirmed
            reasons.append(f"✅ SWEEP {direction}: {sweep_info.get('sweep_detail','')}")
        elif ema_dir != "NONE":
            direction = ema_dir
            score     = 1  # base: EMA only
            if fresh_cross:
                score = 3
                reasons.append(f"✅ EMA FRESH CROSS {direction} EMA{fast_p}/{slow_p}")
            else:
                reasons.append(f"✅ EMA ALIGNED {direction} (+1)")
        else:
            return 0, "NONE", "No EMA direction", levels, 0

        # ── Apply regime rules ────────────────────────────────────────────────
        regime_block = False
        ranging_size_mult = 1.0

        if regime == "TRENDING_UP":
            if direction == "SELL":
                regime_block = True
                reasons.append("🚫 TRENDING_UP — SELL blocked (H4 bullish)")
            else:
                score += 2
                reasons.append("✅ TRENDING_UP — BUY with trend (+2)")

        elif regime == "TRENDING_DOWN":
            if direction == "BUY":
                regime_block = True
                reasons.append("🚫 TRENDING_DOWN — BUY blocked (H4 bearish)")
            else:
                score += 2
                reasons.append("✅ TRENDING_DOWN — SELL with trend (+2)")

        elif regime == "RANGING":
            # In ranging: only trade near extremes
            near_extreme, extreme_info = self._near_range_extreme(
                m15_c, m15_h, m15_l, price, direction, PIP, DP,
                int(settings.get("range_lookback_bars", 80)))
            levels["range_extreme"] = extreme_info
            if not near_extreme:
                return 0, "NONE", (f"RANGING — price in mid-range, too risky | "
                                   f"{extreme_info}"), levels, 0
            # Reduce position size in ranging market
            ranging_size_mult = RANGING_SIZE_MULT
            score += 1
            reasons.append(f"⚠️ RANGING — near extreme, reduced size ×{ranging_size_mult} | {extreme_info}")

        if regime_block:
            levels["signal_blockers"] = [reasons[-1]]
            return score, "NONE", " | ".join(reasons), levels, 0

        # ── H1 trend confirmation (bonus +1) ─────────────────────────────────
        h1_info = self._h1_trend(instrument, int(settings.get("h1_ema_period",21)), DP)
        h1_trend    = h1_info.get("h1_trend","UNKNOWN")
        h1_aligned  = (h1_trend=="BULLISH" and direction=="BUY") or \
                      (h1_trend=="BEARISH" and direction=="SELL")
        h1_neutral  = h1_trend in ("UNKNOWN","FLAT")
        h1_opposite = not h1_aligned and not h1_neutral

        levels["h1_trend"]   = h1_trend
        levels["h1_aligned"] = h1_aligned
        h1_relation = "aligned" if h1_aligned else ("neutral" if h1_neutral else "opposite")
        levels["h1_relation"] = h1_relation

        # H1 opposite in score_aware mode: block only for low scores
        h1_mode = settings.get("h1_filter_mode","score_aware")
        if h1_opposite:
            if h1_mode == "strict" or score < 5:
                reasons.append(f"🚫 H1 {h1_trend} OPPOSITE — blocked")
                levels["signal_blockers"] = [f"H1 {h1_trend} opposite"]
                return score, "NONE", " | ".join(reasons), levels, 0
            reasons.append(f"⚠️ H1 {h1_trend} opposite but score={score} — allowed")
        elif h1_aligned:
            score += 1
            reasons.append(f"✅ H1 {h1_trend} aligned (+1)")
        else:
            reasons.append(f"➡️ H1 {h1_trend} neutral")

        # ── SL / TP ───────────────────────────────────────────────────────────
        pair_sl_tp = settings.get("pair_sl_tp",{})
        pair_cfg   = pair_sl_tp.get(instrument,{})
        sl_pips    = int(pair_cfg.get("sl_pips", 15))
        tp_pips    = int(pair_cfg.get("tp_pips", 25))
        pip_val    = float(pair_cfg.get("pip_value_usd", 10.0))
        pip_unit   = pip_val / 100_000

        sl_price_dist = round(sl_pips * PIP, DP+2)
        tp_price_dist = round(tp_pips * PIP, DP+2)
        rr_ratio      = round(tp_pips / sl_pips, 2)

        min_rr = float(settings.get("min_rr_ratio", 1.3))
        blockers = []
        if rr_ratio < min_rr:
            blockers.append(f"RR {rr_ratio} < {min_rr}")

        levels.update({
            "score": score, "setup": "Cable Smart v3.0",
            "entry": round(price, DP),
            "sl_price_dist": sl_price_dist, "tp_price_dist": tp_price_dist,
            "sl_pips": sl_pips, "tp_pips": tp_pips, "rr_ratio": rr_ratio,
            "sl_usd_rec": round(sl_pips * pip_unit, DP+2),
            "tp_usd_rec": round(tp_pips * pip_unit, DP+2),
            "sl_risk_per_unit_usd": round(sl_pips * pip_unit, DP+2),
            "tp_reward_per_unit_usd": round(tp_pips * pip_unit, DP+2),
            "signal_blockers": blockers,
            "mandatory_checks": {
                "score_ok": score >= int(settings.get("signal_threshold", MIN_TRADE_SCORE)),
                "rr_ok": rr_ratio >= min_rr,
            },
            "quality_checks": {"tp_ok": True},
            "regime": regime,
            "ranging_size_mult": ranging_size_mult,
        })

        # ── Position sizing ───────────────────────────────────────────────────
        position_usd = score_to_position_usd(score, settings)
        if ranging_size_mult < 1.0:
            position_usd = max(20, int(position_usd * ranging_size_mult))
        levels["position_usd"] = position_usd

        reasons.append(f"SL={sl_pips}p TP={tp_pips}p RR=1:{rr_ratio} "
                       f"Score={score}/6 $${position_usd}")
        if blockers:
            reasons.append("BLOCKED: " + " | ".join(blockers))

        details = " | ".join(reasons)
        thr = int(settings.get("signal_threshold", MIN_TRADE_SCORE))

        if blockers:
            log.info("BLOCKED %s dir=%s score=%d | %s", instrument, direction, score, blockers)
        elif score < thr:
            log.info("BELOW THRESHOLD %s dir=%s score=%d/%d", instrument, direction, score, thr)
        else:
            log.info("SIGNAL %s dir=%s score=%d/6 $%d regime=%s",
                     instrument, direction, score, position_usd, regime)

        return score, direction, details, levels, position_usd

    # ── LAYER 1: Market Regime Detection ──────────────────────────────────────

    def _detect_regime(self, instrument: str, dp: int,
                       settings: dict) -> tuple:
        """
        Detect TRENDING_UP / TRENDING_DOWN / RANGING using H4 candles.

        Method:
          - Compute H4 EMA20 and EMA50
          - If EMA20 > EMA50 AND slope of EMA20 > threshold → TRENDING_UP
          - If EMA20 < EMA50 AND slope of EMA20 < -threshold → TRENDING_DOWN
          - Otherwise → RANGING

        Real data insight: GBP/USD Apr-Jun 2026 was RANGING (1.330-1.365).
        All SELL losses happened because bot sold in a ranging/up-biased market.
        """
        h4_c, h4_h, h4_l = self._candles(instrument, "H4", 60)
        info = {"h4_ema20": 0, "h4_ema50": 0, "h4_slope": 0,
                "h4_price": 0, "h4_trend": "UNKNOWN"}

        if len(h4_c) < 52:
            return "RANGING", info  # safe default

        ema20 = _ema(h4_c, H4_EMA_FAST)
        ema50 = _ema(h4_c, H4_EMA_SLOW)
        e20_now = ema20[-1]; e20_prv = ema20[-4]  # slope over 4 bars
        e50_now = ema50[-1]
        h4_px   = h4_c[-1]

        slope = (e20_now - e20_prv) / 4  # price change per bar
        thresh = float(settings.get("h4_slope_threshold", H4_SLOPE_THRESHOLD))

        info = {
            "h4_ema20":  round(e20_now, dp),
            "h4_ema50":  round(e50_now, dp),
            "h4_slope":  round(slope, 7),
            "h4_price":  round(h4_px, dp),
        }

        if e20_now > e50_now and slope > thresh and h4_px > e50_now:
            info["h4_trend"] = "TRENDING_UP"
            return "TRENDING_UP", info
        elif e20_now < e50_now and slope < -thresh and h4_px < e50_now:
            info["h4_trend"] = "TRENDING_DOWN"
            return "TRENDING_DOWN", info
        else:
            info["h4_trend"] = "RANGING"
            return "RANGING", info

    # ── LAYER 2: Sweep + Reclaim Entry ────────────────────────────────────────

    def _detect_sweep(self, closes, highs, lows, price, pip, atr_pip,
                      dp: int) -> tuple:
        """
        Detect liquidity sweep + reclaim pattern.

        FOR BUY (most important based on real data):
          1. Price sweeps BELOW the 20-bar low (takes out sell stops)
          2. Then the CURRENT bar closes BACK ABOVE that low
          3. This is the "reclaim" — shorts are trapped, price will rise
          = Enter BUY

        FOR SELL:
          1. Price sweeps ABOVE the 20-bar high
          2. Current bar closes BACK BELOW that high
          = Enter SELL

        This is the core fix: old bot entered at EMA cross → got swept.
        New bot enters AFTER the sweep, on the reclaim.

        Returns: (direction, info_dict)
        """
        if len(closes) < 25:
            return "NONE", {}

        # Use last 20 bars EXCLUDING current bar for S/R levels
        lookback_highs = highs[-21:-1]
        lookback_lows  = lows[-21:-1]
        recent_high    = max(lookback_highs)
        recent_low     = min(lookback_lows)
        current_low    = lows[-1]
        current_high   = highs[-1]
        current_close  = closes[-1]

        range_pips = (recent_high - recent_low) / pip
        info = {
            "sweep_high": round(recent_high, dp),
            "sweep_low":  round(recent_low,  dp),
            "range_pips": round(range_pips, 1),
        }

        # Minimum sweep depth: at least 3 pips below/above the level
        min_sweep = 3.0

        # BUY sweep: current bar went below recent_low, then closed above it
        swept_below = current_low < recent_low - (min_sweep * pip)
        reclaimed   = current_close > recent_low

        if swept_below and reclaimed:
            sweep_depth = (recent_low - current_low) / pip
            info["sweep_detail"] = f"swept {sweep_depth:.1f}p below {recent_low:.{dp}f}, reclaimed"
            info["sweep_direction"] = "BUY"
            return "BUY", info

        # SELL sweep: current bar went above recent_high, then closed below it
        swept_above = current_high > recent_high + (min_sweep * pip)
        rejected    = current_close < recent_high

        if swept_above and rejected:
            sweep_depth = (current_high - recent_high) / pip
            info["sweep_detail"] = f"swept {sweep_depth:.1f}p above {recent_high:.{dp}f}, rejected"
            info["sweep_direction"] = "SELL"
            return "SELL", info

        info["sweep_detail"] = f"no sweep (range={range_pips:.1f}p HL={recent_high:.{dp}f}/{recent_low:.{dp}f})"
        info["sweep_direction"] = "NONE"
        return "NONE", info

    # ── Range Extreme Check ───────────────────────────────────────────────────

    def _near_range_extreme(self, closes, highs, lows, price, direction,
                            pip, dp, lookback: int = 80) -> tuple:
        """
        In ranging market, only trade near support (BUY) or resistance (SELL).
        'Near' = within 20% of the range from the extreme.

        This prevents buying at mid-range or selling at mid-range
        where risk/reward is poor.
        """
        if len(closes) < lookback:
            lookback = len(closes)

        range_high = max(highs[-lookback:])
        range_low  = min(lows[-lookback:])
        range_size = range_high - range_low
        range_pips = range_size / pip

        if range_pips < 20:
            return False, f"range too narrow ({range_pips:.0f}p)"

        zone_pct = 0.25  # within 25% of extreme
        near_support    = price <= range_low  + (range_size * zone_pct)
        near_resistance = price >= range_high - (range_size * zone_pct)

        info = (f"range={range_pips:.0f}p "
                f"[{range_low:.{dp}f}–{range_high:.{dp}f}] "
                f"price={price:.{dp}f} "
                f"near_sup={near_support} near_res={near_resistance}")

        if direction == "BUY"  and near_support:    return True,  info
        if direction == "SELL" and near_resistance:  return True,  info
        return False, info

    # ── H1 trend ──────────────────────────────────────────────────────────────

    def _h1_trend(self, instrument: str, period: int = 21, dp: int = 5) -> dict:
        try:
            closes, _, _ = self._candles(instrument, "H1", 40)
            if len(closes) < period+2:
                return {"h1_trend": "UNKNOWN", "h1_ema_now": None}
            ema = _ema(closes[:-1], period)
            e   = ema[-1]; p = closes[-1]
            trend = "BULLISH" if p>e else "BEARISH" if p<e else "FLAT"
            return {"h1_trend": trend, "h1_ema_now": round(e, dp)}
        except Exception as exc:
            log.warning("H1 trend error: %s", exc)
            return {"h1_trend": "UNKNOWN", "h1_ema_now": None}

    # ── Data fetchers ─────────────────────────────────────────────────────────

    def _candles(self, instrument: str, granularity: str,
                 count: int = 60) -> tuple:
        url = f"{self.base_url}/v3/instruments/{instrument}/candles"
        prm = {"count": str(count), "granularity": granularity, "price": "M"}
        for _ in range(3):
            try:
                r = self.session.get(url, headers=self.headers,
                                     params=prm, timeout=15)
                if r.status_code == 200:
                    cc = [c for c in r.json().get("candles",[])
                          if c.get("complete")]
                    return ([float(c["mid"]["c"]) for c in cc],
                            [float(c["mid"]["h"]) for c in cc],
                            [float(c["mid"]["l"]) for c in cc])
                log.warning("Candles %s %s HTTP %s",
                            instrument, granularity, r.status_code)
            except Exception as e:
                log.warning("Candles error: %s", e)
            time.sleep(1)
        return [], [], []

    def _get_pip_value_usd(self, instrument: str, price: float,
                           pair_cfg: dict) -> float:
        override = float(pair_cfg.get("pip_value_usd", 0.0))
        if override > 0: return override
        pip_size = float(pair_cfg.get("pip_size", 0.0001))
        return (pip_size / price * 100_000) if price > 0 else 10.0
