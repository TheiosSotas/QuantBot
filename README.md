import csv
import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import ccxt
import pandas as pd
import requests

# ---------------------------------------------------------
# DESK CONFIGURATION
# ---------------------------------------------------------
SYMBOL = "BTC/USDT"            # Binance USDT-M Perpetual Futures
TIMEFRAME = "1h"
CANDLE_LIMIT = 250
SLEEP_INTERVAL_SECONDS = 60    # Evaluation cadence

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:14b"

MAX_RISK_PER_TRANCHE_PCT = 0.01  # Risk 1% equity per entry tranche
TAKE_PROFIT_RATIO = 2.0         # 2:1 Reward-to-Risk target
MAX_TRANCHES = 3                # Max pyramid additions on strong momentum

TRADES_CSV = "trades.csv"
MEMORY_FILE = "quant_memory.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Connect to Binance USDT-M Perpetual Futures
exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# ---------------------------------------------------------
# THREAD-SAFE STATE (RLock prevents recursive deadlocks)
# ---------------------------------------------------------
state_lock = threading.RLock()

BOT_STATE: Dict[str, Any] = {
    "system": {
        "status": "ONLINE",
        "market_type": "BINANCE USDT-M FUTURES",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "last_update": "Starting up...",
        "port": 0
    },
    "market": {
        "price": 0.0,
        "ema_20": 0.0,
        "ema_50": 0.0,
        "ema_200": 0.0,
        "rsi_14": 50.0,
        "atr_14": 0.0,
        "vol_ratio": 1.0
    },
    "model": {
        "regime": "CALIBRATING",
        "action": "HOLD",
        "confidence": 0.0,
        "thesis": "Initializing local Qwen 2.5 14B quantitative core..."
    },
    "portfolio": {
        "cash_usdt": 10000.00,
        "side": "FLAT",              # "FLAT", "LONG", or "SHORT"
        "position_units": 0.0,
        "entry_price": 0.0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "tranches": 0,
        "breakeven_active": False,
        "unrealized_pnl": 0.0,
        "total_equity": 10000.00
    },
    "trades": [],
    "copilot_feed": [],
    "lessons": []
}

# ---------------------------------------------------------
# PERSISTENCE & REFLEXION MEMORY BANK
# ---------------------------------------------------------
def load_historical_data():
    """Restores trading history and Qwen's memory bank from disk."""
    global BOT_STATE

    if os.path.exists(TRADES_CSV):
        try:
            df = pd.read_csv(TRADES_CSV)
            if not df.empty:
                loaded_trades = []
                cum_pnl = 0.0
                for _, row in df.iterrows():
                    pnl_val = float(row["PnL_USDT"])
                    cum_pnl += pnl_val
                    loaded_trades.insert(0, {
                        "time": str(row["Timestamp"]),
                        "side": str(row.get("Side", "LONG")),
                        "type": str(row["Type"]),
                        "entry": float(row["Entry"]),
                        "exit": float(row["Exit"]),
                        "units": float(row["Units"]),
                        "pnl": pnl_val,
                        "return_pct": float(row["Return_Pct"]),
                        "result": str(row["Result"])
                    })
                with state_lock:
                    BOT_STATE["trades"] = loaded_trades
                    BOT_STATE["portfolio"]["cash_usdt"] = round(10000.00 + cum_pnl, 2)
                    BOT_STATE["portfolio"]["total_equity"] = BOT_STATE["portfolio"]["cash_usdt"]
                logging.info(f"Loaded {len(loaded_trades)} historical trades. Equity: ${BOT_STATE['portfolio']['cash_usdt']:.2f}")
        except Exception as e:
            logging.error(f"Error loading {TRADES_CSV}: {e}")

    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                lessons = json.load(f)
                with state_lock:
                    BOT_STATE["lessons"] = lessons
                logging.info(f"Loaded {len(lessons)} institutional rules from {MEMORY_FILE}")
        except Exception as e:
            logging.error(f"Error loading {MEMORY_FILE}: {e}")

def save_lesson_to_memory(lesson_entry: Dict[str, Any]):
    with state_lock:
        BOT_STATE["lessons"].insert(0, lesson_entry)
        BOT_STATE["lessons"] = BOT_STATE["lessons"][:25]
        data_to_save = BOT_STATE["lessons"]

    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(data_to_save, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to persist rule: {e}")

def get_recent_lessons_for_prompt() -> str:
    with state_lock:
        lessons = BOT_STATE["lessons"][:4]
    if not lessons:
        return "No prior failures logged. Execute according to baseline risk parameters."

    rules = []
    for idx, l in enumerate(lessons, 1):
        rules.append(f"Prior Fault #{idx} ({l.get('side', 'TRADE')} on {l.get('date')}): "
                     f"{l.get('root_cause')} -> MANDATORY HARD RULE: {l.get('preventative_rule')}")
    return "\n".join(rules)

def log_trade_to_csv(trade_record: Dict[str, Any]):
    file_exists = os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, mode="a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Side", "Type", "Entry", "Exit", "Units", "PnL_USDT", "Return_Pct", "Result"])
        writer.writerow([
            trade_record["time"],
            trade_record["side"],
            trade_record["type"],
            trade_record["entry"],
            trade_record["exit"],
            trade_record["units"],
            trade_record["pnl"],
            trade_record["return_pct"],
            trade_record["result"]
        ])

