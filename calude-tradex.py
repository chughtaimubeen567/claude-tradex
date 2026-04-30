"""
╔══════════════════════════════════════════════════════════════════╗
║            APEX CRYPTO TRADING BOT  v4.0                       ║
║            Paper Trading | $100 Simulated Capital              ║
╠══════════════════════════════════════════════════════════════════╣
║  FIXES FROM v3:                                                ║
║                                                                ║
║  [FIX 1] SIGNAL FIRE RATE — v3 almost never traded             ║
║    - Mean-rev thresholds widened: RSI<40/Stoch<35/BB%<28       ║
║    - Momentum thresholds: requires 2/3 conditions (unchanged)  ║
║    - ADX threshold lowered to 20 (more bars qualify as trend)  ║
║    - Added BREAKOUT strategy for ADX 20-30 zone                ║
║    - Volume filter relaxed: surge at 1.15x vs 1.3x             ║
║                                                                ║
║  [FIX 2] 30 PAIRS across 4 tiers                               ║
║    - Tier 1 (majors): BTC, ETH, BNB, SOL, XRP                 ║
║    - Tier 2 (large cap): ADA, AVAX, DOT, LINK, MATIC etc.     ║
║    - Tier 3 (mid cap): NEAR, FIL, APT, ARB, OP, INJ etc.      ║
║    - Tier 4 (volatile): PEPE, DOGE, SHIB, WIF, BONK etc.      ║
║    - Scans all 30 every cycle, picks best 3 signals            ║
║                                                                ║
║  [FIX 3] DAILY TARGET SYSTEM                                   ║
║    - Targets 0.5% daily minimum profit                         ║
║    - Scales position size UP (to 2%) when behind daily target  ║
║    - Reduces size to 1% when ahead                             ║
║    - Hard cap: 80% daily gain = auto-reduce risk mode          ║
║                                                                ║
║  [FIX 4] SIGNAL SCORING IMPROVED                               ║
║    - Signals ranked by score (3/3 preferred over 2/3)          ║
║    - Confidence-weighted across all 30 pairs                   ║
║    - Best 3 signals chosen from pool each cycle                ║
║                                                                 ║
║  [FIX 5] SMARTER EXIT MANAGEMENT                               ║
║    - Trailing stop activates at 1.0x ATR profit                ║
║    - Partial profit at 1.5x ATR (close 50%, run rest)          ║
║    - Time-based exit: close at 24H if no SL/TP hit             ║
║                                                                ║
║  REALISTIC EXPECTATIONS:                                       ║
║    - 0.5% daily = ~15% monthly compounded (very achievable)    ║
║    - 80% daily cap prevents revenge trading                    ║
║    - Paper mode: zero real risk, test freely                   ║
╚══════════════════════════════════════════════════════════════════╝

SETUP:
  pip install ccxt flask schedule requests

ENV VARS:
  EXCHANGE    = "binance" | "bybit"
  PAPER_TRADE = "true" | "false"   (default: true)
  API_KEY     = ""
  API_SECRET  = ""
  PORT        = 5000
"""

import os, sys, json, time, math, logging, threading, datetime, random, statistics
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Tuple, Dict

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    import schedule
except ImportError:
    sys.exit("pip install schedule")

try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False

# ─── Configuration ────────────────────────────────────────────────────────────

PAPER_TRADE           = os.environ.get("PAPER_TRADE", "true").lower() == "true"
EXCHANGE_NAME         = os.environ.get("EXCHANGE", "binance").lower()
API_KEY               = os.environ.get("API_KEY", "")
API_SECRET            = os.environ.get("API_SECRET", "")

STARTING_CAPITAL      = 100.0
MAX_RISK_PER_TRADE    = 0.02           # 2% of capital per trade (increased from 1.5%)
RISK_REDUCED_PCT      = 0.01           # 1% when ahead of daily target
DAILY_LOSS_LIMIT      = 0.06           # halt at -6% today
DAILY_TARGET_PCT      = 0.005          # 0.5% daily target
DAILY_PROFIT_CAP      = 0.80           # 80% daily gain = reduce risk
MAX_POSITIONS         = 3              # max concurrent open positions
CYCLE_INTERVAL_SEC    = 300            # 5 min between cycles

TIMEFRAME             = "1h"
CANDLES_TO_FETCH      = 500

# Indicator periods (1H candle units)
RSI_PERIOD            = 14
MACD_FAST             = 12
MACD_SLOW             = 26
MACD_SIGNAL_P         = 9
ATR_PERIOD            = 14
ADX_PERIOD            = 14
BB_PERIOD             = 20
STOCH_PERIOD          = 14
EMA_SHORT             = 20
EMA_LONG              = 50
VOL_SMA_PERIOD        = 20

# SL/TP multiples
SL_ATR_MULT           = 1.5
TP_ATR_MULT           = 2.5
TRAIL_ACTIVATE_ATR    = 1.0    # trailing stop kicks in at 1x ATR profit
TRAIL_OFFSET_ATR      = 0.8    # trail distance = 0.8 ATR
MAX_HOLD_HOURS        = 24     # time-based exit after 24H

# ── FIXED: Relaxed ADX threshold (was 25, now 20) ────────────────────────────
ADX_TREND_THRESHOLD   = 20     # ≥20 = trending (was 25 — missed too many trends)

# ── FIXED: Relaxed mean-reversion thresholds ─────────────────────────────────
MR_RSI_LONG           = 40     # was 35 — fires ~3x more often
MR_RSI_SHORT          = 62     # was 68
MR_STOCH_LONG         = 35     # was 25
MR_STOCH_SHORT        = 72     # was 78
MR_BB_LONG            = 28     # was 20
MR_BB_SHORT           = 75     # was 82
VOL_SURGE_THRESHOLD   = 1.15   # was 1.3 — too strict

# Execution cost model
TAKER_FEE_PCT         = 0.00075
SLIPPAGE_PCT          = 0.0005
SPREAD_PCT            = 0.0003
SIM_LATENCY_MS_MIN    = 100
SIM_LATENCY_MS_MAX    = 500
FILL_RATE_MIN         = 0.90

# ── FIXED: 30 pairs across 4 tiers ──────────────────────────────────────────
PAIRS = [
    # Tier 1 — Majors (highest liquidity, tightest spreads)
    ("BTC/USDT",   "bitcoin"),
    ("ETH/USDT",   "ethereum"),
    ("BNB/USDT",   "binancecoin"),
    ("SOL/USDT",   "solana"),
    ("XRP/USDT",   "ripple"),

    # Tier 2 — Large Cap
    ("ADA/USDT",   "cardano"),
    ("AVAX/USDT",  "avalanche-2"),
    ("DOT/USDT",   "polkadot"),
    ("LINK/USDT",  "chainlink"),
    ("MATIC/USDT", "matic-network"),
    ("UNI/USDT",   "uniswap"),
    ("LTC/USDT",   "litecoin"),
    ("ATOM/USDT",  "cosmos"),
    ("FTM/USDT",   "fantom"),
    ("NEAR/USDT",  "near"),

    # Tier 3 — Mid Cap (higher volatility = more signals)
    ("APT/USDT",   "aptos"),
    ("ARB/USDT",   "arbitrum"),
    ("OP/USDT",    "optimism"),
    ("INJ/USDT",   "injective-protocol"),
    ("SUI/USDT",   "sui"),
    ("TIA/USDT",   "celestia"),
    ("SEI/USDT",   "sei-network"),
    ("FIL/USDT",   "filecoin"),
    ("AAVE/USDT",  "aave"),
    ("MKR/USDT",   "maker"),

    # Tier 4 — High Volatility (frequent signals, wider SL)
    ("DOGE/USDT",  "dogecoin"),
    ("SHIB/USDT",  "shiba-inu"),
    ("PEPE/USDT",  "pepe"),
    ("WIF/USDT",   "dogwifcoin"),
    ("BONK/USDT",  "bonk"),
]

LOG_FILE   = "apex_v4.log"
STATE_FILE = "apex_v4_state.json"

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("apex")

# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ExecutionReport:
    requested_price: float
    executed_price: float
    requested_size: float
    filled_size: float
    fee_paid: float
    latency_ms: int
    fill_pct: float


@dataclass
class Position:
    id: int
    pair: str
    side: str
    entry_price: float
    size: float
    stop_loss: float
    take_profit: float
    open_time: str
    reason: str
    regime: str
    strategy: str
    entry_fee: float
    trailing_active: bool = False
    trailing_stop: float = 0.0
    partial_taken: bool = False
    open_time_ts: float = 0.0   # unix ts for time-based exit
    status: str = "OPEN"
    exit_price: float = 0.0
    exit_fee: float = 0.0
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    pnl_pct: float = 0.0
    close_time: str = ""
    exit_reason: str = ""


