import os
import sys
import requests
import pandas as pd

# Load credentials from GitHub Actions secrets
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL             = "XAUUSDT"
INTERVAL           = "15m"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("[-] Missing Telegram environment variables.")
    sys.exit(1)

def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload, timeout=10)

# Fetch latest candles
url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=100"
res = requests.get(url, timeout=10).json()

df = pd.DataFrame(res, columns=[
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"
])
df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)

# Indicators
df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

hh14 = df["high"].rolling(14).max()
ll14 = df["low"].rolling(14).min()
df["wpr"] = ((hh14 - df["close"]) / (hh14 - ll14)) * -100.0

# iloc[-2] is the completed candle; iloc[-3] is the preceding candle
c_dip  = df.iloc[-2]
c_prev = df.iloc[-3]

# Larry Williams 9 EMA setup criteria
trend_ok = (c_dip["close"] > c_dip["ema50"]) and (c_dip["ema9"] > c_dip["ema50"])
wpr_ok   = c_dip["wpr"] <= -60.0
is_dip   = (c_dip["low"] < c_dip["ema9"]) and (c_prev["close"] > c_prev["ema9"])

if trend_ok and wpr_ok and is_dip:
    trigger_high = c_dip["high"]
    stop_loss    = c_dip["low"]
    risk         = trigger_high - stop_loss
    take_profit  = trigger_high + (risk * 2.0)

    alert_msg = (
        f"⚡ *LARRY WILLIAMS 9-EMA ALERT* ⚡\n\n"
        f"• *Asset:* `{SYMBOL}` (15m)\n"
        f"• *Buy Stop Trigger:* `${trigger_high:,.2f}`\n"
        f"• *Stop Loss:* `${stop_loss:,.2f}`\n"
        f"• *Target (1:2 R:R):* `${take_profit:,.2f}`\n"
        f"• *Williams %R:* `{c_dip['wpr']:.1f}`\n\n"
        f"📌 Place Buy Stop above `${trigger_high:,.2f}`. Cancel if unfilled in 3 bars."
    )
    send_telegram_alert(alert_msg)
    print("Condition met: Alert sent to Telegram.")
else:
    print("Scan complete: No setup detected on the last closed candle.")
