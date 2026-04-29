"""
APEX CRYPTO TRADING BOT - Paper Trading Mode
=============================================
Strategy: Multi-Timeframe Momentum + Mean Reversion
Capital:  $100 simulated (paper trading only)
Target:   20-100% monthly return
Risk:     Max 2% per trade, 10% daily loss limit, 3 max positions

SETUP:
  pip install ccxt pandas numpy python-dotenv flask requests schedule

DEPLOY FREE ON RAILWAY.APP:
  1. Push this to GitHub
  2. Connect Railway to your repo
  3. Set env vars in Railway dashboard
  4. Railway auto-deploys and keeps it running 24/7
"""

import os
import json
import time
import logging
import threading
import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional
import requests
import schedule

# ─── Config ──────────────────────────────────────────────────────────────────

STARTING_CAPITAL = 100.0
MAX_RISK_PER_TRADE_PCT = 0.02          # 2% of capital per trade
DAILY_LOSS_LIMIT_PCT = 0.10            # halt trading if down 10% today
MAX_CONCURRENT_POSITIONS = 3
ATR_PERIOD = 14
RSI_PERIOD = 14
EMA_SHORT = 20
EMA_LONG = 50
REWARD_TO_RISK_RATIO = 3.0             # 3:1 TP:SL (2:1 minimum)
SL_ATR_MULTIPLIER = 1.5               # SL = 1.5x ATR from entry
FETCH_INTERVAL_SECONDS = 60            # CoinGecko free tier limit
PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD"]
LOG_FILE = "apex_bot.log"
STATE_FILE = "apex_bot_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("apex")


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class Position:
    id: int
    pair: str
    side: str                # "LONG" or "SHORT"
    entry_price: float
    size: float              # in base currency units
    stop_loss: float
    take_profit: float
    open_time: str
    reason: str
    status: str = "OPEN"
    exit_price: float = 0.0
    pnl: float = 0.0
    close_time: str = ""
    exit_reason: str = ""


@dataclass
class BotState:
    capital: float = STARTING_CAPITAL
    portfolio_value: float = STARTING_CAPITAL
    open_positions: list = field(default_factory=list)
    closed_trades: list = field(default_factory=list)
    today_pnl: float = 0.0
    today_date: str = ""
    trade_id_counter: int = 1
    equity_history: list = field(default_factory=lambda: [STARTING_CAPITAL])
    equity_times: list = field(default_factory=lambda: [datetime.datetime.utcnow().isoformat()])
    total_trades: int = 0
    winning_trades: int = 0
    log_entries: list = field(default_factory=list)


# ─── Indicator calculations ──────────────────────────────────────────────────