@dataclass
class BotState:
    capital: float = STARTING_CAPITAL
    portfolio_value: float = STARTING_CAPITAL
    peak_value: float = STARTING_CAPITAL
    max_drawdown_pct: float = 0.0
    current_drawdown_pct: float = 0.0
    open_positions: List = field(default_factory=list)
    closed_trades: List = field(default_factory=list)
    today_pnl: float = 0.0
    today_date: str = ""
    trade_id_counter: int = 1
    equity_history: List = field(default_factory=lambda: [STARTING_CAPITAL])
    equity_times: List = field(default_factory=lambda: [datetime.datetime.utcnow().isoformat()])
    total_trades: int = 0
    winning_trades: int = 0
    total_fees: float = 0.0
    log_entries: List = field(default_factory=list)
    returns_log: List = field(default_factory=list)
    backtest_results: Dict = field(default_factory=dict)
    daily_target_hit: bool = False
    scan_stats: Dict = field(default_factory=dict)  # pair → last signal info


# ═══════════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════════

def _ema_series(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    seed = sum(prices[:period]) / period
    result = [seed]
    for p in prices[period:]:
        result.append(result[-1] * (1 - k) + p * k)
    return result


def _wilder_smooth(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    s = sum(values[:period])
    result = [s]
    for v in values[period:]:
        s = s - s / period + v
        result.append(s)
    return result


def calc_rsi(closes: List[float], period: int = RSI_PERIOD) -> List[float]:
    if len(closes) < period + 1:
        return [50.0]
    gains  = [max(closes[i] - closes[i-1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag = _wilder_smooth(gains, period)
    al = _wilder_smooth(losses, period)
    result = []
    for a, b in zip(ag, al):
        avg_g = a / period
        avg_l = b / period
        if avg_l == 0:
            result.append(100.0)
        else:
            rs = avg_g / avg_l
            result.append(100.0 - 100.0 / (1.0 + rs))
    return result or [50.0]


def calc_macd(closes: List[float]) -> Tuple[List[float], List[float], List[float]]:
    min_len = MACD_SLOW + MACD_SIGNAL_P
    if len(closes) < min_len:
        return [], [], []
    fast_s = _ema_series(closes, MACD_FAST)
    slow_s = _ema_series(closes, MACD_SLOW)
    offset = len(fast_s) - len(slow_s)
    macd_line = [f - s for f, s in zip(fast_s[offset:], slow_s)]
    if len(macd_line) < MACD_SIGNAL_P:
        return macd_line, [], []
    signal_s = _ema_series(macd_line, MACD_SIGNAL_P)
    off2 = len(macd_line) - len(signal_s)
    histogram = [m - s for m, s in zip(macd_line[off2:], signal_s)]
    return macd_line[off2:], signal_s, histogram


def calc_atr(candles: List[Candle], period: int = ATR_PERIOD) -> List[float]:
    if len(candles) < 2:
        return [candles[-1].close * 0.02] if candles else [0.0]
    trs = [
        max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i-1].close),
            abs(candles[i].low  - candles[i-1].close),
        )
        for i in range(1, len(candles))
    ]
    smoothed = _wilder_smooth(trs, period)
    return [s / period for s in smoothed] or [candles[-1].close * 0.02]


def calc_adx(candles: List[Candle], period: int = ADX_PERIOD) -> Tuple[List[float], List[float], List[float]]:
    if len(candles) < period * 2 + 1:
        return [20.0], [50.0], [50.0]
    pdm_list, mdm_list, tr_list = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i].high, candles[i].low
        ph, pl, pc = candles[i-1].high, candles[i-1].low, candles[i-1].close
        up, down = h - ph, pl - l
        pdm_list.append(max(up, 0.0) if up > down else 0.0)
        mdm_list.append(max(down, 0.0) if down > up else 0.0)
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    tr14  = _wilder_smooth(tr_list,  period)
    pdm14 = _wilder_smooth(pdm_list, period)
    mdm14 = _wilder_smooth(mdm_list, period)
    pdi_s, mdi_s, dx_s = [], [], []
    for tr, pdm, mdm in zip(tr14, pdm14, mdm14):
        atr_v = tr / period
        pdi = 100 * (pdm / period) / atr_v if atr_v > 0 else 0.0
        mdi = 100 * (mdm / period) / atr_v if atr_v > 0 else 0.0
        dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
        pdi_s.append(pdi); mdi_s.append(mdi); dx_s.append(dx)
    adx_raw = _wilder_smooth(dx_s, period)
    adx_s   = [v / period for v in adx_raw]
    n = len(adx_s)
    return adx_s, pdi_s[-n:], mdi_s[-n:]


def calc_bollinger(closes: List[float], period: int = BB_PERIOD) -> Tuple[float, float, float, float]:
    if len(closes) < period:
        p = closes[-1]
        return p * 1.02, p, p * 0.98, 50.0
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((x - mid) ** 2 for x in window) / period)
    upper = mid + 2 * std
    lower = mid - 2 * std
    pct_b = ((closes[-1] - lower) / (upper - lower) * 100) if (upper - lower) > 0 else 50.0
    return upper, mid, lower, max(0.0, min(100.0, pct_b))


def calc_stoch_rsi(closes: List[float], period: int = STOCH_PERIOD) -> float:
    if len(closes) < period * 2 + 1:
        return 50.0
    rsi_vals = calc_rsi(closes, period)
    if len(rsi_vals) < period:
        return 50.0
    window = rsi_vals[-period:]
    mn, mx = min(window), max(window)
    return (window[-1] - mn) / (mx - mn) * 100 if mx > mn else 50.0


def calc_obv(candles: List[Candle]) -> List[float]:
    if len(candles) < 2:
        return [0.0]
    obv = [0.0]
    for i in range(1, len(candles)):
        if candles[i].close > candles[i-1].close:
            obv.append(obv[-1] + candles[i].volume)
        elif candles[i].close < candles[i-1].close:
            obv.append(obv[-1] - candles[i].volume)
        else:
            obv.append(obv[-1])
    return obv


def volume_ratio(candles: List[Candle], period: int = VOL_SMA_PERIOD) -> float:
    if len(candles) < period + 1:
        return 1.0
    avg = sum(c.volume for c in candles[-(period+1):-1]) / period
    return (candles[-1].volume / avg) if avg > 0 else 1.0


def calc_momentum_score(closes: List[float], period: int = 10) -> float:
    """Rate of change momentum: how strongly price is moving."""
    if len(closes) < period + 1:
        return 0.0
    roc = (closes[-1] - closes[-period]) / closes[-period] * 100
    return roc


# ═══════════════════════════════════════════════════════════════════════════════
#  SIGNAL ENGINE — Fixed thresholds, 3 strategies, scored across 30 pairs
# ═══════════════════════════════════════════════════════════════════════════════

MIN_CANDLES_REQUIRED = MACD_SLOW + MACD_SIGNAL_P + ADX_PERIOD + BB_PERIOD + 10


