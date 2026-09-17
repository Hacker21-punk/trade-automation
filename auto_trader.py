import time
import math
import pandas as pd
from binance.um_futures import UMFutures

# ================= CONFIGURATION =================
API_KEY    = "p16doIsjFzvqXfrrGBtZK4xa4hB9WNczWzCgUuOeD3CNu3sPT6gCjkKOZZfp8YuH"
API_SECRET = "hLnSFAPZB2zqyGJSYqB6DjIr8cpgiJzPIia5O17QXbsAGU3lqXbJqIhOFu7g9qsA"

# Set base_url to testnet for safe practice
BASE_URL   = "https://testnet.binancefuture.com"
SYMBOL     = "XAUUSDT"   # Or "BTCUSDT" on testnet if Gold perpetual is unavailable
INTERVAL   = "15m"
RISK_PCT   = 0.01        # Risk exactly 1% of equity per trade
MAX_BARS   = 3           # Cancel entry if unfilled after 3 bars
# =================================================

client = UMFutures(key=API_KEY, secret=API_SECRET, base_url=BASE_URL)

active_order_id = None
order_placed_bar = None

def get_market_data():
    raw = client.klines(symbol=SYMBOL, interval=INTERVAL, limit=100)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tb_base", "tb_quote", "ignore"
    ])
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    
    # EMAs & %R
    df["ema9"]  = df["close"].ewm(span=9, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    hh14        = df["high"].rolling(14).max()
    ll14        = df["low"].rolling(14).min()
    df["wpr"]   = ((hh14 - df["close"]) / (hh14 - ll14)) * -100.0
    return df

def get_account_balance():
    balances = client.balance()
    for item in balances:
        if item["asset"] == "USDT":
            return float(item["availableBalance"])
    return 0.0

def cancel_pending_order():
    global active_order_id, order_placed_bar
    if active_order_id:
        try:
            client.cancel_order(symbol=SYMBOL, orderId=active_order_id)
            print(f"[-] Stale order {active_order_id} canceled.")
        except Exception as e:
            print(f"[-] Order cancel info: {e}")
        active_order_id = None
        order_placed_bar = None

def execute_strategy():
    global active_order_id, order_placed_bar
    
    df = get_market_data()
    c_dip  = df.iloc[-2]
    c_prev = df.iloc[-3]
    current_bar_index = len(df)

    # 1. Cancel unfulfilled setup if 3 bars have passed
    if active_order_id and (current_bar_index - order_placed_bar >= MAX_BARS):
        cancel_pending_order()

    # 2. Check position status (avoid stacking trades)
    positions = client.get_position_risk(symbol=SYMBOL)
    in_position = any(float(p["positionAmt"]) != 0.0 for p in positions)
    if in_position or active_order_id:
        return

    # 3. Larry Williams Rules
    trend_ok = (c_dip["close"] > c_dip["ema50"]) and (c_dip["ema9"] > c_dip["ema50"])
    wpr_ok   = c_dip["wpr"] <= -60.0
    is_dip   = (c_dip["low"] < c_dip["ema9"]) and (c_prev["close"] > c_prev["ema9"])

    if trend_ok and wpr_ok and is_dip:
        trigger_price = round(c_dip["high"], 2)
        stop_loss     = round(c_dip["low"], 2)
        risk_per_unit = trigger_price - stop_loss
        
        if risk_per_unit <= 0:
            return

        # 4. Strict Risk-Based Position Sizing
        balance = get_account_balance()
        dollar_risk = balance * RISK_PCT
        quantity = round(dollar_risk / risk_per_unit, 3)

        if quantity <= 0:
            print("[-] Insufficient balance for minimum lot size.")
            return

        print(f"[+] Signal found! Placing Buy Stop at {trigger_price}, SL: {stop_loss}")

        # 5. Send Conditional Stop Entry Order
        order = client.new_order(
            symbol=SYMBOL,
            side="BUY",
            type="STOP_MARKET",
            stopPrice=trigger_price,
            quantity=quantity
        )
        active_order_id = order["orderId"]
        order_placed_bar = current_bar_index

print(f"[*] Automated Execution Engine Live on {SYMBOL} ({INTERVAL}). Testing Mode...")
while True:
    try:
        execute_strategy()
    except Exception as err:
        print(f"[-] Execution error: {err}")
    time.sleep(30)