def add_copilot_thought(message: str, category: str = "ANALYSIS"):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "category": category,
        "message": message
    }
    with state_lock:
        BOT_STATE["copilot_feed"].insert(0, entry)
        BOT_STATE["copilot_feed"] = BOT_STATE["copilot_feed"][:45]

# ---------------------------------------------------------
# TECHNICAL INDICATORS
# ---------------------------------------------------------
def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema_20"] = close.ewm(span=20, adjust=False).mean()
    df["ema_50"] = close.ewm(span=50, adjust=False).mean()
    df["ema_200"] = close.ewm(span=200, adjust=False).mean()

    # RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss.replace(0, 0.00001)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # ATR 14
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(window=14).mean()

    # Volume vs 20 SMA
    df["volume_sma_20"] = df["volume"].rolling(window=20).mean()
    df["vol_ratio"] = df["volume"] / df["volume_sma_20"].replace(0, 1)

    return df

def fetch_futures_market_snapshot() -> Optional[Dict[str, Any]]:
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = calculate_indicators(df)
        last_row = df.iloc[-1]

        return {
            "current_price": float(round(last_row["close"], 2)),
            "ema_20": float(round(last_row["ema_20"], 2)),
            "ema_50": float(round(last_row["ema_50"], 2)),
            "ema_200": float(round(last_row["ema_200"], 2)),
            "rsi_14": float(round(last_row["rsi_14"], 2)),
            "atr_14": float(round(last_row["atr_14"], 2)),
            "volume_ratio": float(round(last_row["vol_ratio"], 2)),
        }
    except Exception as e:
        logging.error(f"Error fetching futures candles: {e}")
        return None

# ---------------------------------------------------------
# QWEN 2.5 REASONING & REFLEXION POST-MORTEM
# ---------------------------------------------------------
def clean_json_response(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    text = text.strip()
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        text = text[first_brace : last_brace + 1]
    return text.strip()

def consult_qwen(metrics: Dict[str, Any], current_portfolio: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    past_lessons = get_recent_lessons_for_prompt()

    system_prompt = (
        "You are an elite quantitative portfolio manager operating a Binance USDT-M Futures desk.\n"
        "You have full authority to execute LONG and SHORT perpetual positions, scale into winners, or stay in cash.\n\n"
        "SELF-CORRECTING INSTITUTIONAL MEMORY (AVOID REPEATING THESE MISTAKES):\n"
        f"{past_lessons}\n\n"
        "STRATEGY & EXECUTION RULES:\n"
        "1. LONG setups require confirmed bullish structure: Price > EMA 50 > EMA 200, RSI < 68 (not overbought).\n"
        "2. SHORT setups require confirmed bearish breakdown: Price < EMA 50 < EMA 200, RSI > 32 (not oversold).\n"
        "3. NEVER open trades in choppy consolidation ranges where price oscillates randomly around EMAs.\n"
        "4. MOMENTUM PYRAMIDING: If we already hold a position in the winning direction with strong volume (ratio > 1.25), "
        "you may issue a PYRAMID scale-in signal to strengthen the position.\n"
        "5. Output STRICTLY valid JSON conforming to this schema:\n"
        "{\n"
        '  "market_regime": "TRENDING_BULL" | "TRENDING_BEAR" | "CHOPPY_RANGE" | "VOLATILITY_EXPANSION",\n'
        '  "action": "BUY" | "SELL" | "HOLD" | "CLOSE",\n'
        '  "confidence": 0.0 to 1.0,\n'
        '  "scale_in_signal": true | false,\n'
        '  "thesis": "Concise 1-sentence risk thesis",\n'
        '  "copilot_commentary": "Detailed 2-3 sentence live market and trade assessment for the operator."\n'
        "}"
    )

    user_payload = {
        "asset": f"{SYMBOL} (Perpetual Futures)",
        "timeframe": TIMEFRAME,
        "metrics": metrics,
        "portfolio_position": {
            "side": current_portfolio["side"],
            "tranches": current_portfolio["tranches"],
            "entry_price": current_portfolio["entry_price"],
            "unrealized_pnl": current_portfolio["unrealized_pnl"],
            "breakeven_active": current_portfolio["breakeven_active"]
        }
    }

    try:
        logging.info("🧠 Sending snapshot to Qwen 2.5 (Awaiting GPU inference)...")
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "system": system_prompt,
                "prompt": json.dumps(user_payload),
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=120,
        )
        response.raise_for_status()
        raw_output = response.json().get("response", "")
        cleaned_output = clean_json_response(raw_output)
        return json.loads(cleaned_output)
    except requests.exceptions.Timeout:
        logging.error("⚠️ Ollama inference timed out (model loading into VRAM). Retrying next loop.")
        return None
    except json.JSONDecodeError as jde:
        logging.error(f"⚠️ JSON Decode Error: {jde}. Response was: {raw_output[:120]}")
        return None
    except Exception as e:
        logging.error(f"⚠️ Qwen futures reasoning error: {e}")
        return None

def run_ai_post_mortem(closed_trade: Dict[str, Any], market_context: Dict[str, Any]):
    """Async background worker: critiques losses and persists new defensive rules."""
    def _worker():
        logging.info(f"Running Qwen 2.5 post-mortem on {closed_trade['side']} trade...")
        prompt = (
            "You are a hedge fund post-mortem risk analyst reviewing a failed futures trade.\n"
            f"Asset: {SYMBOL} Futures\n"
            f"Position Side: {closed_trade['side']}\n"
            f"Entry Price: ${closed_trade['entry']:,.2f} | Exit Price: ${closed_trade['exit']:,.2f}\n"
            f"Realized PnL: ${closed_trade['pnl']:.2f} ({closed_trade['return_pct']:.2f}%)\n"
            f"Exit Trigger: {closed_trade['type']}\n"
            f"Indicators at Exit: RSI {market_context.get('rsi_14')}, ATR ${market_context.get('atr_14')}, "
            f"Volume Ratio {market_context.get('volume_ratio')}x\n\n"
            "TASK:\n"
            "Diagnose mathematically why this trade failed to reach the profit target. "
            "Formulate 1 strict quantitative rule to eliminate this error in the future.\n"
            "Output strictly in JSON:\n"
            "{\n"
            '  "root_cause": "1 sentence technical diagnosis of failure",\n'
            '  "preventative_rule": "1 concrete quantitative entry/filter rule to avoid repeating this"\n'
            "}"
        )

        try:
            resp = requests.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.2}
                },
                timeout=60
            )
            resp.raise_for_status()
            res = json.loads(clean_json_response(resp.json().get("response", "")))

            lesson = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "side": closed_trade["side"],
                "pnl": closed_trade["pnl"],
                "root_cause": res.get("root_cause", "Momentum exhaustion against trend."),
                "preventative_rule": res.get("preventative_rule", "Wait for confirmed pullbacks to EMA 20 before entry.")
            }
            save_lesson_to_memory(lesson)
            add_copilot_thought(f"🧠 REFLEXION LEARNED ({lesson['side']}): {lesson['preventative_rule']}", category="LEARNING")
        except Exception as e:
            logging.error(f"Error during post-mortem: {e}")

    threading.Thread(target=_worker, daemon=True).start()