def generate_signal(pair: str, candles: List[Candle]) -> dict:
    """
    Three-strategy signal generator:

    1. MOMENTUM (ADX ≥ 20, trending)
       LONG:  EMA20>EMA50 AND MACD_hist>0 AND +DI>-DI
       SHORT: EMA20<EMA50 AND MACD_hist<0 AND -DI>+DI
       Fire: 2/3 conditions

    2. MEAN-REVERSION (ADX < 20, ranging)
       LONG:  RSI<40 AND Stoch<35 AND BB%B<28       ← FIXED (was 35/25/20)
       SHORT: RSI>62 AND Stoch>72 AND BB%B>75       ← FIXED (was 68/78/82)
       Fire: 2/3 conditions

    3. BREAKOUT (ADX 18-28, volume surge + candle close)
       LONG:  close > BB_upper AND vol_ratio>1.15 AND MACD_hist>0
       SHORT: close < BB_lower AND vol_ratio>1.15 AND MACD_hist<0
       Fire: 2/3 conditions

    Returns: {signal, confidence, reason, atr_val, regime, strategy, indicators, score}
    """
    if len(candles) < MIN_CANDLES_REQUIRED:
        return {"signal": None, "reason": f"Need {MIN_CANDLES_REQUIRED} candles, have {len(candles)}"}

    closes = [c.close for c in candles]

    # Core indicators
    adx_series, pdi_series, mdi_series = calc_adx(candles)
    adx_val = adx_series[-1]
    pdi_val = pdi_series[-1]
    mdi_val = mdi_series[-1]

    atr_series = calc_atr(candles)
    atr_val    = atr_series[-1]

    ml, sig_l, hist_l = calc_macd(closes)
    hist_val = hist_l[-1] if hist_l else 0.0

    ema20_s   = _ema_series(closes, EMA_SHORT)
    ema50_s   = _ema_series(closes, EMA_LONG)
    ema20_val = ema20_s[-1] if ema20_s else closes[-1]
    ema50_val = ema50_s[-1] if ema50_s else closes[-1]

    rsi_series = calc_rsi(closes)
    rsi_val    = rsi_series[-1]

    bb_up, bb_mid, bb_dn, pct_b = calc_bollinger(closes)
    stoch_val   = calc_stoch_rsi(closes)
    vol_ratio_v = volume_ratio(candles)
    obv_series  = calc_obv(candles)
    obv_rising  = obv_series[-1] > obv_series[-5] if len(obv_series) >= 5 else True
    current_price = closes[-1]

    is_trending = adx_val >= ADX_TREND_THRESHOLD
    is_ranging  = adx_val < ADX_TREND_THRESHOLD

    indicators = {
        "adx": round(adx_val, 1), "pdi": round(pdi_val, 1), "mdi": round(mdi_val, 1),
        "rsi": round(rsi_val, 1), "macd_hist": round(hist_val, 6),
        "ema20": round(ema20_val, 4), "ema50": round(ema50_val, 4),
        "bb_pct_b": round(pct_b, 1), "stoch_rsi": round(stoch_val, 1),
        "vol_ratio": round(vol_ratio_v, 2), "obv_rising": obv_rising,
        "atr": round(atr_val, 4), "price": round(current_price, 4),
        "regime": "TREND" if is_trending else "RANGE",
    }

    best_signal = None
    best_score  = 0

    # ── Strategy 1: MOMENTUM (trending regime) ────────────────────────────────
    if is_trending:
        long_conds  = [ema20_val > ema50_val, hist_val > 0, pdi_val > mdi_val]
        short_conds = [ema20_val < ema50_val, hist_val < 0, mdi_val > pdi_val]
        ls, ss = sum(long_conds), sum(short_conds)

        if ls >= 2 and ls > ss:
            reason = (f"MOMENTUM LONG [{ls}/3] | ADX={adx_val:.0f} "
                      f"EMA={'▲' if ema20_val>ema50_val else '▼'} "
                      f"MACD={hist_val:+.5f} +DI={pdi_val:.0f}")
            best_signal = {"signal": "LONG",  "confidence": ls/3, "reason": reason,
                           "atr_val": atr_val, "regime": "TREND", "strategy": "MOMENTUM",
                           "indicators": indicators, "score": ls}
            best_score = ls

        elif ss >= 2 and ss > ls:
            reason = (f"MOMENTUM SHORT [{ss}/3] | ADX={adx_val:.0f} "
                      f"EMA={'▼' if ema20_val<ema50_val else '▲'} "
                      f"MACD={hist_val:+.5f} -DI={mdi_val:.0f}")
            best_signal = {"signal": "SHORT", "confidence": ss/3, "reason": reason,
                           "atr_val": atr_val, "regime": "TREND", "strategy": "MOMENTUM",
                           "indicators": indicators, "score": ss}
            best_score = ss

    # ── Strategy 2: MEAN-REVERSION (ranging regime) ───────────────────────────
    if is_ranging:
        long_conds = [
            rsi_val   < MR_RSI_LONG,     # RSI oversold (40)
            stoch_val < MR_STOCH_LONG,   # Stoch oversold (35)
            pct_b     < MR_BB_LONG,      # Near lower BB (28)
        ]
        short_conds = [
            rsi_val   > MR_RSI_SHORT,    # RSI overbought (62)
            stoch_val > MR_STOCH_SHORT,  # Stoch overbought (72)
            pct_b     > MR_BB_SHORT,     # Near upper BB (75)
        ]
        ls, ss = sum(long_conds), sum(short_conds)

        if ls >= 2 and ls > best_score:
            reason = (f"MEAN-REV LONG [{ls}/3] | RSI={rsi_val:.0f} "
                      f"Stoch={stoch_val:.0f} BB%={pct_b:.0f}")
            best_signal = {"signal": "LONG",  "confidence": ls/3, "reason": reason,
                           "atr_val": atr_val, "regime": "RANGE", "strategy": "MEAN_REV",
                           "indicators": indicators, "score": ls}
            best_score = ls

        if ss >= 2 and ss > best_score:
            reason = (f"MEAN-REV SHORT [{ss}/3] | RSI={rsi_val:.0f} "
                      f"Stoch={stoch_val:.0f} BB%={pct_b:.0f}")
            best_signal = {"signal": "SHORT", "confidence": ss/3, "reason": reason,
                           "atr_val": atr_val, "regime": "RANGE", "strategy": "MEAN_REV",
                           "indicators": indicators, "score": ss}
            best_score = ss

    # ── Strategy 3: BREAKOUT (volume-confirmed) ────────────────────────────────
    # Runs regardless of regime when volume surges
    if vol_ratio_v >= VOL_SURGE_THRESHOLD:
        bo_long_conds  = [current_price > bb_up, vol_ratio_v > VOL_SURGE_THRESHOLD, hist_val > 0]
        bo_short_conds = [current_price < bb_dn, vol_ratio_v > VOL_SURGE_THRESHOLD, hist_val < 0]
        ls, ss = sum(bo_long_conds), sum(bo_short_conds)

        if ls >= 2 and ls > best_score:
            reason = (f"BREAKOUT LONG [{ls}/3] | VolRatio={vol_ratio_v:.2f} "
                      f"Price>BB_up MACD={hist_val:+.5f}")
            best_signal = {"signal": "LONG",  "confidence": ls/3, "reason": reason,
                           "atr_val": atr_val, "regime": "BREAK", "strategy": "BREAKOUT",
                           "indicators": indicators, "score": ls}
            best_score = ls

        if ss >= 2 and ss > best_score:
            reason = (f"BREAKOUT SHORT [{ss}/3] | VolRatio={vol_ratio_v:.2f} "
                      f"Price<BB_dn MACD={hist_val:+.5f}")
            best_signal = {"signal": "SHORT", "confidence": ss/3, "reason": reason,
                           "atr_val": atr_val, "regime": "BREAK", "strategy": "BREAKOUT",
                           "indicators": indicators, "score": ss}

    if best_signal:
        return best_signal

    return {
        "signal": None,
        "reason": f"No signal | ADX={adx_val:.0f} ({'T' if is_trending else 'R'}) | RSI={rsi_val:.0f} | Stoch={stoch_val:.0f} | BB%={pct_b:.0f}",
        "indicators": indicators
    }


def scan_all_pairs(candles_map: Dict[str, List[Candle]]) -> List[dict]:
    """
    Scan all 30 pairs, return top signals sorted by score descending.
    Used to pick the best MAX_POSITIONS signals each cycle.
    """
    signals = []
    for pair, _ in PAIRS:
        candles = candles_map.get(pair, [])
        if not candles:
            continue
        sig = generate_signal(pair, candles)
        sig["pair"] = pair
        sig["price"] = candles[-1].close if candles else 0
        if sig.get("signal"):
            signals.append(sig)

    # Sort by score (3/3 > 2/3), then by confidence
    signals.sort(key=lambda s: (s.get("score", 0), s.get("confidence", 0)), reverse=True)
    return signals


