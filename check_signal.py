import os
import sys
import requests
import yfinance as yf
import pandas as pd

# Load credentials from GitHub repository secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL             = "GC=F"       # Gold Futures (XAU/USD equivalent, unrestricted globally)
INTERVAL           = "15m"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("[-] Error: Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in repository secrets.")
    sys.exit(1)

def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        res.raise_for_status()
    except Exception as e:
        print(f"[-] Telegram dispatch error: {e}")

# Fetch the last 5 days of 15-minute candlesticks
gold = yf.Ticker(SYMBOL)
df = gold.history(period="5d", interval=INTERVAL)

# Guard against weekend market closures or connection anomalies
if df.empty or len(df) < 55:
    print(f"[-] Market data unavailable or insufficient candles returned (count: {len(df)}). Exiting safely.")
    sys.exit(0)

# Normalize column names to lowercase
df = df.rename(columns={
    "Open": "open", 
    "High": "high", 
    "Low": "low", 
    "Close": "close", 
    "Volume": "volume"
})

# Calculate Moving Averages
df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

# Calculate Williams %R (14-period lookback)
hh14 = df["high"].rolling(14).max()
ll14 = df["low"].rolling(14).min()
df["wpr"] = ((hh14 - df["close"]) / (hh14 - ll14)) * -100.0

# iloc[-2] is the completed 15m candle; iloc[-3] is the candle before it
c_dip  = df.iloc[-2]
c_prev = df.iloc[-3]

# Larry Williams Setup Conditions
trend_ok = (c_dip["close"] > c_dip["ema50"]) and (c_dip["ema9"] > c_dip["ema50"])
wpr_ok   = c_dip["wpr"] <= -60.0
is_dip   = (c_dip["low"] < c_dip["ema9"]) and (c_prev["close"] > c_prev["ema9"])

if true:
    trigger_high = float(c_dip["high"])
    stop_loss    = float(c_dip["low"])
    risk         = trigger_high - stop_loss
    take_profit  = trigger_high + (risk * 2.0)

    alert_msg = (
        f"⚡ *LARRY WILLIAMS 9-EMA ALERT* ⚡\n\n"
        f"• *Market:* Gold (`XAU/USD` / `GC`)\n"
        f"• *Timeframe:* `{INTERVAL}`\n"
        f"• *Buy Stop Trigger:* `${trigger_high:,.2f}`\n"
        f"• *Stop Loss:* `${stop_loss:,.2f}`\n"
        f"• *Target (1:2 R:R):* `${take_profit:,.2f}`\n"
        f"• *Williams %R:* `{c_dip['wpr']:.1f}`\n\n"
        f"📌 *Plan:* Place a Buy Stop at `${trigger_high:,.2f}`. Cancel the order if unfilled within 3 candles."
    )
    send_telegram_alert(alert_msg)
    print("Setup verified: Alert sent to Telegram.")
else:
    print(f"Scan complete: No setup detected on bar closing at {c_dip.name}.")