# ---------------------------------------------------------
# EXECUTION FIREWALL (BREAKEVEN & PYRAMIDING)
# ---------------------------------------------------------
def execute_trade_logic(metrics: Dict[str, Any], decision: Dict[str, Any]):
    global BOT_STATE
    price = metrics["current_price"]
    atr = metrics["atr_14"]
    action = str(decision.get("action", "HOLD")).strip().upper()
    scale_in_signal = decision.get("scale_in_signal", False)

    raw_conf = decision.get("confidence", 0.0)
    try:
        clean_conf = float(str(raw_conf).replace("%", ""))
        confidence = clean_conf / 100.0 if clean_conf > 1.0 else clean_conf
    except ValueError:
        confidence = 0.0

    with state_lock:
        portfolio = BOT_STATE["portfolio"]
        side = portfolio["side"]
        units = portfolio["position_units"]
        entry = portfolio["entry_price"]
        sl = portfolio["stop_loss"]
        tp = portfolio["take_profit"]

        # ---------------------------------------------------------
        # 1. MANAGE ACTIVE POSITION
        # ---------------------------------------------------------
        if side in ["LONG", "SHORT"] and units > 0:
            if side == "LONG":
                portfolio["unrealized_pnl"] = round((price - entry) * units, 2)
            else:
                portfolio["unrealized_pnl"] = round((entry - price) * units, 2)

            portfolio["total_equity"] = round(portfolio["cash_usdt"] + portfolio["unrealized_pnl"], 2)

            # Two-Way Dynamic Breakeven Shield (50% Distance to TP)
            if not portfolio["breakeven_active"]:
                if side == "LONG":
                    dist_to_tp = tp - entry
                    be_threshold = entry + (0.50 * dist_to_tp)
                    if price >= be_threshold:
                        portfolio["stop_loss"] = entry
                        portfolio["breakeven_active"] = True
                        msg = f"🛡️ LONG SHIELD ACTIVATED: Bitcoin hit 50% TP distance (${price:,.2f}). SL snapped to Entry (${entry:,.2f}). Trade is now RISK-FREE."
                        logging.info(msg)
                        add_copilot_thought(msg, category="GUARDRAIL")
                elif side == "SHORT":
                    dist_to_tp = entry - tp
                    be_threshold = entry - (0.50 * dist_to_tp)
                    if price <= be_threshold:
                        portfolio["stop_loss"] = entry
                        portfolio["breakeven_active"] = True
                        msg = f"🛡️ SHORT SHIELD ACTIVATED: Bitcoin dropped 50% TP distance (${price:,.2f}). SL snapped to Entry (${entry:,.2f}). Trade is now RISK-FREE."
                        logging.info(msg)
                        add_copilot_thought(msg, category="GUARDRAIL")

            # Momentum Pyramiding
            can_scale = (
                scale_in_signal
                and confidence >= 0.80
                and portfolio["tranches"] < MAX_TRANCHES
                and ((side == "LONG" and price > entry) or (side == "SHORT" and price < entry))
            )

            if can_scale:
                stop_dist = abs(price - sl) if sl > 0 else (1.5 * atr)
                risk_budget = portfolio["total_equity"] * MAX_RISK_PER_TRANCHE_PCT
                add_units = round(risk_budget / stop_dist, 4)

                if add_units > 0:
                    new_total_units = round(units + add_units, 4)
                    blended_entry = round(((entry * units) + (price * add_units)) / new_total_units, 2)

                    portfolio["tranches"] += 1
                    portfolio["position_units"] = new_total_units
                    portfolio["entry_price"] = blended_entry

                    if side == "LONG":
                        portfolio["stop_loss"] = round(max(sl, price - (1.2 * atr)), 2)
                        portfolio["take_profit"] = round(blended_entry + (abs(blended_entry - portfolio['stop_loss']) * TAKE_PROFIT_RATIO), 2)
                    else:
                        portfolio["stop_loss"] = round(min(sl, price + (1.2 * atr)), 2)
                        portfolio["take_profit"] = round(blended_entry - (abs(portfolio['stop_loss'] - blended_entry) * TAKE_PROFIT_RATIO), 2)

                    scale_msg = (f"⚡ PYRAMID SCALE-IN #{portfolio['tranches']}: Added {add_units} BTC to {side}. "
                                 f"New Blended Entry: ${blended_entry:,.2f} | Total: {new_total_units} BTC.")
                    logging.info(scale_msg)
                    add_copilot_thought(scale_msg, category="PYRAMID_SCALE")

            # Check Exit Conditions
            close_reason = None
            if side == "LONG":
                if price <= portfolio["stop_loss"]:
                    close_reason = "BREAKEVEN_EXIT" if portfolio["breakeven_active"] and abs(price - entry) <= (entry * 0.005) else "STOP_LOSS"
                elif price >= portfolio["take_profit"]:
                    close_reason = "TAKE_PROFIT"
                elif action in ["SELL", "CLOSE"] and confidence >= 0.75:
                    close_reason = "MODEL_REVERSAL"
            elif side == "SHORT":
                if price >= portfolio["stop_loss"]:
                    close_reason = "BREAKEVEN_EXIT" if portfolio["breakeven_active"] and abs(price - entry) <= (entry * 0.005) else "STOP_LOSS"
                elif price <= portfolio["take_profit"]:
                    close_reason = "TAKE_PROFIT"
                elif action in ["BUY", "CLOSE"] and confidence >= 0.75:
                    close_reason = "MODEL_REVERSAL"

            if close_reason:
                if side == "LONG":
                    pnl = round((price - entry) * units, 2)
                    return_pct = round(((price - entry) / entry) * 100, 2)
                else:
                    pnl = round((entry - price) * units, 2)
                    return_pct = round(((entry - price) / entry) * 100, 2)

                portfolio["cash_usdt"] = round(portfolio["cash_usdt"] + pnl, 2)
                portfolio["total_equity"] = portfolio["cash_usdt"]

                trade_record = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "side": side,
                    "type": close_reason,
                    "entry": entry,
                    "exit": price,
                    "units": units,
                    "pnl": pnl,
                    "return_pct": return_pct,
                    "result": "WIN" if pnl > 0 else ("BREAKEVEN" if pnl == 0 else "LOSS")
                }
                BOT_STATE["trades"].insert(0, trade_record)
                log_trade_to_csv(trade_record)

                # Reset to FLAT
                portfolio["side"] = "FLAT"
                portfolio["position_units"] = 0.0
                portfolio["entry_price"] = 0.0
                portfolio["stop_loss"] = 0.0
                portfolio["take_profit"] = 0.0
                portfolio["tranches"] = 0
                portfolio["breakeven_active"] = False
                portfolio["unrealized_pnl"] = 0.0

                exit_msg = f"Closed {side} via {close_reason} at ${price:,.2f} | Realized PnL: {pnl:+.2f} USDT"
                logging.info(exit_msg)
                add_copilot_thought(exit_msg, category="TRADE_EXIT")

                if pnl < 0:
                    run_ai_post_mortem(trade_record, metrics)
                return

        # ---------------------------------------------------------
        # 2. EVALUATE FRESH ENTRY (FROM FLAT)
        # ---------------------------------------------------------
        if portfolio["side"] == "FLAT":
            stop_distance = 1.5 * atr
            risk_budget = portfolio["cash_usdt"] * MAX_RISK_PER_TRANCHE_PCT
            units_to_open = round(risk_budget / stop_distance, 4)

            # LONG ENTRY
            if action == "BUY" and confidence >= 0.75 and units_to_open > 0:
                portfolio["side"] = "LONG"
                portfolio["position_units"] = units_to_open
                portfolio["entry_price"] = price
                portfolio["stop_loss"] = round(price - stop_distance, 2)
                portfolio["take_profit"] = round(price + (stop_distance * TAKE_PROFIT_RATIO), 2)
                portfolio["tranches"] = 1
                portfolio["breakeven_active"] = False
                portfolio["unrealized_pnl"] = 0.0

                entry_msg = (f"🚀 OPENED FUTURES LONG: {units_to_open} BTC at ${price:,.2f} | "
                             f"Initial SL: ${portfolio['stop_loss']:,.2f} | Target TP: ${portfolio['take_profit']:,.2f}")
                logging.info(entry_msg)
                add_copilot_thought(entry_msg, category="LONG_ENTRY")

            # SHORT ENTRY
            elif action == "SELL" and confidence >= 0.75 and units_to_open > 0:
                portfolio["side"] = "SHORT"
                portfolio["position_units"] = units_to_open
                portfolio["entry_price"] = price
                portfolio["stop_loss"] = round(price + stop_distance, 2)
                portfolio["take_profit"] = round(price - (stop_distance * TAKE_PROFIT_RATIO), 2)
                portfolio["tranches"] = 1
                portfolio["breakeven_active"] = False
                portfolio["unrealized_pnl"] = 0.0

                entry_msg = (f"🔻 OPENED FUTURES SHORT: {units_to_open} BTC at ${price:,.2f} | "
                             f"Initial SL: ${portfolio['stop_loss']:,.2f} | Target TP: ${portfolio['take_profit']:,.2f}")
                logging.info(entry_msg)
                add_copilot_thought(entry_msg, category="SHORT_ENTRY")

            portfolio["total_equity"] = portfolio["cash_usdt"]