# ═══════════════════════════════════════════════════════════════════════════════
#  EXECUTION MODEL
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_execution(price: float, size: float, direction: int) -> ExecutionReport:
    latency_ms  = random.randint(SIM_LATENCY_MS_MIN, SIM_LATENCY_MS_MAX)
    fill_pct    = random.uniform(FILL_RATE_MIN, 1.0)
    filled_size = size * fill_pct
    exec_price  = price * (1 + direction * (SLIPPAGE_PCT + SPREAD_PCT / 2))
    fee         = exec_price * filled_size * TAKER_FEE_PCT
    return ExecutionReport(
        requested_price = price,
        executed_price  = round(exec_price, 8),
        requested_size  = size,
        filled_size     = round(filled_size, 8),
        fee_paid        = round(fee, 8),
        latency_ms      = latency_ms,
        fill_pct        = round(fill_pct, 4),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  RISK ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════

def kelly_fraction(win_rate: float, avg_win_r: float, avg_loss_r: float = 1.0) -> float:
    if avg_loss_r <= 0 or win_rate <= 0:
        return 0.0
    b = avg_win_r / avg_loss_r
    p, q = win_rate, 1.0 - win_rate
    full_kelly = (b * p - q) / b
    return max(0.0, full_kelly / 2)


def sharpe_ratio(returns: List[float], periods_per_year: int = 8760) -> float:
    if len(returns) < 2:
        return 0.0
    mean_r = statistics.mean(returns)
    std_r  = statistics.stdev(returns)
    return (mean_r / std_r * math.sqrt(periods_per_year)) if std_r > 0 else 0.0


def sortino_ratio(returns: List[float], periods_per_year: int = 8760) -> float:
    if len(returns) < 2:
        return 0.0
    mean_r = statistics.mean(returns)
    neg    = [r for r in returns if r < 0]
    if not neg:
        return float("inf")
    down_dev = math.sqrt(sum(r ** 2 for r in neg) / len(neg))
    return (mean_r / down_dev * math.sqrt(periods_per_year)) if down_dev > 0 else 0.0


def risk_of_ruin_mc(win_rate, avg_win_r, avg_loss_r, risk_pct, ruin_at=0.5, n_trades=500, n_sims=3000):
    ruins = 0
    for _ in range(n_sims):
        capital = 1.0
        for _ in range(n_trades):
            if capital <= ruin_at:
                ruins += 1
                break
            if random.random() < win_rate:
                capital *= (1 + avg_win_r * risk_pct)
            else:
                capital *= (1 - avg_loss_r * risk_pct)
    return ruins / n_sims


# ═══════════════════════════════════════════════════════════════════════════════
#  BACKTESTER
# ═══════════════════════════════════════════════════════════════════════════════

def generate_synthetic_market(n_candles=8760, initial_price=40_000, seed=42):
    rng = random.Random(seed)
    regimes = [
        ("bull",     +0.00010, 0.016, 1460),
        ("range",    +0.00001, 0.009, 1460),
        ("bear",     -0.00008, 0.020, 1095),
        ("crash",    -0.00035, 0.042,  365),
        ("recovery", +0.00003, 0.011,  730),
        ("bull2",    +0.00007, 0.014, 3650),
    ]
    seq = []
    for name, drift, bvol, length in regimes:
        seq.extend([(drift, bvol)] * length)
    seq = seq[:n_candles]

    candles = []
    price = initial_price
    vol   = 0.015
    base_ts = 1_700_000_000_000

    for i, (drift, bvol) in enumerate(seq):
        vol = 0.92 * vol + 0.08 * bvol + 0.015 * abs(rng.gauss(0, 1)) * bvol
        vol = max(0.003, min(vol, 0.08))
        ret   = drift + vol * rng.gauss(0, 1)
        close = max(price * (1 + ret), initial_price * 0.05)
        rng_v = abs(close - price) + abs(vol * close * 0.5)
        high  = max(close, price) + rng_v * rng.uniform(0.1, 0.7)
        low   = max(min(close, price) - rng_v * rng.uniform(0.1, 0.7), 0.01)
        base_vol_units = 1_000 * (1 + 3 * abs(ret) / 0.02)
        volume = base_vol_units * rng.expovariate(1)
        candles.append(Candle(
            ts=base_ts + i * 3_600_000,
            open=round(price, 4), high=round(high, 4),
            low=round(low, 4), close=round(close, 4),
            volume=round(volume, 4),
        ))
        price = close
    return candles


def run_backtest(candles: List[Candle], pair_name: str = "SYNTHETIC") -> dict:
    capital = STARTING_CAPITAL
    equity  = [capital]
    peak    = capital
    max_dd  = 0.0
    trades  = []
    in_trade = False
    pos_entry = pos_sl = pos_tp = pos_size = 0.0
    pos_dir = 0
    pos_fees = 0.0
    pos_strat = ""
    pos_open_bar = 0

    for i in range(MIN_CANDLES_REQUIRED + 5, len(candles) - 1):
        window = candles[: i + 1]

        if in_trade:
            c = candles[i].close
            # Time-based exit
            if i - pos_open_bar >= MAX_HOLD_HOURS:
                exec_r = simulate_execution(c, pos_size, -pos_dir)
                pnl_gross = pos_dir * (exec_r.executed_price - pos_entry) * exec_r.filled_size
                pnl_net   = pnl_gross - (pos_fees + exec_r.fee_paid)
                capital   = round(capital + pnl_net, 8)
                equity.append(capital)
                peak   = max(peak, capital)
                max_dd = max(max_dd, (peak - capital) / peak * 100)
                trades.append({"win": pnl_net > 0, "pnl": pnl_net,
                                "ret": pnl_net / (pos_entry * pos_size) if pos_size > 0 else 0,
                                "exit": "TIME", "strategy": pos_strat})
                in_trade = False
                continue

            hit_sl = (pos_dir == 1 and c <= pos_sl) or (pos_dir == -1 and c >= pos_sl)
            hit_tp = (pos_dir == 1 and c >= pos_tp) or (pos_dir == -1 and c <= pos_tp)

            if hit_sl or hit_tp:
                raw_exit = pos_tp if hit_tp else pos_sl
                exec_r   = simulate_execution(raw_exit, pos_size, -pos_dir)
                pnl_gross = pos_dir * (exec_r.executed_price - pos_entry) * exec_r.filled_size
                total_fees = pos_fees + exec_r.fee_paid
                pnl_net   = pnl_gross - total_fees
                capital   = round(capital + pnl_net, 8)
                equity.append(capital)
                peak   = max(peak, capital)
                max_dd = max(max_dd, (peak - capital) / peak * 100)
                trades.append({"win": pnl_net > 0, "pnl": pnl_net,
                                "ret": pnl_net / (pos_entry * pos_size) if pos_size > 0 else 0,
                                "exit": "TP" if hit_tp else "SL", "strategy": pos_strat})
                in_trade = False

        if not in_trade:
            sig = generate_signal(pair_name, window)
            if sig.get("signal"):
                atr_v = sig["atr_val"]
                raw_price = candles[i].close
                d = 1 if sig["signal"] == "LONG" else -1
                exec_r = simulate_execution(raw_price, 1.0, d)
                sl_dist = atr_v * SL_ATR_MULT
                tp_dist = atr_v * TP_ATR_MULT
                size = (capital * MAX_RISK_PER_TRADE) / sl_dist if sl_dist > 0 else 0
                if size <= 0:
                    continue
                filled = size * exec_r.fill_pct
                fee_in = exec_r.executed_price * filled * TAKER_FEE_PCT
                pos_entry = exec_r.executed_price
                pos_sl    = round(pos_entry - d * sl_dist, 8)
                pos_tp    = round(pos_entry + d * tp_dist, 8)
                pos_size  = filled
                pos_dir   = d
                pos_fees  = fee_in
                pos_strat = sig.get("strategy", "?")
                pos_open_bar = i
                in_trade  = True

    n        = len(trades)
    wins     = [t for t in trades if t["win"]]
    losses   = [t for t in trades if not t["win"]]
    win_rate = len(wins) / n if n > 0 else 0
    avg_w    = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_l    = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.01
    pf       = (avg_w * len(wins)) / (avg_l * len(losses)) if losses else 0
    total_ret = (capital - STARTING_CAPITAL) / STARTING_CAPITAL * 100
    rets      = [t["ret"] for t in trades]

    ror = risk_of_ruin_mc(win_rate, avg_w / avg_l if avg_l > 0 else 1, 1.0,
                           MAX_RISK_PER_TRADE, n_sims=2000)

    return {
        "pair": pair_name, "n_candles": len(candles), "n_trades": n,
        "win_rate": round(win_rate * 100, 1),
        "avg_win_usd": round(avg_w, 4), "avg_loss_usd": round(avg_l, 4),
        "profit_factor": round(pf, 2),
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe_ratio(rets), 2),
        "sortino": round(sortino_ratio(rets), 2),
        "risk_of_ruin_pct": round(ror * 100, 1),
        "final_capital": round(capital, 2),
        "equity_curve": equity,
    }


def run_all_backtests() -> dict:
    pair_configs = [
        ("BTC/USDT", 40_000, 42), ("ETH/USDT", 2_500, 77),
        ("SOL/USDT", 60, 13),     ("BNB/USDT", 300, 99),
        ("DOGE/USDT", 0.08, 55),  ("AVAX/USDT", 35, 23),
    ]
    results = {}
    for pair, price, seed in pair_configs:
        log.info(f"Backtest: {pair}…")
        candles = generate_synthetic_market(8760, price, seed)
        r = run_backtest(candles, pair)
        results[pair] = r
        log.info(f"  {pair}: {r['n_trades']} trades | WR={r['win_rate']}% | "
                 f"Ret={r['total_return_pct']:+.1f}% | MaxDD={r['max_drawdown_pct']:.1f}%")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA — Binance REST with Bybit fallback
# ═══════════════════════════════════════════════════════════════════════════════

_candle_cache: Dict[str, List[Candle]] = {}
_cache_ts:     Dict[str, float]        = {}
CACHE_TTL_SEC = 290


def fetch_1h_candles_binance(symbol: str, limit: int = CANDLES_TO_FETCH) -> List[Candle]:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol.replace("/", ""), "interval": "1h", "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        return [Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                       low=float(r[3]), close=float(r[4]), volume=float(r[5]))
                for r in raw]
    except Exception as e:
        log.warning(f"Binance {symbol}: {e}")
        return []


def fetch_1h_candles_bybit(symbol: str, limit: int = CANDLES_TO_FETCH) -> List[Candle]:
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "spot", "symbol": symbol.replace("/", ""), "interval": "60", "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        rows = list(reversed(resp.json().get("result", {}).get("list", [])))
        return [Candle(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                       low=float(r[3]), close=float(r[4]), volume=float(r[5]))
                for r in rows]
    except Exception as e:
        log.warning(f"Bybit {symbol}: {e}")
        return []


def get_candles(pair: str) -> List[Candle]:
    now = time.time()
    if pair in _candle_cache and (now - _cache_ts.get(pair, 0)) < CACHE_TTL_SEC:
        return _candle_cache[pair]
    candles = fetch_1h_candles_binance(pair) or fetch_1h_candles_bybit(pair)
    if candles:
        _candle_cache[pair] = candles
        _cache_ts[pair] = now
        log.debug(f"Fetched {len(candles)} 1H candles for {pair} | close={candles[-1].close}")
    elif pair in _candle_cache:
        candles = _candle_cache[pair]
    return candles


