import os
import alpaca_trade_api as tradeapi

# 1. Pull API Keys securely from GitHub Secrets (Never hardcode real keys)
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = "https://alpaca.markets"

if not API_KEY or not SECRET_KEY:
    print("❌ Error: API keys are missing from environment variables.")
    exit(1)

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version='v2')

def run_trading_bot():
    symbol = "SPY"
    qty_to_trade = 10
    
    # 2. Fetch market data to compute the 200-day Simple Moving Average
    barset = api.get_bars(symbol, '1Day', limit=250).df
    current_price = float(barset['close'].iloc[-1])
    sma_200 = float(barset['close'].rolling(window=200).mean().iloc[-1])
    
    # 3. Check if we already own the asset
    try:
        position = api.get_position(symbol)
        holds_position = True
    except:
        holds_position = False

    # 4. Asymmetric Risk Calculations (Using a 2% Risk vs 6% Reward Blueprint)
    stop_loss_price = round(current_price * 0.98, 2)      # 2% Downside protection
    take_profit_price = round(current_price * 1.06, 2)    # 6% Upside target (3:1 Ratio)

    # 5. Core Execution Logic
    if current_price > sma_200 and not holds_position:
        print(f"📈 Price ({current_price}) is above 200 SMA ({round(sma_200, 2)}). Sending Bracket Order...")
        
        # Send an advanced order that sets stop/profit protections instantly
        api.submit_order(
            symbol=symbol,
            qty=qty_to_trade,
            side='buy',
            type='market',
            time_in_force='gtc',
            order_class='bracket',
            take_profit={'limit_price': take_profit_price},
            stop_loss={'stop_price': stop_loss_price}
        )
        print(f"✅ Ordered {qty_to_trade} shares of {symbol}. Stop: {stop_loss_price}, Target: {take_profit_price}")
        
    elif current_price < sma_200 and holds_position:
        print(f"⚠️ Trend broke down! Price ({current_price}) dropped below 200 SMA. Liquidating...")
        api.submit_order(
            symbol=symbol,
            qty=position.qty,
            side='sell',
            type='market',
            time_in_force='gtc'
        )
        print("✅ Position closed successfully.")
    else:
        print("😴 Conditions not met. No actions taken today.")

if __name__ == "__main__":
    run_trading_bot()