# ---------------------------------------------------------
# CRYPTORIO 5.0 THEMED DASHBOARD
# ---------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Cryptorio 5.0 | Binance Futures Desk</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-base: #070a10;
      --bg-card: #0f1420;
      --bg-card-alt: #131929;
      --border-color: #1a2336;
      --neon-cyan: #00f0ff;
      --neon-green: #00e676;
      --neon-red: #ff3366;
      --neon-gold: #ffb300;
      --neon-purple: #c084fc;
      --text-main: #f8fafc;
      --text-muted: #8391a7;
      --font-ui: 'Space Grotesk', system-ui, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg-base);
      color: var(--text-main);
      font-family: var(--font-ui);
      padding: 16px;
      line-height: 1.4;
      overflow-x: hidden;
    }
    .nav {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 20px;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      margin-bottom: 16px;
    }
    .logo-area { display: flex; align-items: center; gap: 12px; }
    .logo-badge {
      background: linear-gradient(135deg, #00f0ff 0%, #3b82f6 100%);
      color: #000;
      font-weight: 800;
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 13px;
      letter-spacing: 0.05em;
    }
    .pulse-dot {
      width: 8px; height: 8px;
      background: var(--neon-green);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--neon-green);
      animation: pulse 1.8s infinite;
    }
    @keyframes pulse { 0% { opacity: 0.3; } 50% { opacity: 1; } 100% { opacity: 0.3; } }
    .top-row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .kpi-card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      padding: 14px 18px;
    }
    .kpi-title { font-size: 11px; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.08em; font-weight: 600; }
    .kpi-val { font-size: 24px; font-weight: 700; font-family: var(--font-mono); margin-top: 4px; }
    .layout-grid {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    @media (max-width: 1200px) { .layout-grid { grid-template-columns: 1fr; } }
    .panel {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 16px;
      display: flex;
      flex-direction: column;
    }
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--border-color);
    }
    .panel-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
    .chart-box {
      width: 100%;
      height: 480px;
      border-radius: 8px;
      overflow: hidden;
      background: #0b0f19;
    }
    .copilot-stream {
      height: 340px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding-right: 6px;
    }
    .copilot-stream::-webkit-scrollbar { width: 5px; }
    .copilot-stream::-webkit-scrollbar-thumb { background: var(--border-color); border-radius: 4px; }
    .chat-bubble {
      background: var(--bg-card-alt);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 13px;
    }
    .chat-meta {
      display: flex;
      justify-content: space-between;
      font-size: 10px;
      font-family: var(--font-mono);
      color: var(--text-muted);
      margin-bottom: 4px;
    }
    .tag-analysis { color: var(--neon-cyan); }
    .tag-guardrail { color: var(--neon-gold); font-weight: 700; }
    .tag-long_entry { color: var(--neon-green); font-weight: 700; }
    .tag-short_entry { color: var(--neon-red); font-weight: 700; }
    .tag-pyramid_scale { color: var(--neon-purple); font-weight: 700; }
    .tag-trade_exit { color: #f43f5e; font-weight: 700; }
    .tag-learning { color: #e879f9; font-weight: 700; }
    .telemetry-row {
      display: flex;
      justify-content: space-between;
      padding: 8px 0;
      border-bottom: 1px solid rgba(255,255,255,0.04);
      font-size: 13px;
    }
    .telemetry-row:last-child { border-bottom: none; }
    .shield-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 700;
      font-family: var(--font-mono);
    }
    .shield-safe { background: rgba(0, 230, 118, 0.15); color: var(--neon-green); border: 1px solid var(--neon-green); }
    .shield-wait { background: rgba(255, 179, 0, 0.12); color: var(--neon-gold); border: 1px solid var(--neon-gold); }
    .badge {
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
      font-family: var(--font-mono);
      text-transform: uppercase;
    }
    .badge-long { background: rgba(0,230,118,0.15); color: var(--neon-green); border: 1px solid var(--neon-green); }
    .badge-short { background: rgba(255,51,102,0.15); color: var(--neon-red); border: 1px solid var(--neon-red); }
    .badge-flat { background: rgba(255,179,0,0.15); color: var(--neon-gold); border: 1px solid var(--neon-gold); }
    .lesson-item {
      background: #161226;
      border-left: 3px solid var(--neon-purple);
      border-radius: 0 6px 6px 0;
      padding: 10px 12px;
      margin-bottom: 8px;
      font-size: 12px;
    }
    table { width: 100%; border-collapse: collapse; margin-top: 6px; }
    th { text-align: left; padding: 10px; font-size: 11px; color: var(--text-muted); border-bottom: 1px solid var(--border-color); text-transform: uppercase; }
    td { padding: 10px; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,0.03); }
    .win { color: var(--neon-green); font-weight: 700; }
    .loss { color: var(--neon-red); font-weight: 700; }
    .mono { font-family: var(--font-mono); }
  </style>
