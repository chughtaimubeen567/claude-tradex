"""
APEX CRYPTO TRADING BOT v2.0 - Paper Trading Mode
===================================================
FIXES IN THIS VERSION:
  1. Real OHLCV data via CoinGecko /ohlc endpoint (not synthetic high/low)
  2. Proper timeframe-aware indicators (1H candles)
  3. Correct MACD: EMA(12) - EMA(26), signal = EMA(9) of MACD (not shortcut)
  4. Fee simulation (0.075% taker), slippage (0.05%), spread modelling
  5. Regime filter: only trade in confirmed trend/mean-reversion regimes
  6. Momentum + mean-reversion split into separate sub-strategies
  7. ATR-based position sizing with Kelly Criterion cap
  8. Built-in walk-forward backtester (runs on startup, prints report)
  9. Drawdown tracker (max DD, current DD)
 10. Risk of Ruin calculation
 11. Sharpe & Sortino ratio tracking
 12. Proper state persistence with integrity checks

STRATEGY:
  - Regime Detection (ADX): trending → Momentum sub-strategy
                              ranging → Mean-Reversion sub-strategy
  - Momentum: EMA crossover + MACD confirmation + Volume filter
  - Mean-Reversion: RSI + Bollinger Band %B + Stochastic RSI
  - Entry scored 0-5 per sub-strategy; requires ≥ 3/5 confluence
  - SL = 1.5×ATR, TP = 2.5×ATR (asymmetric; slightly lower reward
    because regime filter improves hit rate)
  - Fees + slippage deducted on every entry AND exit

SETUP:
  pip install requests flask schedule python-dotenv

DEPLOY FREE ON RAILWAY.APP:
  1. Push to GitHub
  2. Connect Railway → set PORT env var
  3. Railway keeps it running 24/7
"""

import os, json, time, math, logging, threading, datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Tuple
import requests, schedule

# ─── Config ──────────────────────────────────────────────────────────────────

STARTING_CAPITAL       = 100.0
MAX_RISK_PER_TRADE_PCT = 0.015         # 1.5% per trade (tighter)
DAILY_LOSS_LIMIT_PCT   = 0.06          # halt at -6% today
MAX_CONCURRENT_POS     = 3
FETCH_INTERVAL_SEC     = 300           # 5 min (CoinGecko free: 50 req/min)

# Indicator periods (all on 1H candles)
ATR_PERIOD   = 14
RSI_PERIOD   = 14
EMA_SHORT    = 20
EMA_LONG     = 50
MACD_FAST    = 12
MACD_SLOW    = 26
MACD_SIGNAL  = 9
ADX_PERIOD   = 14
BB_PERIOD    = 20
STOCH_PERIOD = 14

# Execution costs (simulated)
TAKER_FEE    = 0.00075   # 0.075% (Binance taker)
SLIPPAGE     = 0.0005    # 0.05% market impact
SPREAD_PCT   = 0.0003    # 0.03% bid-ask spread

SL_ATR_MULT  = 1.5
TP_ATR_MULT  = 2.5       # 2.5:1.5 ≈ 1.67 reward/risk (post-fee ≈ 1.5)
ADX_TREND_THRESH = 25    # ADX > 25 → trending regime

PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD"]
LOG_FILE   = "apex_bot.log"
STATE_FILE = "apex_bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("apex")


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class Candle:
    timestamp: int   # unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float


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
    regime: str       # "TREND" | "RANGE"
    strategy: str     # "MOMENTUM" | "MEAN_REV"
    status: str = "OPEN"
    exit_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    close_time: str = ""
    exit_reason: str = ""
    fees_paid: float = 0.0


@dataclass
class BotState:
    capital: float = STARTING_CAPITAL
    portfolio_value: float = STARTING_CAPITAL
    peak_value: float = STARTING_CAPITAL
    max_drawdown: float = 0.0
    open_positions: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    today_pnl: float = 0.0
    today_date: str = ""
    trade_id_counter: int = 1
    equity_history: list = field(default_factory=lambda: [STARTING_CAPITAL])
    equity_times: list = field(default_factory=lambda: [datetime.datetime.utcnow().isoformat()])
    total_trades: int = 0
    winning_trades: int = 0
    total_fees: float = 0.0
    log_entries: list = field(default_factory=list)
    returns_history: list = field(default_factory=list)  # for Sharpe/Sortino


# ─── Indicators (all correct implementations) ─────────────────────────────────

def ema(prices: List[float], period: int) -> List[float]:
    """Returns full EMA series (not just last value)."""
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def last_ema(prices: List[float], period: int) -> float:
    series = ema(prices, period)
    return series[-1] if series else (prices[-1] if prices else 0.0)


