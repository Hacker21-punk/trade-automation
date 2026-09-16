"""
SPY 200-day SMA trend-following bot (paper trading).

Design notes:
  - Entry and exit are BOTH the 200 SMA, with a symmetric band to damp whipsaw.
  - No take-profit. Trend following depends on the right tail; capping it inverts
    the payoff the strategy relies on.
  - A wide ATR-based stop exists only as a catastrophe brake, not as strategy.
  - Every external call is guarded. Nothing is swallowed by a bare except.
  - The run is idempotent: safe to execute twice in the same session.

This is a personal research/paper-trading project. It is not investment advice,
and I'm not a financial advisor. Do not point it at a live-money account until
you have backtested it yourself and understand its failure modes.
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.common.exceptions import APIError

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SYMBOL = "SPY"
SMA_WINDOW = 200
BAND_PCT = 0.005          # 0.5% dead-band around the SMA, both directions
TARGET_EXPOSURE = 0.95    # fraction of equity to deploy when in a trend
CATASTROPHE_ATR_MULT = 5.0  # disaster stop distance, in ATR(14) units
MINUTES_BEFORE_CLOSE = 45   # only act inside this window before the closing bell
PAPER = True                # NEVER flip this without a full backtest first

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("bot")


class Abort(Exception):
    """Raised for an expected, non-error reason to stop without trading."""


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
def build_clients():
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in environment.")

    # paper=True sets the correct base URL internally. Do not hand-write a URL.
    trading = TradingClient(key, secret, paper=PAPER)
    data = StockHistoricalDataClient(key, secret)

    # Fail loudly and immediately if credentials are bad. This is the check the
    # original script was missing -- a 404 was being read as "no position".
    acct = trading.get_account()
    log.info(
        "Authenticated. account=%s status=%s equity=%s buying_power=%s",
        acct.account_number, acct.status, acct.equity, acct.buying_power,
    )
    if acct.trading_blocked or acct.account_blocked:
        raise Abort("Account is blocked from trading.")
    return trading, data, acct


# --------------------------------------------------------------------------
# Timing guards
# --------------------------------------------------------------------------
def check_market_window(trading):
    """Only trade on a real session, inside the pre-close window."""
    clock = trading.get_clock()
    now_et = clock.timestamp.astimezone(ET)

    if not clock.is_open:
        raise Abort(f"Market closed at {now_et:%Y-%m-%d %H:%M %Z}. Nothing to do.")

    minutes_left = (clock.next_close - clock.timestamp).total_seconds() / 60
    if minutes_left > MINUTES_BEFORE_CLOSE:
        raise Abort(
            f"{minutes_left:.0f} min to close; waiting for the "
            f"final {MINUTES_BEFORE_CLOSE} min window."
        )
    log.info("In trading window: %.0f min to close (%s ET).", minutes_left, f"{now_et:%H:%M}")
    return clock


def already_acted_today(trading, clock):
    """
    Idempotency guard. If this symbol already has an order from the current
    session, a duplicate run must not fire a second one.
    """
    session_open = clock.next_close - timedelta(hours=24)
    try:
        orders = trading.get_orders(
            GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                symbols=[SYMBOL],
                after=session_open,
                limit=50,
            )
        )
    except APIError as e:
        raise RuntimeError(f"Could not read order history: {e}") from e

    live = [o for o in orders if o.status.value not in ("canceled", "expired", "rejected")]
    if live:
        raise Abort(
            f"{len(live)} order(s) already placed for {SYMBOL} this session "
            f"(latest: {live[0].side.value} {live[0].qty} @ {live[0].status.value}). "
            "Refusing to duplicate."
        )


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def load_bars(data):
    """
    Pull enough daily bars for a 200 SMA plus slack for holidays.
    adjustment=ALL keeps splits/dividends from creating fake crossovers.
    """
    start = datetime.now(timezone.utc) - timedelta(days=int(SMA_WINDOW * 2.2))
    try:
        bars = data.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=SYMBOL,
                timeframe=TimeFrame.Day,
                start=start,
                adjustment=Adjustment.ALL,
                feed=DataFeed.IEX,   # free tier. Switch to SIP if you subscribe.
            )
        )
    except APIError as e:
        raise RuntimeError(f"Bar request failed: {e}") from e

    df = bars.df
    if df.empty:
        raise RuntimeError("Alpaca returned zero bars.")
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(SYMBOL, level="symbol")
    df = df.sort_index()

    if len(df) < SMA_WINDOW + 20:
        raise RuntimeError(f"Only {len(df)} bars; need at least {SMA_WINDOW + 20}.")

    # Staleness guard: a silently stale feed is worse than a missing one.
    last_bar_date = df.index[-1].date()
    age_days = (datetime.now(ET).date() - last_bar_date).days
    if age_days > 5:
        raise RuntimeError(f"Last bar is {age_days} days old ({last_bar_date}). Feed looks stale.")

    return df


def compute_signals(df):
    close = df["close"]
    sma = close.rolling(SMA_WINDOW).mean()

    # True Range -> ATR(14), used only for the catastrophe stop.
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean()

    price = float(close.iloc[-1])
    sma_now = float(sma.iloc[-1])
    atr_now = float(atr.iloc[-1])

    if any(pd.isna(x) for x in (price, sma_now, atr_now)):
        raise RuntimeError("NaN in computed indicators.")

    log.info(
        "%s: price=%.2f sma%d=%.2f (%+.2f%%) atr14=%.2f",
        SYMBOL, price, SMA_WINDOW, sma_now, (price / sma_now - 1) * 100, atr_now,
    )
    return price, sma_now, atr_now


# --------------------------------------------------------------------------
# Position
# --------------------------------------------------------------------------
def get_position(trading):
    """Returns the position object, or None. Distinguishes 404 from real errors."""
    try:
        return trading.get_open_position(SYMBOL)
    except APIError as e:
        if getattr(e, "status_code", None) == 404 or "position does not exist" in str(e).lower():
            return None
        raise RuntimeError(f"Position lookup failed: {e}") from e


def target_quantity(equity, price):
    qty = int((float(equity) * TARGET_EXPOSURE) // price)
    return max(qty, 0)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run():
    trading, data, acct = build_clients()
    clock = check_market_window(trading)
    already_acted_today(trading, clock)

    df = load_bars(data)
    price, sma, atr = compute_signals(df)
    position = get_position(trading)

    upper = sma * (1 + BAND_PCT)   # must clear this to enter
    lower = sma * (1 - BAND_PCT)   # must break this to exit

    if position is None:
        if price > upper:
            qty = target_quantity(acct.equity, price)
            if qty < 1:
                raise Abort(f"Equity {acct.equity} insufficient for 1 share at {price:.2f}.")
            log.info("ENTRY: %.2f > upper band %.2f. Buying %d shares.", price, upper, qty)
            order = trading.submit_order(
                MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            log.info("Order %s submitted (%s).", order.id, order.status.value)
            log.info(
                "Catastrophe reference: %.2f (%.1f x ATR below entry). "
                "Primary exit remains the SMA band.",
                price - CATASTROPHE_ATR_MULT * atr, CATASTROPHE_ATR_MULT,
            )
        else:
            log.info("FLAT and price %.2f <= upper band %.2f. No entry.", price, upper)
        return

    # --- We hold a position ---
    qty_held = abs(int(float(position.qty)))
    unrealized = float(position.unrealized_plpc) * 100
    log.info("Holding %d shares, unrealized %+.2f%%.", qty_held, unrealized)

    catastrophe = float(position.avg_entry_price) - CATASTROPHE_ATR_MULT * atr

    if price < lower:
        reason = f"price {price:.2f} broke lower band {lower:.2f}"
    elif price < catastrophe:
        reason = f"catastrophe stop hit ({price:.2f} < {catastrophe:.2f})"
    else:
        log.info("Trend intact (%.2f >= %.2f). Holding.", price, lower)
        return

    log.info("EXIT: %s. Closing full position.", reason)
    order = trading.close_position(SYMBOL)
    log.info("Close order %s submitted (%s).", order.id, order.status.value)


if __name__ == "__main__":
    try:
        run()
    except Abort as e:
        log.info("No action: %s", e)
        sys.exit(0)          # expected no-op -> green build
    except Exception as e:
        log.error("FAILED: %s", e, exc_info=True)
        sys.exit(1)          # real failure -> red build -> GitHub emails you