</head>
<body>

  <div class="nav">
    <div class="logo-area">
      <div class="logo-badge">CRYPTORIO 5.0</div>
      <div class="pulse-dot"></div>
      <div style="font-size: 13px; font-weight: 600;">BINANCE USDT-M FUTURES DESK (QWEN 2.5 14B)</div>
    </div>
    <div style="display: flex; gap: 16px; align-items: center;">
      <span class="badge" style="background:#131c2e; color: var(--neon-cyan); border: 1px solid var(--border-color);" id="nav-pair">BTC/USDT PERP [1H]</span>
      <span style="font-size: 12px; color: var(--text-muted); font-family: var(--font-mono);" id="nav-time">--:--:--</span>
    </div>
  </div>

  <div class="top-row">
    <div class="kpi-card">
      <div class="kpi-title">Bitcoin Futures Index</div>
      <div class="kpi-val" style="color: #fff;" id="kpi-price">$0.00</div>
      <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Perpetual Contract Index</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Portfolio Margin Equity</div>
      <div class="kpi-val" style="color: var(--neon-cyan);" id="kpi-equity">$10,000.00</div>
      <div style="font-size: 11px; font-family: var(--font-mono); margin-top: 4px;" id="kpi-pnl">Unrealized: $0.00</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Active Market Direction</div>
      <div style="display: flex; align-items: center; gap: 8px; margin-top: 6px;">
        <span class="badge badge-flat" id="kpi-side">FLAT</span>
        <span class="mono" style="font-size: 14px; font-weight: 700;" id="kpi-tranches">0/3 Tranches</span>
      </div>
      <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;" id="kpi-regime">REGIME: CALIBRATING</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Two-Way Downside Shield</div>
      <div style="margin-top: 6px;">
        <span class="shield-badge shield-wait" id="kpi-shield">STANDBY (FLAT)</span>
      </div>
      <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">50% TP Breakeven Snap</div>
    </div>
  </div>

  <div class="layout-grid">
    <div style="display: flex; flex-direction: column; gap: 16px;">
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Institutional Candlestick Chart (Binance Futures)</span>
          <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">Real-Time Perp Feed</span>
        </div>
        <div class="chart-box">
          <iframe 
            src="https://s.tradingview.com/widgetembed/?frameElementId=tradingview_widget&symbol=BINANCE%3ABTCUSDT.P&interval=60&hidesidetoolbar=0&symboledit=1&saveimage=1&toolbarbg=0f1420&studies=%5B%22RSI%40tv-basicstudies%22%2C%22MASimple%40tv-basicstudies%22%5D&theme=dark&style=1&timezone=Etc%2FUTC&overrides=%7B%22mainSeriesProperties.candleStyle.upColor%22%3A%22%2300e676%22%2C%22mainSeriesProperties.candleStyle.downColor%22%3A%22%23ff3366%22%7D&locale=en"
            style="width: 100%; height: 100%; border: none;">
          </iframe>
        </div>
      </div>

      <div class="panel" style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        <div>
          <div class="panel-title" style="margin-bottom: 8px;">Futures Position Guardrails</div>
          <div class="telemetry-row"><span>Position Size:</span><span class="mono" id="tel-units">0.0000 BTC</span></div>
          <div class="telemetry-row"><span>Blended Entry:</span><span class="mono" id="tel-entry">$0.00</span></div>
          <div class="telemetry-row"><span>Stop-Loss Level:</span><span class="mono" style="color: var(--neon-red);" id="tel-sl">$0.00</span></div>
          <div class="telemetry-row"><span>Take-Profit Target:</span><span class="mono" style="color: var(--neon-green);" id="tel-tp">$0.00</span></div>
          <div class="telemetry-row"><span>50% Breakeven Point:</span><span class="mono" style="color: var(--neon-gold);" id="tel-be">$0.00</span></div>
        </div>
        <div>
          <div class="panel-title" style="margin-bottom: 8px;">Quant Momentum Metrics</div>
          <div class="telemetry-row"><span>RSI (14-Period):</span><span class="mono" style="font-weight: 700;" id="tel-rsi">50.0</span></div>
          <div class="telemetry-row"><span>20 Exponential MA:</span><span class="mono" id="tel-ema20">$0.00</span></div>
          <div class="telemetry-row"><span>50 Exponential MA:</span><span class="mono" id="tel-ema50">$0.00</span></div>
          <div class="telemetry-row"><span>200 Exponential MA:</span><span class="mono" id="tel-ema200">$0.00</span></div>
          <div class="telemetry-row"><span>ATR Volatility (14):</span><span class="mono" id="tel-atr">$0.00</span></div>
        </div>
      </div>
    </div>

    <div style="display: flex; flex-direction: column; gap: 16px;">
      <div class="panel" style="flex: 1;">
        <div class="panel-header">
          <span class="panel-title">Qwen 2.5 Futures Copilot (Live Narrative)</span>
          <span class="badge" style="background: rgba(0,240,255,0.1); color: var(--neon-cyan); border: 1px solid var(--neon-cyan);">60s AUDIT</span>
        </div>
        <div class="copilot-stream" id="copilot-box">
          <div class="chat-bubble">
            <div class="chat-meta"><span class="tag-analysis">ANALYSIS</span><span>Just now</span></div>
            <div>Awaiting initial inference loop from local GPU...</div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">Reflexion Memory Bank (Self-Correction)</span>
          <span style="font-size: 11px; color: var(--neon-purple); font-weight: 700;">INSTITUTIONAL RULES</span>
        </div>
        <div id="lessons-box" style="max-height: 200px; overflow-y: auto;">
          <div style="font-size: 12px; color: var(--text-muted); font-style: italic;">No losing trades logged. Model adhering to core constraints.</div>
        </div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Futures Master Execution Ledger</span>
      <span style="font-size: 11px; color: var(--text-muted); font-family: var(--font-mono);">Permanent Disk Storage (trades.csv)</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Timestamp</th>
          <th>Side</th>
          <th>Trigger Event</th>
          <th>Entry</th>
          <th>Exit</th>
          <th>Units</th>
          <th>PnL (USDT)</th>
          <th>Return %</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody id="trade-table" class="mono">
        <tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 16px;">No closed trades logged yet.</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    async function syncTerminal() {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) return;
        const data = await res.json();

        document.getElementById('nav-time').innerText = data.system.last_update;
        document.getElementById('kpi-price').innerText = "$" + data.market.price.toLocaleString('en-US', {minimumFractionDigits: 2});
        document.getElementById('kpi-equity').innerText = "$" + data.portfolio.total_equity.toLocaleString('en-US', {minimumFractionDigits: 2});

        const unPnl = data.portfolio.unrealized_pnl;
        const pnlEl = document.getElementById('kpi-pnl');
        pnlEl.innerText = "Unrealized: " + (unPnl >= 0 ? "+" : "") + unPnl.toFixed(2) + " USDT";
        pnlEl.style.color = unPnl >= 0 ? "var(--neon-green)" : "var(--neon-red)";

        const sideEl = document.getElementById('kpi-side');
        sideEl.innerText = data.portfolio.side;
        sideEl.className = 'badge ' + (data.portfolio.side === 'LONG' ? 'badge-long' : (data.portfolio.side === 'SHORT' ? 'badge-short' : 'badge-flat'));
        document.getElementById('kpi-tranches').innerText = data.portfolio.tranches + "/3 Tranches";
        document.getElementById('kpi-regime').innerText = "REGIME: " + data.model.regime;

        const shieldEl = document.getElementById('kpi-shield');
        if (data.portfolio.side !== "FLAT") {
          if (data.portfolio.breakeven_active) {
            shieldEl.className = 'shield-badge shield-safe';
            shieldEl.innerText = 'SHIELD: ACTIVE (RISK-FREE)';
          } else {
            shieldEl.className = 'shield-badge shield-wait';
            shieldEl.innerText = 'SHIELD: ARMED (50% TP)';
          }
        } else {
          shieldEl.className = 'shield-badge shield-wait';
          shieldEl.innerText = 'STANDBY (FLAT)';
        }

        const entry = data.portfolio.entry_price;
        const tp = data.portfolio.take_profit;
        let beLevel = 0;
        if (entry > 0 && tp > 0) {
          beLevel = data.portfolio.side === "LONG" ? (entry + 0.5 * (tp - entry)) : (entry - 0.5 * (entry - tp));
        }

        document.getElementById('tel-units').innerText = data.portfolio.position_units.toFixed(4) + " BTC";
        document.getElementById('tel-entry').innerText = "$" + entry.toLocaleString();
        document.getElementById('tel-sl').innerText = "$" + data.portfolio.stop_loss.toLocaleString();
        document.getElementById('tel-tp').innerText = "$" + tp.toLocaleString();
        document.getElementById('tel-be').innerText = "$" + beLevel.toLocaleString();

        document.getElementById('tel-rsi').innerText = data.market.rsi_14;
        document.getElementById('tel-ema20').innerText = "$" + data.market.ema_20.toLocaleString();
        document.getElementById('tel-ema50').innerText = "$" + data.market.ema_50.toLocaleString();
        document.getElementById('tel-ema200').innerText = "$" + data.market.ema_200.toLocaleString();
        document.getElementById('tel-atr').innerText = "$" + data.market.atr_14.toLocaleString();

        if (data.copilot_feed && data.copilot_feed.length > 0) {
          const feedBox = document.getElementById('copilot-box');
          feedBox.innerHTML = data.copilot_feed.map(item => `
            <div class="chat-bubble">
              <div class="chat-meta">
                <span class="tag-${item.category.toLowerCase()}">${item.category}</span>
                <span>${item.time}</span>
              </div>
              <div style="color: #e2e8f0;">${item.message}</div>
            </div>
          `).join('');
        }

        if (data.lessons && data.lessons.length > 0) {
          const lBox = document.getElementById('lessons-box');
          lBox.innerHTML = data.lessons.map(l => `
            <div class="lesson-item">
              <div style="font-weight: 700; color: #f472b6; margin-bottom: 2px;">AUDIT [${l.date}]: Loss ${l.pnl} USDT (${l.side})</div>
              <div style="color: #cbd5e1; margin-bottom: 4px;"><b>Cause:</b> ${l.root_cause}</div>
              <div style="color: var(--neon-cyan);"><b>Rule:</b> ${l.preventative_rule}</div>
            </div>
          `).join('');
        }

        if (data.trades && data.trades.length > 0) {
          const tbody = document.getElementById('trade-table');
          tbody.innerHTML = data.trades.map(t => `
            <tr>
              <td>${t.time}</td>
              <td><span class="badge ${t.side === 'LONG' ? 'badge-long' : 'badge-short'}">${t.side}</span></td>
              <td><span class="badge ${t.result === 'WIN' ? 'badge-long' : (t.result === 'BREAKEVEN' ? 'badge-flat' : 'badge-short')}">${t.type}</span></td>
              <td>$${t.entry.toLocaleString()}</td>
              <td>$${t.exit.toLocaleString()}</td>
              <td>${t.units}</td>
              <td class="${t.result === 'WIN' ? 'win' : (t.result === 'BREAKEVEN' ? 'mono' : 'loss')}">${t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}</td>
              <td class="${t.result === 'WIN' ? 'win' : (t.result === 'BREAKEVEN' ? 'mono' : 'loss')}">${t.return_pct >= 0 ? '+' : ''}${t.return_pct.toFixed(2)}%</td>
              <td class="${t.result === 'WIN' ? 'win' : (t.result === 'BREAKEVEN' ? 'mono' : 'loss')}">${t.result}</td>
            </tr>
          `).join('');
        }
      } catch (err) {
        console.error("Dashboard sync error:", err);
      }
    }

    setInterval(syncTerminal, 3000);
    syncTerminal();
  </script>