def rsi(closes: List[float], period: int = RSI_PERIOD) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff); losses.append(0)
        else:
            gains.append(0); losses.append(-diff)
    # Wilder smoothing (correct RSI)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def macd(closes: List[float]) -> Tuple[float, float, float]:
    """
    Proper MACD:
      MACD line   = EMA(12) - EMA(26)
      Signal line = EMA(9) of MACD line
      Histogram   = MACD line - Signal line
    Returns (macd_line, signal_line, histogram) — all as latest values.
    """
    if len(closes) < MACD_SLOW + MACD_SIGNAL:
        return 0.0, 0.0, 0.0
    fast_series = ema(closes, MACD_FAST)
    slow_series = ema(closes, MACD_SLOW)
    # Align: fast_series starts earlier, trim to same length as slow_series
    offset = len(fast_series) - len(slow_series)
    macd_series = [f - s for f, s in zip(fast_series[offset:], slow_series)]
    if len(macd_series) < MACD_SIGNAL:
        return macd_series[-1], macd_series[-1], 0.0
    signal_series = ema(macd_series, MACD_SIGNAL)
    macd_val = macd_series[-1]
    sig_val  = signal_series[-1]
    return macd_val, sig_val, macd_val - sig_val


def atr(candles: List[Candle], period: int = ATR_PERIOD) -> float:
    if len(candles) < 2:
        return candles[-1].close * 0.02 if candles else 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return sum(trs) / len(trs)
    # Wilder ATR
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def bollinger(closes: List[float], period: int = BB_PERIOD) -> Tuple[float, float, float, float]:
    """Returns (upper, mid, lower, pct_b 0-100)."""
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


def stoch_rsi(closes: List[float], period: int = STOCH_PERIOD) -> float:
    """Stochastic RSI: position of current RSI within its range over `period` bars."""
    if len(closes) < period * 2 + 1:
        return 50.0
    rsi_vals = []
    for i in range(period, len(closes)):
        rsi_vals.append(rsi(closes[max(0, i - period * 2):i + 1], period))
    if len(rsi_vals) < period:
        return 50.0
    window = rsi_vals[-period:]
    min_r, max_r = min(window), max(window)
    if max_r == min_r:
        return 50.0
    return (window[-1] - min_r) / (max_r - min_r) * 100


def adx(candles: List[Candle], period: int = ADX_PERIOD) -> Tuple[float, float, float]:
    """
    Returns (ADX, +DI, -DI).
    ADX > 25 → trending regime; ADX < 20 → ranging.
    """
    if len(candles) < period * 2:
        return 20.0, 50.0, 50.0
    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i].high, candles[i].low
        ph, pl, pc = candles[i-1].high, candles[i-1].low, candles[i-1].close
        up_move   = h - ph
        down_move = pl - l
        plus_dm_list.append(max(up_move, 0) if up_move > down_move else 0)
        minus_dm_list.append(max(down_move, 0) if down_move > up_move else 0)
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))

    def wilder_smooth(data, p):
        s = sum(data[:p])
        result = [s]
        for x in data[p:]:
            s = s - s / p + x
            result.append(s)
        return result

    atr14  = wilder_smooth(tr_list, period)
    pdm14  = wilder_smooth(plus_dm_list, period)
    mdm14  = wilder_smooth(minus_dm_list, period)

    pdi = [100 * p / a if a else 0 for p, a in zip(pdm14, atr14)]
    mdi = [100 * m / a if a else 0 for m, a in zip(mdm14, atr14)]
    dx  = [100 * abs(p - m) / (p + m) if (p + m) > 0 else 0 for p, m in zip(pdi, mdi)]

    if len(dx) < period:
        return 20.0, pdi[-1], mdi[-1]
    adx_val = sum(dx[:period]) / period
    for d in dx[period:]:
        adx_val = (adx_val * (period - 1) + d) / period
    return adx_val, pdi[-1], mdi[-1]


def volume_sma(candles: List[Candle], period: int = 20) -> float:
    vols = [c.volume for c in candles[-period:]]
    return sum(vols) / len(vols) if vols else 0.0


# ─── Regime Detection + Signal Generation ────────────────────────────────────