def compute_rsi(prices: list, period: int = RSI_PERIOD) -> float:
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[-(period + 1 - i)] - prices[-(period + 2 - i)]
        (gains if diff > 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.0001
    avg_loss = sum(losses) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def compute_macd(prices: list) -> tuple:
    """Returns (macd_line, signal_line, histogram)"""
    if len(prices) < 26:
        return 0.0, 0.0, 0.0
    ema12 = compute_ema(prices, 12)
    ema26 = compute_ema(prices, 26)
    macd_line = ema12 - ema26
    signal = macd_line * 0.8  # simplified; real impl needs history
    return macd_line, signal, macd_line - signal


def compute_atr(high: list, low: list, close: list, period: int = ATR_PERIOD) -> float:
    if len(high) < 2:
        return close[-1] * 0.02 if close else 0.0
    trs = []
    for i in range(1, min(period + 1, len(high))):
        tr = max(
            high[-i] - low[-i],
            abs(high[-i] - close[-i - 1]),
            abs(low[-i] - close[-i - 1])
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else close[-1] * 0.02


def compute_bollinger(prices: list, period: int = 20) -> tuple:
    """Returns (upper, middle, lower, %b position 0-100)"""
    if len(prices) < period:
        p = prices[-1]
        return p * 1.02, p, p * 0.98, 50.0
    window = prices[-period:]
    mid = sum(window) / period
    std = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    pct_b = ((prices[-1] - lower) / (upper - lower) * 100) if (upper - lower) > 0 else 50.0
    return upper, mid, lower, max(0.0, min(100.0, pct_b))


def compute_stoch_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period * 2:
        return 50.0
    rsi_values = [compute_rsi(prices[:i + period], period) for i in range(period)]
    min_rsi, max_rsi = min(rsi_values), max(rsi_values)
    if max_rsi == min_rsi:
        return 50.0
    return (rsi_values[-1] - min_rsi) / (max_rsi - min_rsi) * 100


# ─── Signal generation ───────────────────────────────────────────────────────

def generate_signal(pair: str, price_history: list) -> dict:
    """
    Multi-indicator signal generator.
    Returns dict with signal ('LONG', 'SHORT', or None), confidence, and indicator values.
    """
    if len(price_history) < 30:
        return {"signal": None, "reason": "Insufficient history"}

    prices = price_history
    close = prices
    high = [p * 1.005 for p in prices]   # synthetic high/low (replace with real OHLC)
    low = [p * 0.995 for p in prices]

    rsi = compute_rsi(prices)
    macd_line, signal_line, histogram = compute_macd(prices)
    ema_short = compute_ema(prices, EMA_SHORT)
    ema_long = compute_ema(prices, EMA_LONG)
    upper_bb, mid_bb, lower_bb, pct_b = compute_bollinger(prices)
    atr = compute_atr(high, low, close)
    stoch = compute_stoch_rsi(prices)

    current_price = prices[-1]
    macd_bullish = histogram > 0
    ema_uptrend = ema_short > ema_long

    indicators = {
        "rsi": round(rsi, 1),
        "macd": round(macd_line, 4),
        "macd_histogram": round(histogram, 4),
        "ema_short": round(ema_short, 2),
        "ema_long": round(ema_long, 2),
        "bb_upper": round(upper_bb, 2),
        "bb_lower": round(lower_bb, 2),
        "pct_b": round(pct_b, 1),
        "atr": round(atr, 2),
        "stoch_rsi": round(stoch, 1),
    }

    # ── LONG signal: oversold bounce in uptrend ──
    long_conditions = [
        rsi < 38,                        # RSI oversold
        macd_bullish,                    # MACD bullish momentum
        ema_uptrend,                     # Price above EMA trend
        stoch < 35,                      # Stochastic oversold
        pct_b < 40,                      # Near lower Bollinger Band
    ]
    long_score = sum(long_conditions)

    # ── SHORT signal: overbought rejection in downtrend ──
    short_conditions = [
        rsi > 68,                        # RSI overbought
        not macd_bullish,                # MACD bearish momentum
        not ema_uptrend,                 # Below EMA trend
        stoch > 72,                      # Stochastic overbought
        pct_b > 72,                      # Near upper Bollinger Band
    ]
    short_score = sum(short_conditions)

    if long_score >= 3 and long_score > short_score:
        confidence = long_score / len(long_conditions)
        reason = f"LONG: RSI={rsi:.0f} MACD={'bull' if macd_bullish else 'bear'} EMA={'up' if ema_uptrend else 'dn'} Stoch={stoch:.0f} BB={pct_b:.0f}%"
        return {"signal": "LONG", "confidence": confidence, "reason": reason, "indicators": indicators, "atr": atr}

    if short_score >= 3 and short_score > long_score:
        confidence = short_score / len(short_conditions)
        reason = f"SHORT: RSI={rsi:.0f} MACD={'bull' if macd_bullish else 'bear'} EMA={'up' if ema_uptrend else 'dn'} Stoch={stoch:.0f} BB={pct_b:.0f}%"
        return {"signal": "SHORT", "confidence": confidence, "reason": reason, "indicators": indicators, "atr": atr}

    return {"signal": None, "reason": f"No signal: RSI={rsi:.0f} Score L:{long_score}/S:{short_score}", "indicators": indicators}


# ─── Price fetcher ───────────────────────────────────────────────────────────

COINGECKO_IDS = {
    "BTC/USD": "bitcoin",
    "ETH/USD": "ethereum",
    "SOL/USD": "solana",
    "BNB/USD": "binancecoin",
}

price_history_cache: dict = {pair: [] for pair in PAIRS}


def fetch_live_prices() -> dict:
    ids = ",".join(COINGECKO_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result = {}
        for pair, cg_id in COINGECKO_IDS.items():
            if cg_id in data:
                result[pair] = {
                    "price": data[cg_id]["usd"],
                    "change_24h": data[cg_id].get("usd_24h_change", 0.0),
                }
        return result
    except Exception as e:
        log.warning(f"Price fetch failed: {e}")
        return {}


# ─── Core bot logic ──────────────────────────────────────────────────────────

class ApexBot:
    def __init__(self):
        self.state = self._load_state()
        self._reset_daily_if_needed()
        log.info(f"Bot initialized | Capital: ${self.state.capital:.2f} | Portfolio: ${self.state.portfolio_value:.2f}")

    def _load_state(self) -> BotState:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    data = json.load(f)
                state = BotState(**data)
                state.open_positions = [Position(**p) for p in state.open_positions]
                state.closed_trades = [Position(**p) for p in state.closed_trades]
                log.info(f"State loaded from {STATE_FILE}")
                return state
            except Exception as e:
                log.warning(f"Could not load state: {e}. Starting fresh.")
        return BotState()

    def _save_state(self):
        data = asdict(self.state)
        data["open_positions"] = [asdict(p) for p in self.state.open_positions]
        data["closed_trades"] = [asdict(p) for p in self.state.closed_trades]
        with open(STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if self.state.today_date != today:
            self.state.today_date = today
            self.state.today_pnl = 0.0
            log.info(f"New trading day: {today}. Daily P&L reset.")

    def _add_log(self, level: str, msg: str):
        entry = {"time": datetime.datetime.utcnow().isoformat(), "level": level, "msg": msg}
        self.state.log_entries.insert(0, entry)
        self.state.log_entries = self.state.log_entries[:200]   # keep last 200

    def _compute_position_size(self, price: float, atr: float) -> float:
        """Risk-adjusted position sizing: never risk more than 2% of capital."""
        risk_amount = self.state.capital * MAX_RISK_PER_TRADE_PCT
        sl_distance = atr * SL_ATR_MULTIPLIER
        if sl_distance <= 0:
            return 0.0
        size = risk_amount / sl_distance
        return round(size, 6)

    def _update_portfolio_value(self, current_prices: dict):
        unrealized = 0.0
        for pos in self.state.open_positions:
            cp = current_prices.get(pos.pair, {}).get("price")
            if cp:
                if pos.side == "LONG":
                    unrealized += (cp - pos.entry_price) * pos.size
                else:
                    unrealized += (pos.entry_price - cp) * pos.size
        self.state.portfolio_value = round(self.state.capital + unrealized, 2)

    def check_daily_halt(self) -> bool:
        """Returns True if trading should be halted today."""
        if self.state.today_pnl <= -(self.state.capital * DAILY_LOSS_LIMIT_PCT):
            log.warning(f"Daily loss limit hit: ${self.state.today_pnl:.2f}. Halting trading.")
            self._add_log("HALT", f"Daily loss limit reached (${self.state.today_pnl:.2f}). No new trades today.")
            return True
        return False

    def try_entry(self, pair: str, signal: dict, current_price: float):
        """Attempt to open a new position based on signal."""
        if len(self.state.open_positions) >= MAX_CONCURRENT_POSITIONS:
            return
        if any(p.pair == pair for p in self.state.open_positions):
            return
        if self.check_daily_halt():
            return

        sig = signal.get("signal")
        if not sig:
            return

        atr = signal.get("atr", current_price * 0.02)
        size = self._compute_position_size(current_price, atr)
        if size <= 0:
            return

        sl_dist = atr * SL_ATR_MULTIPLIER
        tp_dist = sl_dist * REWARD_TO_RISK_RATIO

        if sig == "LONG":
            sl = round(current_price - sl_dist, 2)
            tp = round(current_price + tp_dist, 2)
        else:
            sl = round(current_price + sl_dist, 2)
            tp = round(current_price - tp_dist, 2)

        pos = Position(
            id=self.state.trade_id_counter,
            pair=pair,
            side=sig,
            entry_price=round(current_price, 2),
            size=size,
            stop_loss=sl,
            take_profit=tp,
            open_time=datetime.datetime.utcnow().isoformat(),
            reason=signal.get("reason", ""),
        )
        self.state.open_positions.append(pos)
        self.state.trade_id_counter += 1

        msg = f"{sig} {pair} @ ${current_price:,.2f} | SL ${sl:,.2f} | TP ${tp:,.2f} | Size {size:.4f} | {signal.get('reason', '')}"
        log.info(f"ENTRY: {msg}")
        self._add_log(sig, msg)
        self._save_state()

    def check_exits(self, current_prices: dict):
        """Check all open positions for SL/TP hits."""
        still_open = []
        for pos in self.state.open_positions:
            cp_data = current_prices.get(pos.pair)
            if not cp_data:
                still_open.append(pos)
                continue
            cp = cp_data["price"]
            hit_sl = (pos.side == "LONG" and cp <= pos.stop_loss) or \
                     (pos.side == "SHORT" and cp >= pos.stop_loss)
            hit_tp = (pos.side == "LONG" and cp >= pos.take_profit) or \
                     (pos.side == "SHORT" and cp <= pos.take_profit)

            if hit_sl or hit_tp:
                exit_price = pos.take_profit if hit_tp else pos.stop_loss
                if pos.side == "LONG":
                    pnl = (exit_price - pos.entry_price) * pos.size
                else:
                    pnl = (pos.entry_price - exit_price) * pos.size
                pnl = round(pnl, 4)

                pos.status = "CLOSED"
                pos.exit_price = round(exit_price, 2)
                pos.pnl = pnl
                pos.close_time = datetime.datetime.utcnow().isoformat()
                pos.exit_reason = "TP" if hit_tp else "SL"

                self.state.capital = round(self.state.capital + pnl, 4)
                self.state.today_pnl = round(self.state.today_pnl + pnl, 4)
                self.state.total_trades += 1
                if pnl > 0:
                    self.state.winning_trades += 1

                self.state.closed_trades.insert(0, pos)
                self.state.closed_trades = self.state.closed_trades[:200]

                self.state.equity_history.append(self.state.capital)
                self.state.equity_times.append(datetime.datetime.utcnow().isoformat())

                emoji = "WIN" if pnl > 0 else "LOSS"
                msg = f"{emoji} | {pos.exit_reason} | {pos.side} {pos.pair} | Entry ${pos.entry_price:,.2f} Exit ${exit_price:,.2f} | P&L ${pnl:+.4f}"
                log.info(f"EXIT: {msg}")
                self._add_log("CLOSE", msg)
            else:
                still_open.append(pos)

        self.state.open_positions = still_open

    def run_cycle(self):
        """One full bot cycle: fetch prices → generate signals → entry/exit."""
        self._reset_daily_if_needed()
        log.info("Running trading cycle...")
        self._add_log("SCAN", "Fetching live market prices...")

        prices = fetch_live_prices()
        if not prices:
            log.warning("No prices received. Skipping cycle.")
            return

        self.check_exits(prices)
        self._update_portfolio_value(prices)

        for pair in PAIRS:
            if pair not in prices:
                continue
            cp = prices[pair]["price"]
            history = price_history_cache.get(pair, [])
            history.append(cp)
            if len(history) > 200:
                history = history[-200:]
            price_history_cache[pair] = history

            signal = generate_signal(pair, history)
            log.info(f"{pair}: ${cp:,.2f} | {signal.get('reason', 'No signal')}")

            if signal.get("signal"):
                self.try_entry(pair, signal, cp)

        self._save_state()
        win_rate = (self.state.winning_trades / self.state.total_trades * 100) if self.state.total_trades > 0 else 0
        log.info(
            f"Cycle done | Portfolio: ${self.state.portfolio_value:.2f} | "
            f"Capital: ${self.state.capital:.2f} | "
            f"Open: {len(self.state.open_positions)} | "
            f"Trades: {self.state.total_trades} | Win: {win_rate:.0f}%"
        )

    def get_dashboard_data(self) -> dict:
        """Returns all state for the dashboard API endpoint."""
        win_rate = (self.state.winning_trades / self.state.total_trades * 100) if self.state.total_trades > 0 else 0
        pnl = self.state.portfolio_value - STARTING_CAPITAL
        return {
            "portfolio_value": round(self.state.portfolio_value, 2),
            "capital": round(self.state.capital, 2),
            "starting_capital": STARTING_CAPITAL,
            "total_pnl": round(pnl, 2),
            "total_pnl_pct": round(pnl / STARTING_CAPITAL * 100, 2),
            "today_pnl": round(self.state.today_pnl, 2),
            "open_positions": [asdict(p) for p in self.state.open_positions],
            "closed_trades": [asdict(p) for p in self.state.closed_trades[:50]],
            "total_trades": self.state.total_trades,
            "winning_trades": self.state.winning_trades,
            "win_rate": round(win_rate, 1),
            "equity_history": self.state.equity_history[-60:],
            "equity_times": self.state.equity_times[-60:],
            "log_entries": self.state.log_entries[:50],
            "last_updated": datetime.datetime.utcnow().isoformat(),
        }


# ─── Flask Dashboard API ─────────────────────────────────────────────────────

bot = ApexBot()

try:
    from flask import Flask, jsonify, send_file
    app = Flask(__name__)

    @app.route("/")
    def index():
        """Serve the dashboard HTML."""
        return """<!DOCTYPE html>
<html><head><title>Apex Trading Bot</title>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font-family:monospace;background:#0a0a0a;color:#e0e0e0;padding:20px;margin:0}
  h1{color:#1D9E75;margin-bottom:4px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:16px 0}
  .card{background:#151515;border:1px solid #222;border-radius:8px;padding:12px}
  .label{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:.06em}
  .val{font-size:22px;font-weight:bold;margin-top:4px}
  .up{color:#1D9E75}.down{color:#c0392b}
  table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
  th{font-size:10px;text-transform:uppercase;color:#555;text-align:left;padding:6px 8px;border-bottom:1px solid #222}
  td{padding:7px 8px;border-bottom:1px solid #1a1a1a}
  .log{max-height:200px;overflow-y:auto;font-size:11px;background:#0f0f0f;padding:8px;border-radius:6px}
  .log-entry{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid #111}
  .t{color:#555;min-width:90px}.type{min-width:50px;text-align:center;padding:1px 4px;border-radius:3px;font-size:10px;background:#1a1a1a}
  .buy{color:#1D9E75}.sell{color:#c0392b}.info{color:#666}
  h2{font-size:13px;color:#888;text-transform:uppercase;letter-spacing:.06em;margin-top:20px;border-bottom:1px solid #1a1a1a;padding-bottom:6px}
</style></head>
<body>
<h1>APEX TRADING BOT</h1>
<small style="color:#555">Paper trading | $100 simulated capital | Auto-refreshes every 60s</small>
<div class="grid" id="metrics"></div>
<h2>Open positions</h2>
<div id="positions"><p style="color:#555">Loading...</p></div>
<h2>Trade log</h2>
<div class="log" id="logdiv"></div>
<script>
async function refresh(){
  const d=await(await fetch('/api/state')).json();
  const pnl=d.total_pnl;
  document.getElementById('metrics').innerHTML=`
    <div class="card"><div class="label">Portfolio</div><div class="val">$${d.portfolio_value.toFixed(2)}</div></div>
    <div class="card"><div class="label">P&L</div><div class="val ${pnl>=0?'up':'down'}">${pnl>=0?'+':''}$${Math.abs(pnl).toFixed(2)}</div></div>
    <div class="card"><div class="label">P&L %</div><div class="val ${pnl>=0?'up':'down'}">${pnl>=0?'+':''}${d.total_pnl_pct.toFixed(2)}%</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="val">${d.total_trades>0?d.win_rate+'%':'—'}</div></div>
    <div class="card"><div class="label">Trades</div><div class="val">${d.total_trades}</div></div>
    <div class="card"><div class="label">Open Pos</div><div class="val">${d.open_positions.length}</div></div>
  `;
  const tbody=d.open_positions.map(p=>`<tr><td>${p.pair}</td><td>${p.side}</td><td>$${p.entry_price.toLocaleString()}</td><td>$${p.stop_loss.toLocaleString()}</td><td>$${p.take_profit.toLocaleString()}</td></tr>`).join('');
  document.getElementById('positions').innerHTML=tbody?`<table><thead><tr><th>Pair</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th></tr></thead><tbody>${tbody}</tbody></table>`:'<p style="color:#555;font-size:12px">No open positions</p>';
  document.getElementById('logdiv').innerHTML=d.log_entries.slice(0,20).map(e=>`<div class="log-entry"><span class="t">${e.time.slice(11,19)}</span><span class="type ${e.level.toLowerCase()}">${e.level}</span><span>${e.msg}</span></div>`).join('');
}
refresh();setInterval(refresh,60000);
</script></body></html>"""

    @app.route("/api/state")
    def state():
        return jsonify(bot.get_dashboard_data())

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "time": datetime.datetime.utcnow().isoformat()})

except ImportError:
    log.warning("Flask not installed — running without dashboard. Install: pip install flask")
    app = None


# ─── Scheduler ───────────────────────────────────────────────────────────────

def run_bot_loop():
    schedule.every(FETCH_INTERVAL_SECONDS).seconds.do(bot.run_cycle)
    bot.run_cycle()   # run immediately on start
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("APEX TRADING BOT STARTING")
    log.info(f"Capital: ${STARTING_CAPITAL} | Strategy: Multi-TF Momentum")
    log.info("=" * 60)

    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()

    if app:
        port = int(os.environ.get("PORT", 5000))
        log.info(f"Dashboard running on http://0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        bot_thread.join()