</body>
</html>
"""

# ---------------------------------------------------------
# HTTP SERVER ROUTING
# ---------------------------------------------------------
class QuantDashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif self.path == "/api/state":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with state_lock:
                serialized = json.dumps(BOT_STATE)
            self.wfile.write(serialized.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return

def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

def start_web_server(port: int):
    server = ThreadingHTTPServer(("127.0.0.1", port), QuantDashboardHandler)
    server.serve_forever()

# ---------------------------------------------------------
# QUANT LOOP
# ---------------------------------------------------------
def quant_loop():
    logging.info(f"Binance Futures Quant loop active on {SYMBOL} [{TIMEFRAME}]. Polling every {SLEEP_INTERVAL_SECONDS}s.")
    while True:
        logging.info("📡 Fetching latest Binance Futures candles...")
        metrics = fetch_futures_market_snapshot()
        
        if not metrics:
            logging.warning("⚠️ Failed to obtain market metrics. Retrying in next cycle...")
        else:
            with state_lock:
                BOT_STATE["market"]["price"] = metrics["current_price"]
                BOT_STATE["market"]["ema_20"] = metrics["ema_20"]
                BOT_STATE["market"]["ema_50"] = metrics["ema_50"]
                BOT_STATE["market"]["ema_200"] = metrics["ema_200"]
                BOT_STATE["market"]["rsi_14"] = metrics["rsi_14"]
                BOT_STATE["market"]["atr_14"] = metrics["atr_14"]
                BOT_STATE["market"]["vol_ratio"] = metrics["volume_ratio"]
                current_portfolio = dict(BOT_STATE["portfolio"])

            decision = consult_qwen(metrics, current_portfolio)
            
            if not decision:
                logging.warning("⚠️ Model did not return a valid decision this cycle. Holding current state.")
            else:
                with state_lock:
                    BOT_STATE["model"]["regime"] = decision.get("market_regime", "UNKNOWN")
                    BOT_STATE["model"]["action"] = decision.get("action", "HOLD")
                    BOT_STATE["model"]["confidence"] = float(decision.get("confidence", 0.0))
                    BOT_STATE["model"]["thesis"] = decision.get("thesis", "Evaluating setup.")
                    BOT_STATE["system"]["last_update"] = datetime.now().strftime("%H:%M:%S")

                commentary = decision.get("copilot_commentary") or decision.get("thesis")
                if commentary:
                    add_copilot_thought(commentary, category="ANALYSIS")

                execute_trade_logic(metrics, decision)

                logging.info(f"✅ [{datetime.now().strftime('%H:%M:%S')}] BTC Futures: ${metrics['current_price']:,.2f} | "
                             f"Regime: {BOT_STATE['model']['regime']} | "
                             f"Action: {BOT_STATE['model']['action']} ({int(BOT_STATE['model']['confidence']*100)}%)")

        time.sleep(SLEEP_INTERVAL_SECONDS)

# ---------------------------------------------------------
# APPLICATION ENTRYPOINT
# ---------------------------------------------------------
if __name__ == "__main__":
    load_historical_data()

    free_port = get_free_port()
    BOT_STATE["system"]["port"] = free_port

    server_thread = threading.Thread(target=start_web_server, args=(free_port,), daemon=True)
    server_thread.start()

    dashboard_url = f"http://localhost:{free_port}"
    print("\n" + "="*65)
    print("   ⚡ CRYPTORIO 5.0 | BINANCE FUTURES DESK ACTIVATED")
    print(f"   📊 Live Trading Terminal: {dashboard_url}")
    print("="*65 + "\n", flush=True)

    webbrowser.open(dashboard_url)

    try:
        quant_loop()
    except KeyboardInterrupt:
        print("\n[!] Shutting down futures desk cleanly...", flush=True)