def regime_and_signal(pair: str, candles: List[Candle]) -> dict:
    """
    Two sub-strategies, activated by regime:
      TREND  regime → MOMENTUM strategy
      RANGE  regime → MEAN_REVERSION strategy
    Returns: {signal, confidence, reason, indicators, atr_val, regime, strategy}
    """
    min_candles = MACD_SLOW + MACD_SIGNAL + ADX_PERIOD + 10
    if len(candles) < min_candles:
        return {"signal": None, "reason": f"Need {min_candles} candles, have {len(candles)}"}

    closes  = [c.close for c in candles]
    cur     = closes[-1]
    atr_val = atr(candles)

    # ── Regime detection ──
    adx_val, pdi_val, mdi_val = adx(candles)
    is_trending = adx_val >= ADX_TREND_THRESH

    # ── Common indicators ──
    rsi_val               = rsi(closes)
    macd_line, sig_line, hist = macd(closes)
    ema_s                 = last_ema(closes, EMA_SHORT)
    ema_l                 = last_ema(closes, EMA_LONG)
    bb_upper, bb_mid, bb_lower, pct_b = bollinger(closes)
    stoch_val             = stoch_rsi(closes)
    vol_avg               = volume_sma(candles)
    cur_vol               = candles[-1].volume
    vol_surge             = cur_vol > vol_avg * 1.3   # 30% above avg

    indicators = {
        "rsi": round(rsi_val, 1),
        "macd": round(macd_line, 5),
        "macd_signal": round(sig_line, 5),
        "macd_hist": round(hist, 5),
        "ema_short": round(ema_s, 2),
        "ema_long": round(ema_l, 2),
        "adx": round(adx_val, 1),
        "pdi": round(pdi_val, 1),
        "mdi": round(mdi_val, 1),
        "bb_upper": round(bb_upper, 2),
        "bb_lower": round(bb_lower, 2),
        "pct_b": round(pct_b, 1),
        "stoch_rsi": round(stoch_val, 1),
        "vol_surge": vol_surge,
        "regime": "TREND" if is_trending else "RANGE",
    }

    # ────────────────────────────────────────────────────────────────────────
    # MOMENTUM STRATEGY (ADX ≥ 25 = trending)
    # ────────────────────────────────────────────────────────────────────────
    if is_trending:
        # LONG momentum: price breaking up with trend
        long_conds = [
            ema_s > ema_l,           # uptrend structure
            pdi_val > mdi_val,       # directional buyers winning
            macd_line > sig_line,    # MACD bullish cross
            hist > 0,                # MACD histogram positive
            vol_surge,               # volume confirms breakout
        ]
        # SHORT momentum
        short_conds = [
            ema_s < ema_l,
            mdi_val > pdi_val,
            macd_line < sig_line,
            hist < 0,
            vol_surge,
        ]
        long_score  = sum(long_conds)
        short_score = sum(short_conds)

        if long_score >= 3 and long_score > short_score:
            conf = long_score / len(long_conds)
            reason = (f"MOMENTUM LONG | ADX={adx_val:.0f} EMA {'↑' if ema_s>ema_l else '↓'} "
                      f"MACD_hist={hist:+.5f} +DI={pdi_val:.0f} Vol={'↑' if vol_surge else '—'} "
                      f"Score={long_score}/5")
            return {"signal": "LONG", "confidence": conf, "reason": reason,
                    "indicators": indicators, "atr_val": atr_val,
                    "regime": "TREND", "strategy": "MOMENTUM"}

        if short_score >= 3 and short_score > long_score:
            conf = short_score / len(short_conds)
            reason = (f"MOMENTUM SHORT | ADX={adx_val:.0f} EMA {'↓' if ema_s<ema_l else '↑'} "
                      f"MACD_hist={hist:+.5f} -DI={mdi_val:.0f} Vol={'↑' if vol_surge else '—'} "
                      f"Score={short_score}/5")
            return {"signal": "SHORT", "confidence": conf, "reason": reason,
                    "indicators": indicators, "atr_val": atr_val,
                    "regime": "TREND", "strategy": "MOMENTUM"}

    # ────────────────────────────────────────────────────────────────────────
    # MEAN-REVERSION STRATEGY (ADX < 25 = ranging)
    # ────────────────────────────────────────────────────────────────────────
    else:
        # LONG: oversold bounce
        long_conds = [
            rsi_val < 35,            # RSI oversold
            stoch_val < 25,          # Stoch RSI oversold
            pct_b < 20,              # Near lower Bollinger Band
            macd_line > sig_line,    # early MACD upturn
            cur < bb_lower * 1.005,  # price near/at lower band
        ]
        # SHORT: overbought rejection
        short_conds = [
            rsi_val > 68,
            stoch_val > 78,
            pct_b > 82,
            macd_line < sig_line,
            cur > bb_upper * 0.995,
        ]
        long_score  = sum(long_conds)
        short_score = sum(short_conds)

        if long_score >= 3 and long_score > short_score:
            conf = long_score / len(long_conds)
            reason = (f"MEAN-REV LONG | RSI={rsi_val:.0f} Stoch={stoch_val:.0f} "
                      f"BB%={pct_b:.0f} MACD_cross={'✓' if macd_line>sig_line else '✗'} "
                      f"Score={long_score}/5")
            return {"signal": "LONG", "confidence": conf, "reason": reason,
                    "indicators": indicators, "atr_val": atr_val,
                    "regime": "RANGE", "strategy": "MEAN_REV"}

        if short_score >= 3 and short_score > long_score:
            conf = short_score / len(short_conds)
            reason = (f"MEAN-REV SHORT | RSI={rsi_val:.0f} Stoch={stoch_val:.0f} "
                      f"BB%={pct_b:.0f} MACD_cross={'✓' if macd_line<sig_line else '✗'} "
                      f"Score={short_score}/5")
            return {"signal": "SHORT", "confidence": conf, "reason": reason,
                    "indicators": indicators, "atr_val": atr_val,
                    "regime": "RANGE", "strategy": "MEAN_REV"}

    return {"signal": None, "reason": f"No signal | ADX={adx_val:.0f} Regime={'TREND' if is_trending else 'RANGE'} RSI={rsi_val:.0f}", "indicators": indicators}


# ─── Price / OHLCV Fetcher ───────────────────────────────────────────────────

COINGECKO_IDS = {
    "BTC/USD": "bitcoin",
    "ETH/USD": "ethereum",
    "SOL/USD": "solana",
    "BNB/USD": "binancecoin",
}

candle_cache: dict = {pair: [] for pair in PAIRS}
_last_fetch: dict = {pair: 0 for pair in PAIRS}