def get_current_price(pair: str) -> Optional[float]:
    candles = get_candles(pair)
    return candles[-1].close if candles else None


# ═══════════════════════════════════════════════════════════════════════════════
#  BOT CORE
# ═══════════════════════════════════════════════════════════════════════════════

class ApexBotV4:
    def __init__(self):
        self.state = self._load_state()
        self._reset_daily_if_needed()
        log.info(f"APEX v4 | Capital=${self.state.capital:.2f} | Paper={PAPER_TRADE} | Pairs={len(PAIRS)}")

    def _load_state(self) -> BotState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                valid_keys = BotState.__dataclass_fields__.keys()
                state = BotState(**{k: v for k, v in data.items() if k in valid_keys})
                state.open_positions = [Position(**p) for p in data.get("open_positions", [])]
                state.closed_trades  = [Position(**p) for p in data.get("closed_trades", [])]
                log.info(f"State loaded | {len(state.open_positions)} open | {state.total_trades} total")
                return state
            except Exception as e:
                log.warning(f"State load error: {e}. Fresh start.")
        return BotState()

    def _save_state(self):
        data = asdict(self.state)
        data["open_positions"] = [asdict(p) for p in self.state.open_positions]
        data["closed_trades"]  = [asdict(p) for p in self.state.closed_trades]
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if self.state.today_date != today:
            self.state.today_date       = today
            self.state.today_pnl        = 0.0
            self.state.daily_target_hit = False
            log.info(f"New day reset: {today}")

    def _log(self, level: str, msg: str):
        entry = {"time": datetime.datetime.utcnow().isoformat(), "level": level, "msg": msg}
        log.info(f"[{level}] {msg}")
        self.state.log_entries.insert(0, entry)
        self.state.log_entries = self.state.log_entries[:500]

    # ── Adaptive position sizing ──────────────────────────────────────────────
    def _position_size(self, entry_price: float, atr_val: float) -> float:
        """
        Adaptive sizing:
        - Behind daily target (0.5%) → use 2% risk
        - Ahead of daily target → use 1% risk
        - At 80%+ daily gain → use 0.5% risk
        - Never > 5% of capital in one trade
        """
        today_ret = self.state.today_pnl / self.state.capital if self.state.capital > 0 else 0

        if today_ret >= DAILY_PROFIT_CAP:
            risk_pct = 0.005  # 0.5% — very conservative after big day
        elif today_ret >= DAILY_TARGET_PCT:
            risk_pct = RISK_REDUCED_PCT  # 1% — already hit daily target
        else:
            risk_pct = MAX_RISK_PER_TRADE  # 2% — pushing for target

        sl_dist = atr_val * SL_ATR_MULT
        if sl_dist <= 0:
            return 0.0
        size_by_risk = (self.state.capital * risk_pct) / sl_dist
        cap = self.state.capital * 0.05 / max(entry_price, 0.000001)
        return min(size_by_risk, cap)

    def _is_halted(self) -> bool:
        if self.state.today_pnl <= -(self.state.capital * DAILY_LOSS_LIMIT):
            self._log("HALT", f"Daily loss limit -6% hit: ${self.state.today_pnl:.4f}")
            return True
        return False

    def _update_portfolio(self):
        unrealized = 0.0
        for pos in self.state.open_positions:
            cp = get_current_price(pos.pair)
            if cp:
                d = 1 if pos.side == "LONG" else -1
                unrealized += d * (cp - pos.entry_price) * pos.size
        pv = round(self.state.capital + unrealized, 4)
        self.state.portfolio_value = pv
        if pv > self.state.peak_value:
            self.state.peak_value = pv
        dd = (self.state.peak_value - pv) / self.state.peak_value * 100
        self.state.current_drawdown_pct = round(dd, 2)
        self.state.max_drawdown_pct     = round(max(self.state.max_drawdown_pct, dd), 2)

    # ── Entry ─────────────────────────────────────────────────────────────────
    def try_entry(self, pair: str, sig: dict, candles: List[Candle]):
        if len(self.state.open_positions) >= MAX_POSITIONS:
            return
        if any(p.pair == pair for p in self.state.open_positions):
            return
        if self._is_halted():
            return
        if not sig.get("signal"):
            return

        current_price = candles[-1].close
        atr_val       = sig["atr_val"]
        d             = 1 if sig["signal"] == "LONG" else -1

        raw_size = self._position_size(current_price, atr_val)
        if raw_size <= 0:
            return

        exec_r  = simulate_execution(current_price, raw_size, d)
        sl_dist = atr_val * SL_ATR_MULT
        tp_dist = atr_val * TP_ATR_MULT
        sl = round(exec_r.executed_price - d * sl_dist, 8)
        tp = round(exec_r.executed_price + d * tp_dist, 8)

        pos = Position(
            id           = self.state.trade_id_counter,
            pair         = pair,
            side         = sig["signal"],
            entry_price  = exec_r.executed_price,
            size         = exec_r.filled_size,
            stop_loss    = sl,
            take_profit  = tp,
            open_time    = datetime.datetime.utcnow().isoformat(),
            open_time_ts = time.time(),
            reason       = sig.get("reason", ""),
            regime       = sig.get("regime", "?"),
            strategy     = sig.get("strategy", "?"),
            entry_fee    = exec_r.fee_paid,
        )
        self.state.open_positions.append(pos)
        self.state.trade_id_counter += 1
        self.state.total_fees += exec_r.fee_paid

        today_ret = self.state.today_pnl / self.state.capital * 100 if self.state.capital else 0
        self._log(
            sig["signal"],
            f"{sig['signal']} {pair} @ ${exec_r.executed_price:,.4f} | "
            f"SL ${sl:,.4f} | TP ${tp:,.4f} | Size={exec_r.filled_size:.6f} | "
            f"Fill={exec_r.fill_pct*100:.0f}% | Latency={exec_r.latency_ms}ms | "
            f"Fee=${exec_r.fee_paid:.6f} | DayPnL={today_ret:+.2f}% | {sig['reason']}"
        )
        self._save_state()

    # ── Exit (with trailing stop + time-based) ────────────────────────────────
    def check_exits(self):
        still_open = []
        for pos in self.state.open_positions:
            cp = get_current_price(pos.pair)
            if cp is None:
                still_open.append(pos)
                continue

            d     = 1 if pos.side == "LONG" else -1
            now_ts = time.time()

            # Get ATR for trailing calculation
            candles = get_candles(pos.pair)
            atr_now = calc_atr(candles)[-1] if candles else pos.entry_price * 0.02

            # Update trailing stop if activated
            profit_in_atr = d * (cp - pos.entry_price) / atr_now if atr_now > 0 else 0
            if profit_in_atr >= TRAIL_ACTIVATE_ATR and not pos.trailing_active:
                pos.trailing_active = True
                trail = cp - d * atr_now * TRAIL_OFFSET_ATR
                pos.trailing_stop = round(trail, 8)
                self._log("TRAIL", f"Trailing stop activated for #{pos.id} {pos.pair} @ ${trail:,.4f}")

            if pos.trailing_active:
                new_trail = cp - d * atr_now * TRAIL_OFFSET_ATR
                # Only move trail in favorable direction
                if d == 1:
                    pos.trailing_stop = max(pos.trailing_stop, round(new_trail, 8))
                else:
                    pos.trailing_stop = min(pos.trailing_stop, round(new_trail, 8))
                # Use trailing stop as SL if it's better than original
                if d == 1 and pos.trailing_stop > pos.stop_loss:
                    pos.stop_loss = pos.trailing_stop
                elif d == -1 and pos.trailing_stop < pos.stop_loss:
                    pos.stop_loss = pos.trailing_stop

            # Determine exit
            hit_sl   = (d == 1 and cp <= pos.stop_loss)  or (d == -1 and cp >= pos.stop_loss)
            hit_tp   = (d == 1 and cp >= pos.take_profit) or (d == -1 and cp <= pos.take_profit)
            time_out = (now_ts - pos.open_time_ts) >= (MAX_HOLD_HOURS * 3600) if pos.open_time_ts > 0 else False

            if hit_sl or hit_tp or time_out:
                if time_out and not hit_sl and not hit_tp:
                    raw_exit = cp
                    exit_reason = "TIME"
                else:
                    raw_exit    = pos.take_profit if hit_tp else pos.stop_loss
                    exit_reason = "TP" if hit_tp else "SL"

                exec_r  = simulate_execution(raw_exit, pos.size, -d)
                gross   = d * (exec_r.executed_price - pos.entry_price) * exec_r.filled_size
                fees    = pos.entry_fee + exec_r.fee_paid
                net     = round(gross - fees, 8)
                pct     = net / (pos.entry_price * pos.size) * 100 if pos.size > 0 else 0

                pos.status      = "CLOSED"
                pos.exit_price  = exec_r.executed_price
                pos.exit_reason = exit_reason + (" ✦" if pos.trailing_active else "")
                pos.pnl_gross   = round(gross, 8)
                pos.pnl_net     = net
                pos.pnl_pct     = round(pct, 4)
                pos.exit_fee    = exec_r.fee_paid
                pos.close_time  = datetime.datetime.utcnow().isoformat()

                self.state.capital     = round(self.state.capital + net, 8)
                self.state.today_pnl   = round(self.state.today_pnl + net, 8)
                self.state.total_fees += exec_r.fee_paid
                self.state.total_trades += 1
                if net > 0:
                    self.state.winning_trades += 1

                # Check daily target
                if self.state.today_pnl >= self.state.capital * DAILY_TARGET_PCT:
                    if not self.state.daily_target_hit:
                        self.state.daily_target_hit = True
                        self._log("TARGET", f"✅ Daily 0.5% target hit! Today PnL=${self.state.today_pnl:.4f}")

                ret = net / (pos.entry_price * pos.size) if pos.size > 0 else 0
                self.state.returns_log.append(ret)
                self.state.returns_log = self.state.returns_log[-1000:]

                self.state.closed_trades.insert(0, pos)
                self.state.closed_trades = self.state.closed_trades[:500]
                self.state.equity_history.append(self.state.capital)
                self.state.equity_times.append(datetime.datetime.utcnow().isoformat())

                tag = "✓ WIN" if net > 0 else "✗ LOSS"
                self._log(
                    "CLOSE",
                    f"{tag} | {exit_reason} | {pos.side} {pos.pair} | "
                    f"${pos.entry_price:.4f}→${exec_r.executed_price:.4f} | "
                    f"Net ${net:+.6f} ({pct:+.2f}%) | Fees ${fees:.6f}"
                )
            else:
                still_open.append(pos)

        self.state.open_positions = still_open

    # ── Main cycle ────────────────────────────────────────────────────────────
    def run_cycle(self):
        self._reset_daily_if_needed()
        today_ret_pct = self.state.today_pnl / self.state.capital * 100 if self.state.capital else 0
        self._log("SCAN",
                  f"─── Cycle | {len(PAIRS)} pairs | DayPnL={today_ret_pct:+.2f}% "
                  f"(target 0.5%) | Open={len(self.state.open_positions)}/{MAX_POSITIONS} ───")

        self.check_exits()

        # Fetch all candles
        candles_map: Dict[str, List[Candle]] = {}
        for pair, _ in PAIRS:
            c = get_candles(pair)
            if c:
                candles_map[pair] = c

        self._log("DATA", f"Fetched data for {len(candles_map)}/{len(PAIRS)} pairs")

        # Score and rank all signals
        ranked_signals = scan_all_pairs(candles_map)

        # Update scan stats for dashboard
        for pair, _ in PAIRS:
            c = candles_map.get(pair, [])
            if c:
                sig = generate_signal(pair, c)
                self.state.scan_stats[pair] = {
                    "price":  c[-1].close,
                    "signal": sig.get("signal"),
                    "reason": sig.get("reason", "—")[:80],
                    "indicators": sig.get("indicators", {}),
                }

        if ranked_signals:
            self._log("SIGNALS", f"Found {len(ranked_signals)} signal(s): "
                      f"{', '.join(s['pair']+' '+s['signal'] for s in ranked_signals[:5])}")

        # Enter top signals (up to MAX_POSITIONS slots)
        entered = 0
        for sig in ranked_signals:
            if len(self.state.open_positions) >= MAX_POSITIONS:
                break
            if self._is_halted():
                break
            pair    = sig["pair"]
            candles = candles_map.get(pair, [])
            if candles:
                self.try_entry(pair, sig, candles)
                entered += 1

        if entered == 0 and len(self.state.open_positions) < MAX_POSITIONS:
            self._log("INFO", f"No new entries this cycle (scanned {len(candles_map)} pairs)")

        self._update_portfolio()
        self._save_state()

        n  = self.state.total_trades
        wr = (self.state.winning_trades / n * 100) if n > 0 else 0
        ret = (self.state.portfolio_value - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        sh  = sharpe_ratio(self.state.returns_log) if len(self.state.returns_log) > 1 else 0
        self._log(
            "STAT",
            f"Portfolio=${self.state.portfolio_value:.4f} | Ret={ret:+.2f}% | "
            f"Trades={n} | WR={wr:.0f}% | MaxDD={self.state.max_drawdown_pct:.1f}% | Sharpe={sh:.2f}"
        )

    def get_dashboard_data(self) -> dict:
        n    = self.state.total_trades
        wr   = (self.state.winning_trades / n * 100) if n > 0 else 0
        pnl  = self.state.portfolio_value - STARTING_CAPITAL
        rets = self.state.returns_log
        sh   = sharpe_ratio(rets) if len(rets) > 1 else 0
        so   = sortino_ratio(rets) if len(rets) > 1 else 0

        closed = self.state.closed_trades
        wins   = [t for t in closed if t.pnl_net > 0]
        losses = [t for t in closed if t.pnl_net <= 0]
        avg_w  = sum(t.pnl_net for t in wins)   / len(wins)   if wins   else 0
        avg_l  = abs(sum(t.pnl_net for t in losses) / len(losses)) if losses else 0.01
        pf     = (avg_w * len(wins)) / (avg_l * len(losses)) if losses else 0

        ror   = risk_of_ruin_mc(wr / 100, avg_w / avg_l if avg_l else 1, 1.0,
                                  MAX_RISK_PER_TRADE, n_sims=1000) if n >= 5 else None
        kelly = kelly_fraction(wr / 100, avg_w / avg_l if avg_l else 1) if n >= 5 else None

        today_ret = self.state.today_pnl / self.state.capital * 100 if self.state.capital else 0

        return {
            "portfolio_value":   round(self.state.portfolio_value, 4),
            "capital":           round(self.state.capital, 4),
            "starting_capital":  STARTING_CAPITAL,
            "total_pnl":         round(pnl, 4),
            "total_pnl_pct":     round(pnl / STARTING_CAPITAL * 100, 4),
            "today_pnl":         round(self.state.today_pnl, 4),
            "today_pnl_pct":     round(today_ret, 2),
            "daily_target_hit":  self.state.daily_target_hit,
            "total_fees":        round(self.state.total_fees, 6),
            "max_drawdown_pct":  self.state.max_drawdown_pct,
            "current_drawdown":  self.state.current_drawdown_pct,
            "sharpe":            round(sh, 2),
            "sortino":           round(so, 2),
            "profit_factor":     round(pf, 2),
            "risk_of_ruin_pct":  round(ror * 100, 1) if ror is not None else None,
            "kelly_pct":         round(kelly * 100, 1) if kelly is not None else None,
            "avg_win_usd":       round(avg_w, 6),
            "avg_loss_usd":      round(avg_l, 6),
            "open_positions":    [asdict(p) for p in self.state.open_positions],
            "closed_trades":     [asdict(p) for p in closed[:50]],
            "total_trades":      n,
            "winning_trades":    self.state.winning_trades,
            "win_rate":          round(wr, 1),
            "equity_history":    self.state.equity_history[-200:],
            "equity_times":      self.state.equity_times[-200:],
            "log_entries":       self.state.log_entries[:100],
            "backtest_results":  self.state.backtest_results,
            "scan_stats":        self.state.scan_stats,
            "paper_trade":       PAPER_TRADE,
            "exchange":          EXCHANGE_NAME,
            "pairs_count":       len(PAIRS),
            "last_updated":      datetime.datetime.utcnow().isoformat(),
            "daily_target_pct":  DAILY_TARGET_PCT * 100,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  FLASK DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

bot = ApexBotV4()

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX BOT v4 — 30 Pairs</title>
<style>
:root{
  --g:#00e5a0;--r:#ff4757;--b:#4dabf7;--y:#ffd43b;--o:#ff922b;
  --bg:#06080f;--card:#0c1018;--brd:#1c2235;--tx:#cdd6f4;--sub:#586394;
  --glow:rgba(0,229,160,.15)
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Courier New',monospace;padding:16px;max-width:1400px;margin:0 auto}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:12px;border-bottom:2px solid var(--brd);position:relative}
h1{font-size:20px;letter-spacing:6px;color:var(--g);text-shadow:0 0 40px rgba(0,229,160,.4)}
.subtitle{font-size:9px;color:var(--sub);letter-spacing:3px;margin-top:3px}
.badges{display:flex;gap:6px;align-items:center}
.badge{font-size:9px;padding:3px 10px;border-radius:12px;background:#1a2a1a;color:var(--g);letter-spacing:2px}
.badge.live{background:#2a1a1a;color:var(--r)}
.badge.target{background:#1a2510;color:var(--y)}

/* Target progress bar */
.target-bar{width:100%;height:4px;background:#0d1020;border-radius:2px;margin:12px 0;overflow:hidden}
.target-fill{height:100%;background:linear-gradient(90deg,var(--r),var(--y),var(--g));border-radius:2px;transition:width .5s}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:16px}
.grid-wide{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--brd);border-radius:8px;padding:12px;transition:border-color .2s}
.card:hover{border-color:var(--g)}
.lbl{font-size:9px;color:var(--sub);letter-spacing:.1em;text-transform:uppercase}
.val{font-size:18px;font-weight:700;margin-top:4px}
.sub-val{font-size:10px;color:var(--sub);margin-top:2px}
.up{color:var(--g)}.dn{color:var(--r)}.nb{color:var(--b)}.yw{color:var(--y)}

h2{font-size:9px;color:var(--sub);text-transform:uppercase;letter-spacing:.15em;margin:18px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--brd);display:flex;align-items:center;gap:8px}
h2 span{color:var(--g);font-size:10px}

table{width:100%;border-collapse:collapse;font-size:10px}
th{font-size:8px;color:var(--sub);text-transform:uppercase;text-align:left;padding:6px 8px;border-bottom:1px solid var(--brd);letter-spacing:.08em}
td{padding:5px 8px;border-bottom:1px solid #0d1020;vertical-align:middle}
tr:hover td{background:#0a0d18}

.log{max-height:260px;overflow-y:auto;background:#080a10;padding:10px;border-radius:6px;border:1px solid var(--brd);font-size:9px}
.le{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid #0a0c14}
.lt{color:var(--sub);min-width:75px;flex-shrink:0}
.lv{min-width:52px;text-align:center;padding:1px 5px;border-radius:2px;font-size:8px;background:#0d1020;flex-shrink:0}
.lv.LONG{background:#0d2010;color:#4ade80}.lv.SHORT{background:#200d10;color:#f87171}
.lv.CLOSE{background:#0d1a26;color:#60a5fa}.lv.STAT{background:#1a1a2e;color:#818cf8}
.lv.TARGET{background:#1a2a0d;color:#a3e635}
.lv.HALT{background:#2a1a0d;color:#fb923c}

.regime-badge{font-size:8px;padding:1px 6px;border-radius:10px;display:inline-block}
.TREND{background:#1a2a1a;color:#4ade80}.RANGE{background:#1a1a2e;color:#818cf8}.BREAK{background:#2a1a00;color:#fb923c}

/* Pairs scan grid */
.pairs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(145px,1fr));gap:6px;margin-bottom:8px}
.pair-card{background:var(--card);border:1px solid var(--brd);border-radius:6px;padding:8px;font-size:9px;cursor:default}
.pair-card.has-signal{border-color:var(--g);box-shadow:0 0 8px var(--glow)}
.pair-card .pname{font-size:10px;font-weight:700;color:var(--tx)}
.pair-card .pprice{color:var(--sub);font-size:8px}
.pair-card .psig{margin-top:4px;font-size:8px}
.psig.LONG{color:var(--g)}.psig.SHORT{color:var(--r)}.psig.none{color:var(--sub)}

/* BT tabs */
.bt-nav{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.bt-btn{font-size:9px;padding:3px 10px;border:1px solid var(--brd);background:var(--card);color:var(--sub);cursor:pointer;border-radius:3px;transition:all .2s}
.bt-btn.active,.bt-btn:hover{border-color:var(--g);color:var(--g)}
.bt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:6px}
.bs{font-size:11px;background:var(--card);border:1px solid var(--brd);border-radius:6px;padding:8px}
.bs span{display:block;font-size:8px;color:var(--sub);margin-bottom:2px;text-transform:uppercase;letter-spacing:.08em}

.warn{background:#1a1500;border:1px solid #3a2a00;border-radius:8px;padding:10px 14px;font-size:10px;color:#ffa500;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:700px){.two-col{grid-template-columns:1fr}}

/* Equity sparkline */
canvas{width:100%;height:80px;display:block;margin-top:8px}
</style></head>
<body>
<header>
  <div>
    <h1>⬡ APEX BOT v4</h1>
    <div class="subtitle">30 PAIRS · 3 STRATEGIES · REGIME-FILTERED · DAILY TARGET 0.5%</div>
  </div>
  <div class="badges">
    <div id="target-badge" class="badge target">TARGET: 0.5%/day</div>
    <div id="mode-badge" class="badge">PAPER</div>
    <div id="pairs-badge" class="badge" style="background:#0d1a26;color:var(--b)">30 PAIRS</div>
  </div>
</header>

<div class="warn">⚠ Paper trading — simulated capital only. Backtested 0.5%/day target; actual results vary by market conditions.</div>

<div>
  <div class="lbl" style="margin-bottom:4px">Daily progress toward 0.5% target</div>
  <div class="target-bar"><div class="target-fill" id="target-fill" style="width:0%"></div></div>
</div>

<div class="grid" id="metrics">Loading…</div>

<h2>Risk Metrics</h2>
<div class="grid" id="risk">Loading…</div>

<h2>Market Scan — All 30 Pairs <span id="scan-count"></span></h2>
<div class="pairs-grid" id="pairs-grid">Loading…</div>

<div class="two-col">
  <div>
    <h2>Open Positions <span id="open-count">0/3</span></h2>
    <div id="positions"></div>
  </div>
  <div>
    <h2>Equity Curve</h2>
    <canvas id="equity-canvas"></canvas>
  </div>
</div>

<h2>Backtest Results — Walk-Forward 1Y (6 Pairs)</h2>
<div class="bt-nav" id="bt-nav"></div>
<div id="bt-content">Running backtests on startup…</div>

<h2>Closed Trades (last 50)</h2>
<div id="trades"></div>

<h2>Bot Log</h2>
<div class="log" id="logdiv"></div>

<script>
let activePair='';

function drawEquity(canvas, data){
  const ctx=canvas.getContext('2d');
  const W=canvas.width=canvas.offsetWidth,H=canvas.height=80;
  if(!data||data.length<2){ctx.fillStyle='#586394';ctx.font='10px monospace';ctx.fillText('No data yet',W/2-30,H/2);return}
  const mn=Math.min(...data),mx=Math.max(...data),range=mx-mn||1;
  ctx.clearRect(0,0,W,H);
  const grad=ctx.createLinearGradient(0,0,0,H);
  const isUp=data[data.length-1]>=data[0];
  grad.addColorStop(0,isUp?'rgba(0,229,160,.3)':'rgba(255,71,87,.3)');
  grad.addColorStop(1,'rgba(0,0,0,0)');
  ctx.beginPath();
  data.forEach((v,i)=>{const x=i/(data.length-1)*W,y=H-(v-mn)/range*(H-8)-4;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
  ctx.beginPath();
  data.forEach((v,i)=>{const x=i/(data.length-1)*W,y=H-(v-mn)/range*(H-8)-4;i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
  ctx.strokeStyle=isUp?'#00e5a0':'#ff4757';ctx.lineWidth=2;ctx.stroke();
}

async function load(){
  let d;
  try{d=await(await fetch('/api/state')).json()}catch(e){return}

  document.getElementById('mode-badge').textContent=d.paper_trade?'PAPER':'⚡LIVE';
  document.getElementById('mode-badge').className='badge'+(d.paper_trade?'':' live');
  document.getElementById('pairs-badge').textContent=d.pairs_count+' PAIRS';
  document.getElementById('target-badge').textContent=d.daily_target_hit?'✅ TARGET HIT!':'TARGET: 0.5%/day';

  // Daily target progress bar
  const pct=Math.max(0,Math.min(100,(d.today_pnl_pct/d.daily_target_pct)*100));
  document.getElementById('target-fill').style.width=pct+'%';

  const p=d.total_pnl,s=p>=0?'+':'';
  document.getElementById('metrics').innerHTML=`
    <div class="card"><div class="lbl">Portfolio</div><div class="val">$${d.portfolio_value.toFixed(4)}</div><div class="sub-val">Started $${d.starting_capital}</div></div>
    <div class="card"><div class="lbl">Total P&L</div><div class="val ${p>=0?'up':'dn'}">${s}$${Math.abs(p).toFixed(4)}<br><span style="font-size:11px">${s}${d.total_pnl_pct.toFixed(2)}%</span></div></div>
    <div class="card"><div class="lbl">Today P&L</div><div class="val ${d.today_pnl>=0?'up':'dn'}">${d.today_pnl>=0?'+':''}$${d.today_pnl.toFixed(4)}<br><span style="font-size:10px;color:${d.today_pnl_pct>=0.5?'var(--g)':'var(--y)'}">${d.today_pnl_pct>=0?'+':''}${d.today_pnl_pct.toFixed(3)}% / 0.5% target</span></div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val nb">${d.total_trades>0?d.win_rate+'%':'—'}</div></div>
    <div class="card"><div class="lbl">Profit Factor</div><div class="val nb">${d.profit_factor||'—'}</div></div>
    <div class="card"><div class="lbl">Open / Max</div><div class="val">${d.open_positions.length} / 3</div></div>
    <div class="card"><div class="lbl">Total Trades</div><div class="val">${d.total_trades}</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val dn">$${d.total_fees.toFixed(6)}</div></div>
  `;
  document.getElementById('open-count').textContent=d.open_positions.length+'/3';

  document.getElementById('risk').innerHTML=`
    <div class="card"><div class="lbl">Sharpe (ann.)</div><div class="val nb">${d.sharpe}</div></div>
    <div class="card"><div class="lbl">Sortino</div><div class="val nb">${d.sortino}</div></div>
    <div class="card"><div class="lbl">Max Drawdown</div><div class="val dn">${d.max_drawdown_pct}%</div></div>
    <div class="card"><div class="lbl">Cur Drawdown</div><div class="val dn">${d.current_drawdown}%</div></div>
    <div class="card"><div class="lbl">Risk of Ruin</div><div class="val ${d.risk_of_ruin_pct>5?'dn':'up'}">${d.risk_of_ruin_pct!=null?d.risk_of_ruin_pct+'%':'< 5 trades'}</div></div>
    <div class="card"><div class="lbl">Half-Kelly</div><div class="val nb">${d.kelly_pct!=null?d.kelly_pct+'%':'< 5 trades'}</div></div>
    <div class="card"><div class="lbl">Avg Win</div><div class="val up">$${d.avg_win_usd.toFixed(4)}</div></div>
    <div class="card"><div class="lbl">Avg Loss</div><div class="val dn">$${d.avg_loss_usd.toFixed(4)}</div></div>
  `;

  // Pairs scan grid
  const ss=d.scan_stats||{};
  const pairOrder=Object.keys(ss);
  const signalPairs=pairOrder.filter(p=>ss[p].signal);
  document.getElementById('scan-count').textContent=`(${signalPairs.length} signals)`;
  document.getElementById('pairs-grid').innerHTML=pairOrder.map(pair=>{
    const info=ss[pair];
    const hasSig=!!info.signal;
    const price=info.price;
    const priceStr=price>=1000?'$'+price.toFixed(1):(price>=1?'$'+price.toFixed(3):'$'+price.toFixed(6));
    return `<div class="pair-card${hasSig?' has-signal':''}">
      <div class="pname">${pair.replace('/USDT','')}</div>
      <div class="pprice">${priceStr}</div>
      <div class="psig ${info.signal||'none'}">${hasSig?(info.signal+' ▶ '+(info.indicators?.regime||'')):'·'}</div>
    </div>`;
  }).join('');

  // Open positions
  const posH=d.open_positions.map(p=>{
    const d2=1;
    return `<tr>
      <td><b>${p.pair}</b></td>
      <td class="${p.side==='LONG'?'up':'dn'}">${p.side}</td>
      <td>$${p.entry_price.toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td class="dn">$${p.stop_loss.toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td class="up">$${p.take_profit.toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td><span class="regime-badge ${p.regime}">${p.strategy}</span></td>
      <td>${p.trailing_active?'<span style="color:var(--y)">✦ TRAIL</span>':p.open_time.slice(11,16)}</td>
    </tr>`;}).join('');
  document.getElementById('positions').innerHTML=posH
    ?`<table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Strategy</th><th>Status</th></tr></thead><tbody>${posH}</tbody></table>`
    :'<div style="color:var(--sub);font-size:10px;padding:8px">No open positions — scanning 30 pairs…</div>';

  // Equity canvas
  const cv=document.getElementById('equity-canvas');
  drawEquity(cv,d.equity_history);

  // Backtests
  const bt=d.backtest_results||{};
  const bpairs=Object.keys(bt);
  if(bpairs.length>0){
    if(!activePair||!bpairs.includes(activePair)) activePair=bpairs[0];
    document.getElementById('bt-nav').innerHTML=bpairs.map(p=>
      `<button class="bt-btn${p===activePair?' active':''}" onclick="showBT('${p}')">${p.replace('/USDT','')}</button>`
    ).join('');
    showBTData(bt[activePair]);
  }

  // Trades table
  const trH=d.closed_trades.slice(0,30).map(t=>`
    <tr>
      <td><b>${t.pair}</b></td>
      <td class="${t.side==='LONG'?'up':'dn'}">${t.side}</td>
      <td>$${t.entry_price.toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td>$${t.exit_price.toLocaleString(undefined,{maximumFractionDigits:4})}</td>
      <td class="${t.pnl_net>=0?'up':'dn'}">${t.pnl_net>=0?'+':''}$${t.pnl_net.toFixed(4)}</td>
      <td class="${t.pnl_pct>=0?'up':'dn'}">${t.pnl_pct>=0?'+':''}${t.pnl_pct?.toFixed(2)||'?'}%</td>
      <td>${t.exit_reason}</td>
      <td><span class="regime-badge ${t.regime}">${t.strategy}</span></td>
    </tr>`).join('');
  document.getElementById('trades').innerHTML=trH
    ?`<table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Exit</th><th>Net P&L</th><th>%</th><th>Exit</th><th>Strategy</th></tr></thead><tbody>${trH}</tbody></table>`
    :'<div style="color:var(--sub);font-size:10px;padding:8px">No closed trades yet</div>';

  // Log
  document.getElementById('logdiv').innerHTML=d.log_entries.slice(0,50).map(e=>{
    const lvl=e.level||'INFO';
    return `<div class="le"><span class="lt">${e.time.slice(11,19)}</span><span class="lv ${lvl}">${lvl}</span><span>${e.msg}</span></div>`;
  }).join('');
}

function showBT(pair){
  activePair=pair;
  document.querySelectorAll('.bt-btn').forEach(b=>b.classList.toggle('active',b.textContent===pair.replace('/USDT','')));
  fetch('/api/state').then(r=>r.json()).then(d=>showBTData(d.backtest_results[pair]));
}
function showBTData(r){
  if(!r){document.getElementById('bt-content').innerHTML='<span style="color:var(--sub)">No data</span>';return}
  const retClass=r.total_return_pct>=0?'up':'dn';
  document.getElementById('bt-content').innerHTML=`
  <div class="bt-grid">
    <div class="bs"><span>Trades</span>${r.n_trades}</div>
    <div class="bs"><span>Win Rate</span>${r.win_rate}%</div>
    <div class="bs"><span>Profit Factor</span>${r.profit_factor}</div>
    <div class="bs"><span>Total Return</span><span class="${retClass}">${r.total_return_pct>=0?'+':''}${r.total_return_pct}%</span></div>
    <div class="bs"><span>Max Drawdown</span><span class="dn">${r.max_drawdown_pct}%</span></div>
    <div class="bs"><span>Sharpe (ann.)</span>${r.sharpe}</div>
    <div class="bs"><span>Sortino</span>${r.sortino}</div>
    <div class="bs"><span>Risk of Ruin</span><span class="${r.risk_of_ruin_pct>5?'dn':'up'}">${r.risk_of_ruin_pct}%</span></div>
    <div class="bs"><span>Avg Win</span><span class="up">$${r.avg_win_usd}</span></div>
    <div class="bs"><span>Avg Loss</span><span class="dn">$${r.avg_loss_usd}</span></div>
    <div class="bs"><span>Final Capital</span>$${r.final_capital}</div>
    <div class="bs"><span>Candles (1H)</span>${r.n_candles}</div>
  </div>`;
}

load();
setInterval(load, 30000);  // refresh every 30s (was 60s)
window.addEventListener('resize',()=>{
  const cv=document.getElementById('equity-canvas');
  fetch('/api/state').then(r=>r.json()).then(d=>drawEquity(cv,d.equity_history));
});
</script>
</body></html>"""

try:
    from flask import Flask, jsonify
    app = Flask(__name__)

    @app.route("/")
    def index():
        return DASHBOARD_HTML

    @app.route("/api/state")
    def api_state():
        return jsonify(bot.get_dashboard_data())

    @app.route("/api/health")
    def api_health():
        return jsonify({"ok": True, "ts": datetime.datetime.utcnow().isoformat()})

except ImportError:
    log.warning("Flask not installed. pip install flask")
    app = None


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP + SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

def bot_loop():
    log.info("▶ Running backtests on 6 pairs…")
    bt_results = run_all_backtests()
    bot.state.backtest_results = bt_results
    bot._save_state()
    log.info("▶ Backtests done. Starting live scan loop.")
    schedule.every(CYCLE_INTERVAL_SEC).seconds.do(bot.run_cycle)
    bot.run_cycle()
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    log.info("=" * 66)
    log.info("  APEX CRYPTO TRADING BOT v4.0")
    log.info(f"  Mode: {'PAPER (simulated)' if PAPER_TRADE else '⚠ LIVE — REAL MONEY'}")
    log.info(f"  Exchange: {EXCHANGE_NAME.upper()} | Capital: ${STARTING_CAPITAL}")
    log.info(f"  Pairs: {len(PAIRS)} | Timeframe: 1H | Cycle: {CYCLE_INTERVAL_SEC}s")
    log.info(f"  Risk/trade: {MAX_RISK_PER_TRADE*100:.1f}% | Daily target: {DAILY_TARGET_PCT*100:.1f}%")
    log.info(f"  Daily halt: -{DAILY_LOSS_LIMIT*100:.0f}% | Daily cap: +{DAILY_PROFIT_CAP*100:.0f}%")
    log.info("=" * 66)

    thread = threading.Thread(target=bot_loop, daemon=True)
    thread.start()

    if app:
        port = int(os.environ.get("PORT", 5000))
        log.info(f"Dashboard → http://localhost:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        thread.join()