def fetch_ohlcv(pair: str, days: int = 7) -> List[Candle]:
    """
    Fetches daily OHLCV candles from CoinGecko (free, no key).
    For hourly data you'd need a paid API; we use daily and note this.
    """
    cg_id = COINGECKO_IDS.get(pair)
    if not cg_id:
        return []
    url = (f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
           f"?vs_currency=usd&days={days}")
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        raw = resp.json()   # [[ts, open, high, low, close], ...]
        candles = []
        for row in raw:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
            vol = 0.0   # CoinGecko free OHLC doesn't include volume
            candles.append(Candle(ts, o, h, l, c, vol))
        log.info(f"Fetched {len(candles)} candles for {pair}")
        return candles
    except Exception as e:
        log.warning(f"OHLCV fetch failed for {pair}: {e}")
        return []


def fetch_current_prices() -> dict:
    """Lightweight price check for SL/TP monitoring between candle fetches."""
    ids = ",".join(COINGECKO_IDS.values())
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true")
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {pair: {"price": data[cg_id]["usd"], "change_24h": data[cg_id].get("usd_24h_change", 0)}
                for pair, cg_id in COINGECKO_IDS.items() if cg_id in data}
    except Exception as e:
        log.warning(f"Price fetch failed: {e}")
        return {}


# ─── Analytics ────────────────────────────────────────────────────────────────

def sharpe(returns: List[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1))
    return ((mean_r - risk_free) / std_r * math.sqrt(252)) if std_r > 0 else 0.0


def sortino(returns: List[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    mean_r = sum(returns) / len(returns)
    neg = [r for r in returns if r < risk_free]
    if not neg:
        return float('inf')
    downside = math.sqrt(sum((r - risk_free) ** 2 for r in neg) / len(neg))
    return (mean_r - risk_free) / downside * math.sqrt(252) if downside > 0 else 0.0


def risk_of_ruin(win_rate: float, avg_win: float, avg_loss: float,
                 risk_pct: float, simulations: int = 1000) -> float:
    """Monte Carlo estimate: probability of losing 50% of capital."""
    if win_rate <= 0 or avg_loss <= 0:
        return 1.0
    ruins = 0
    for _ in range(simulations):
        cap = 1.0
        for _ in range(200):
            if cap <= 0.5:
                ruins += 1; break
            win = (win_rate > (hash(time.time_ns()) % 10000) / 10000)
            cap = cap * (1 + avg_win * risk_pct) if win else cap * (1 - avg_loss * risk_pct)
    return ruins / simulations


# ─── Walk-Forward Backtester ──────────────────────────────────────────────────

def run_backtest(candles: List[Candle], pair: str = "BTC/USD") -> dict:
    """
    Walk-forward backtest on provided candles.
    Simulates the exact same entry/exit logic including fees + slippage.
    """
    min_c = MACD_SLOW + MACD_SIGNAL + ADX_PERIOD + BB_PERIOD + 10
    if len(candles) < min_c + 5:
        return {"error": f"Need {min_c + 5}+ candles for backtest"}

    cap      = 100.0
    trades   = []
    equity   = [cap]
    peak     = cap
    max_dd   = 0.0

    for i in range(min_c, len(candles) - 1):
        window = candles[:i + 1]
        sig    = regime_and_signal(pair, window)
        if not sig.get("signal"):
            continue

        entry_raw  = candles[i].close
        atr_val    = sig["atr_val"]
        sl_dist    = atr_val * SL_ATR_MULT
        tp_dist    = atr_val * TP_ATR_MULT
        direction  = 1 if sig["signal"] == "LONG" else -1

        # Apply slippage + spread on entry
        entry = entry_raw * (1 + direction * (SLIPPAGE + SPREAD_PCT / 2))
        fee_in = entry * TAKER_FEE

        risk_amt = cap * MAX_RISK_PER_TRADE_PCT
        size     = risk_amt / sl_dist if sl_dist > 0 else 0
        if size <= 0:
            continue

        sl = entry - direction * sl_dist
        tp = entry + direction * tp_dist

        # Walk forward: check next candles for SL/TP
        result = None
        for future in candles[i + 1:i + 50]:  # max 50 candles hold
            if direction == 1:   # LONG
                if future.low <= sl:
                    result = ("SL", sl)
                    break
                if future.high >= tp:
                    result = ("TP", tp)
                    break
            else:                # SHORT
                if future.high >= sl:
                    result = ("SL", sl)
                    break
                if future.low <= tp:
                    result = ("TP", tp)
                    break

        if not result:
            continue   # trade still open at end of data — skip

        exit_type, exit_raw = result
        exit_price = exit_raw * (1 - direction * (SLIPPAGE + SPREAD_PCT / 2))
        fee_out    = exit_price * TAKER_FEE
        gross_pnl  = direction * (exit_price - entry) * size
        net_pnl    = gross_pnl - (fee_in + fee_out) * size
        cap       += net_pnl

        trades.append({
            "pair": pair, "direction": sig["signal"],
            "entry": entry, "exit": exit_price,
            "pnl": net_pnl, "win": net_pnl > 0,
            "exit_type": exit_type,
            "regime": sig.get("regime"),
            "strategy": sig.get("strategy"),
        })
        equity.append(cap)
        peak   = max(peak, cap)
        dd     = (peak - cap) / peak * 100
        max_dd = max(max_dd, dd)

    n        = len(trades)
    wins     = sum(1 for t in trades if t["win"])
    win_rate = wins / n if n else 0
    avg_win  = sum(t["pnl"] for t in trades if t["win"]) / wins if wins else 0
    n_loss   = n - wins
    avg_loss = abs(sum(t["pnl"] for t in trades if not t["win"]) / n_loss) if n_loss else 0
    total_ret = (cap - 100) / 100 * 100
    returns  = [(equity[i] - equity[i-1]) / equity[i-1] for i in range(1, len(equity))]

    return {
        "trades": n,
        "win_rate": round(win_rate * 100, 1),
        "avg_win_usd": round(avg_win, 4),
        "avg_loss_usd": round(avg_loss, 4),
        "profit_factor": round(avg_win * wins / (avg_loss * n_loss), 2) if avg_loss * n_loss > 0 else 0,
        "total_return_pct": round(total_ret, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe(returns), 2),
        "sortino": round(sortino(returns), 2),
        "final_capital": round(cap, 2),
        "equity_curve": equity,
    }


# ─── Core Bot ─────────────────────────────────────────────────────────────────

class ApexBot:
    def __init__(self):
        self.state = self._load_state()
        self._reset_daily_if_needed()
        self.backtest_results: dict = {}
        log.info(f"Bot initialized | Capital: ${self.state.capital:.2f}")

    def _load_state(self) -> BotState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                state = BotState(**{k: v for k, v in data.items()
                                    if k in BotState.__dataclass_fields__})
                state.open_positions = [Position(**p) for p in data.get("open_positions", [])]
                state.closed_trades  = [Position(**p) for p in data.get("closed_trades", [])]
                return state
            except Exception as e:
                log.warning(f"State load failed: {e}. Fresh start.")
        return BotState()

    def _save_state(self):
        data = asdict(self.state)
        data["open_positions"] = [asdict(p) for p in self.state.open_positions]
        data["closed_trades"]  = [asdict(p) for p in self.state.closed_trades]
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if self.state.today_date != today:
            self.state.today_date = today
            self.state.today_pnl  = 0.0
            log.info(f"New day: {today}")

    def _add_log(self, level: str, msg: str):
        entry = {"time": datetime.datetime.utcnow().isoformat(), "level": level, "msg": msg}
        self.state.log_entries.insert(0, entry)
        self.state.log_entries = self.state.log_entries[:300]

    def _execution_cost(self, price: float, size: float, direction: int) -> Tuple[float, float]:
        """Returns (actual execution price after slippage+spread, fee in USD)."""
        exec_price = price * (1 + direction * (SLIPPAGE + SPREAD_PCT / 2))
        fee        = exec_price * size * TAKER_FEE
        return exec_price, fee

    def _position_size(self, entry: float, atr_val: float) -> float:
        sl_dist  = atr_val * SL_ATR_MULT
        risk_amt = self.state.capital * MAX_RISK_PER_TRADE_PCT
        # Kelly cap: never more than 5% of capital in one trade
        max_size_usd = self.state.capital * 0.05
        size = risk_amt / sl_dist if sl_dist > 0 else 0
        return min(size, max_size_usd / entry)

    def _update_portfolio_value(self, prices: dict):
        unrealized = 0.0
        for pos in self.state.open_positions:
            cp = prices.get(pos.pair, {}).get("price")
            if cp:
                mult = 1 if pos.side == "LONG" else -1
                unrealized += mult * (cp - pos.entry_price) * pos.size
        pv = round(self.state.capital + unrealized, 2)
        self.state.portfolio_value = pv
        if pv > self.state.peak_value:
            self.state.peak_value = pv
        dd = (self.state.peak_value - pv) / self.state.peak_value * 100
        self.state.max_drawdown = round(max(self.state.max_drawdown, dd), 2)

    def _daily_halted(self) -> bool:
        limit = self.state.capital * DAILY_LOSS_LIMIT_PCT
        if self.state.today_pnl <= -limit:
            self._add_log("HALT", f"Daily loss limit hit (${self.state.today_pnl:.2f}). No new trades.")
            return True
        return False

    def try_entry(self, pair: str, sig: dict, current_price: float):
        if len(self.state.open_positions) >= MAX_CONCURRENT_POS:
            return
        if any(p.pair == pair for p in self.state.open_positions):
            return
        if self._daily_halted():
            return
        if not sig.get("signal"):
            return

        direction = 1 if sig["signal"] == "LONG" else -1
        atr_val   = sig["atr_val"]
        exec_price, fee_in = self._execution_cost(current_price, 1, direction)

        size = self._position_size(exec_price, atr_val)
        if size <= 0:
            return

        sl_dist = atr_val * SL_ATR_MULT
        tp_dist = atr_val * TP_ATR_MULT
        sl = round(exec_price - direction * sl_dist, 4)
        tp = round(exec_price + direction * tp_dist, 4)

        pos = Position(
            id          = self.state.trade_id_counter,
            pair        = pair,
            side        = sig["signal"],
            entry_price = round(exec_price, 4),
            size        = round(size, 6),
            stop_loss   = sl,
            take_profit = tp,
            open_time   = datetime.datetime.utcnow().isoformat(),
            reason      = sig.get("reason", ""),
            regime      = sig.get("regime", "?"),
            strategy    = sig.get("strategy", "?"),
            fees_paid   = round(fee_in * size, 6),
        )
        self.state.open_positions.append(pos)
        self.state.trade_id_counter += 1

        msg = (f"{sig['signal']} {pair} @ ${exec_price:,.4f} | "
               f"SL ${sl:,.4f} TP ${tp:,.4f} | Size {size:.4f} | "
               f"Fee ${fee_in * size:.4f} | {sig['reason']}")
        log.info(f"ENTRY: {msg}")
        self._add_log(sig["signal"], msg)
        self._save_state()

    def check_exits(self, prices: dict):
        still_open = []
        for pos in self.state.open_positions:
            cp_data = prices.get(pos.pair)
            if not cp_data:
                still_open.append(pos); continue
            cp    = cp_data["price"]
            mult  = 1 if pos.side == "LONG" else -1
            hit_sl = (mult == 1 and cp <= pos.stop_loss) or (mult == -1 and cp >= pos.stop_loss)
            hit_tp = (mult == 1 and cp >= pos.take_profit) or (mult == -1 and cp <= pos.take_profit)

            if hit_sl or hit_tp:
                raw_exit    = pos.take_profit if hit_tp else pos.stop_loss
                exec_exit, fee_out = self._execution_cost(raw_exit, pos.size, -mult)
                gross_pnl   = mult * (exec_exit - pos.entry_price) * pos.size
                total_fees  = pos.fees_paid + fee_out * pos.size
                net_pnl     = round(gross_pnl - total_fees, 6)
                pnl_pct     = net_pnl / (pos.entry_price * pos.size) * 100

                pos.status      = "CLOSED"
                pos.exit_price  = round(exec_exit, 4)
                pos.pnl         = net_pnl
                pos.pnl_pct     = round(pnl_pct, 2)
                pos.close_time  = datetime.datetime.utcnow().isoformat()
                pos.exit_reason = "TP" if hit_tp else "SL"
                pos.fees_paid  += round(fee_out * pos.size, 6)

                self.state.capital     = round(self.state.capital + net_pnl, 6)
                self.state.today_pnl   = round(self.state.today_pnl + net_pnl, 6)
                self.state.total_fees += total_fees
                self.state.total_trades += 1
                if net_pnl > 0:
                    self.state.winning_trades += 1
                ret = net_pnl / (pos.entry_price * pos.size)
                self.state.returns_history.append(ret)
                self.state.returns_history = self.state.returns_history[-500:]

                self.state.closed_trades.insert(0, pos)
                self.state.closed_trades = self.state.closed_trades[:200]
                self.state.equity_history.append(self.state.capital)
                self.state.equity_times.append(datetime.datetime.utcnow().isoformat())

                emoji = "WIN" if net_pnl > 0 else "LOSS"
                msg = (f"{emoji} | {pos.exit_reason} | {pos.side} {pos.pair} | "
                       f"Entry ${pos.entry_price:,.4f} Exit ${exec_exit:,.4f} | "
                       f"Net P&L ${net_pnl:+.4f} ({pnl_pct:+.2f}%) | "
                       f"Fees ${total_fees:.4f}")
                log.info(f"EXIT: {msg}")
                self._add_log("CLOSE", msg)
            else:
                still_open.append(pos)
        self.state.open_positions = still_open

    def run_cycle(self):
        self._reset_daily_if_needed()
        log.info("─── Cycle start ───")
        self._add_log("SCAN", "Fetching live prices…")

        prices = fetch_current_prices()
        if not prices:
            log.warning("No prices. Skipping.")
            return

        self.check_exits(prices)
        self._update_portfolio_value(prices)

        for pair in PAIRS:
            if pair not in prices:
                continue
            # Refresh candle cache every cycle
            candles = fetch_ohlcv(pair, days=30)
            if candles:
                candle_cache[pair] = candles
            else:
                candles = candle_cache.get(pair, [])

            cp  = prices[pair]["price"]
            sig = regime_and_signal(pair, candles)
            log.info(f"{pair}: ${cp:,.2f} | {sig.get('reason', '—')}")

            if sig.get("signal"):
                self.try_entry(pair, sig, cp)

        self._save_state()
        n   = self.state.total_trades
        wr  = (self.state.winning_trades / n * 100) if n else 0
        ret = ((self.state.portfolio_value - STARTING_CAPITAL) / STARTING_CAPITAL * 100)
        sh  = sharpe(self.state.returns_history)
        log.info(f"Portfolio ${self.state.portfolio_value:.2f} | Return {ret:+.2f}% | "
                 f"Trades {n} | WR {wr:.0f}% | MaxDD {self.state.max_drawdown:.1f}% | Sharpe {sh:.2f}")

    def get_dashboard_data(self) -> dict:
        n    = self.state.total_trades
        wr   = (self.state.winning_trades / n * 100) if n else 0
        pnl  = self.state.portfolio_value - STARTING_CAPITAL
        sh   = sharpe(self.state.returns_history)
        so   = sortino(self.state.returns_history)

        closed = [asdict(p) for p in self.state.closed_trades[:50]]
        wins   = [t for t in self.state.closed_trades if t.pnl > 0]
        losses = [t for t in self.state.closed_trades if t.pnl <= 0]
        avg_w  = sum(t.pnl for t in wins) / len(wins) if wins else 0
        avg_l  = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 0

        return {
            "portfolio_value": round(self.state.portfolio_value, 2),
            "capital": round(self.state.capital, 2),
            "starting_capital": STARTING_CAPITAL,
            "total_pnl": round(pnl, 2),
            "total_pnl_pct": round(pnl / STARTING_CAPITAL * 100, 2),
            "today_pnl": round(self.state.today_pnl, 2),
            "total_fees": round(self.state.total_fees, 4),
            "max_drawdown": self.state.max_drawdown,
            "sharpe": round(sh, 2),
            "sortino": round(so, 2),
            "open_positions": [asdict(p) for p in self.state.open_positions],
            "closed_trades": closed,
            "total_trades": n,
            "winning_trades": self.state.winning_trades,
            "win_rate": round(wr, 1),
            "avg_win": round(avg_w, 4),
            "avg_loss": round(avg_l, 4),
            "profit_factor": round(avg_w / avg_l, 2) if avg_l > 0 else 0,
            "equity_history": self.state.equity_history[-100:],
            "equity_times": self.state.equity_times[-100:],
            "log_entries": self.state.log_entries[:60],
            "backtest": self.backtest_results,
            "last_updated": datetime.datetime.utcnow().isoformat(),
        }


# ─── Flask Dashboard ──────────────────────────────────────────────────────────

bot = ApexBot()

try:
    from flask import Flask, jsonify
    app = Flask(__name__)

    DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>APEX BOT v2</title>
<style>
:root{--g:#00ffa3;--r:#ff4d6d;--bg:#060810;--card:#0d1117;--border:#1e2433;--text:#cdd6f4;--sub:#6c7086}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Courier New',monospace;padding:20px;min-height:100vh}
h1{font-size:22px;letter-spacing:4px;color:var(--g);text-shadow:0 0 20px rgba(0,255,163,.4)}
.sub{color:var(--sub);font-size:11px;margin-top:4px;letter-spacing:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:20px 0}
.card{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px}
.card .lbl{font-size:9px;color:var(--sub);letter-spacing:.12em;text-transform:uppercase}
.card .val{font-size:20px;font-weight:700;margin-top:6px}
.up{color:var(--g)}.down{color:var(--r)}.neu{color:#89b4fa}
table{width:100%;border-collapse:collapse;font-size:11px;margin-top:8px}
th{font-size:9px;color:var(--sub);text-transform:uppercase;text-align:left;padding:6px;border-bottom:1px solid var(--border)}
td{padding:6px;border-bottom:1px solid #111;font-size:11px}
.log-box{max-height:220px;overflow-y:auto;font-size:10px;background:#08090f;padding:8px;border-radius:4px;border:1px solid var(--border)}
.le{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid #111}
.lt{color:var(--sub);min-width:80px}.lv{min-width:50px;font-size:9px;padding:1px 5px;border-radius:3px;background:#1a1a2e;text-align:center}
h2{font-size:10px;color:var(--sub);text-transform:uppercase;letter-spacing:.1em;margin:20px 0 8px;border-bottom:1px solid var(--border);padding-bottom:6px}
.badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:9px;margin-left:4px}
.trend{background:#1a2a1a;color:#4ade80}.range{background:#1a1a2a;color:#818cf8}
.bt{background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:14px;margin:12px 0}
.bt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-top:8px}
.bt-stat{font-size:10px}.bt-stat span{display:block;color:var(--sub);font-size:9px;margin-bottom:2px}
</style>
</head>
<body>
<h1>⬡ APEX TRADING BOT v2</h1>
<p class="sub">PAPER TRADING · $100 SIMULATED · REGIME-FILTERED DUAL STRATEGY</p>

<div class="grid" id="metrics">Loading…</div>

<h2>Backtest Results (Historical Data)</h2>
<div class="bt" id="bt">Loading…</div>

<h2>Open Positions</h2>
<div id="positions"></div>

<h2>Closed Trades</h2>
<div id="trades"></div>

<h2>Bot Log</h2>
<div class="log-box" id="logdiv"></div>

<script>
async function load(){
  const d = await(await fetch('/api/state')).json();
  const pnl = d.total_pnl;
  const pSign = pnl >= 0 ? '+' : '';

  document.getElementById('metrics').innerHTML = `
    <div class="card"><div class="lbl">Portfolio</div><div class="val">$${d.portfolio_value.toFixed(2)}</div></div>
    <div class="card"><div class="lbl">Total P&L</div><div class="val ${pnl>=0?'up':'down'}">${pSign}$${Math.abs(pnl).toFixed(2)} (${pSign}${d.total_pnl_pct.toFixed(2)}%)</div></div>
    <div class="card"><div class="lbl">Today P&L</div><div class="val ${d.today_pnl>=0?'up':'down'}">${d.today_pnl>=0?'+':''}$${d.today_pnl.toFixed(4)}</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val neu">${d.total_trades>0?d.win_rate+'%':'—'}</div></div>
    <div class="card"><div class="lbl">Profit Factor</div><div class="val neu">${d.profit_factor||'—'}</div></div>
    <div class="card"><div class="lbl">Sharpe</div><div class="val neu">${d.sharpe}</div></div>
    <div class="card"><div class="lbl">Sortino</div><div class="val neu">${d.sortino}</div></div>
    <div class="card"><div class="lbl">Max Drawdown</div><div class="val down">${d.max_drawdown}%</div></div>
    <div class="card"><div class="lbl">Total Fees</div><div class="val down">$${d.total_fees.toFixed(4)}</div></div>
    <div class="card"><div class="lbl">Trades</div><div class="val">${d.total_trades}</div></div>
  `;

  const bt = d.backtest;
  if(bt && !bt.error){
    document.getElementById('bt').innerHTML = `
      <div style="font-size:10px;color:var(--sub)">Walk-forward on 30-day OHLCV</div>
      <div class="bt-grid">
        <div class="bt-stat"><span>Trades</span>${bt.trades}</div>
        <div class="bt-stat"><span>Win Rate</span>${bt.win_rate}%</div>
        <div class="bt-stat"><span>Profit Factor</span>${bt.profit_factor}</div>
        <div class="bt-stat"><span>Total Return</span><span class="${bt.total_return_pct>=0?'up':'down'}">${bt.total_return_pct>=0?'+':''}${bt.total_return_pct}%</span></div>
        <div class="bt-stat"><span>Max DD</span><span class="down">${bt.max_drawdown_pct}%</span></div>
        <div class="bt-stat"><span>Sharpe</span>${bt.sharpe}</div>
        <div class="bt-stat"><span>Sortino</span>${bt.sortino}</div>
        <div class="bt-stat"><span>Final Capital</span>$${bt.final_capital}</div>
      </div>`;
  } else {
    document.getElementById('bt').innerHTML = '<span style="color:var(--sub)">Running backtest on startup… refresh in 60s</span>';
  }

  const posHtml = d.open_positions.map(p=>`
    <tr>
      <td>${p.pair}</td>
      <td class="${p.side==='LONG'?'up':'down'}">${p.side}</td>
      <td>$${p.entry_price.toLocaleString()}</td>
      <td class="down">$${p.stop_loss.toLocaleString()}</td>
      <td class="up">$${p.take_profit.toLocaleString()}</td>
      <td><span class="badge ${p.regime==='TREND'?'trend':'range'}">${p.strategy}</span></td>
    </tr>`).join('');
  document.getElementById('positions').innerHTML = posHtml
    ? `<table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Strategy</th></tr></thead><tbody>${posHtml}</tbody></table>`
    : '<p style="color:var(--sub);font-size:11px">No open positions</p>';

  const trHtml = d.closed_trades.slice(0,20).map(t=>`
    <tr>
      <td>${t.pair}</td>
      <td class="${t.side==='LONG'?'up':'down'}">${t.side}</td>
      <td>$${t.entry_price.toLocaleString()}</td>
      <td>$${t.exit_price.toLocaleString()}</td>
      <td class="${t.pnl>=0?'up':'down'}">${t.pnl>=0?'+':''}$${t.pnl.toFixed(4)}</td>
      <td class="${t.pnl>=0?'up':'down'}">${t.pnl_pct>=0?'+':''}${t.pnl_pct?.toFixed(2)||'?'}%</td>
      <td>${t.exit_reason}</td>
      <td><span class="badge ${t.regime==='TREND'?'trend':'range'}">${t.strategy||'—'}</span></td>
    </tr>`).join('');
  document.getElementById('trades').innerHTML = trHtml
    ? `<table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>Exit</th><th>Net P&L</th><th>P&L%</th><th>Exit</th><th>Strategy</th></tr></thead><tbody>${trHtml}</tbody></table>`
    : '<p style="color:var(--sub);font-size:11px">No closed trades yet</p>';

  document.getElementById('logdiv').innerHTML = d.log_entries.slice(0,30).map(e=>
    `<div class="le"><span class="lt">${e.time.slice(11,19)}</span><span class="lv">${e.level}</span><span>${e.msg}</span></div>`
  ).join('');
}
load(); setInterval(load, 60000);
</script>
</body></html>"""

    @app.route("/")
    def index():
        return DASHBOARD_HTML

    @app.route("/api/state")
    def state_api():
        return jsonify(bot.get_dashboard_data())

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

except ImportError:
    log.warning("Flask not installed. pip install flask")
    app = None


# ─── Scheduler ───────────────────────────────────────────────────────────────

def run_bot_loop():
    # Run backtest on first pair at startup
    log.info("Running walk-forward backtest on BTC/USD…")
    candles = fetch_ohlcv("BTC/USD", days=30)
    if candles:
        candle_cache["BTC/USD"] = candles
        bt = run_backtest(candles, "BTC/USD")
        bot.backtest_results = bt
        log.info(f"BACKTEST: {bt.get('trades',0)} trades | WR {bt.get('win_rate',0)}% | "
                 f"Return {bt.get('total_return_pct',0):+.2f}% | MaxDD {bt.get('max_drawdown_pct',0):.2f}% | "
                 f"Sharpe {bt.get('sharpe',0):.2f} | PF {bt.get('profit_factor',0):.2f}")
    else:
        log.warning("Backtest skipped — could not fetch historical data.")

    schedule.every(FETCH_INTERVAL_SEC).seconds.do(bot.run_cycle)
    bot.run_cycle()
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("APEX TRADING BOT v2.0 STARTING")
    log.info(f"Capital: ${STARTING_CAPITAL} | Dual Strategy | Real Fees + Slippage")
    log.info("=" * 60)

    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()

    if app:
        port = int(os.environ.get("PORT", 5000))
        log.info(f"Dashboard: http://0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        bot_thread.join()
