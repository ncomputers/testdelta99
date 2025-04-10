import time
import logging
import threading
from exchange import DeltaExchangeClient
import config
from trade_manager import TradeManager
import binance_ws  # For live price updates via WebSocket

logger = logging.getLogger(__name__)

class ProfitTrailing:
    def __init__(self, check_interval=1):
        """
        Initialize the ProfitTrailing tracker.
        :param check_interval: Frequency (in seconds) to evaluate trailing conditions.
        """
        self.client = DeltaExchangeClient()
        self.trade_manager = TradeManager()
        self.check_interval = check_interval
        self.position_trailing_stop = {}  # Cache: order_id -> trailing stop price
        self.last_had_positions = True
        self.last_position_fetch_time = 0
        self.position_fetch_interval = 5  # Seconds between fetching positions
        self.cached_positions = []
        self.last_display = {}

    def fetch_open_positions(self):
        """
        Fetch open positions for BTCUSD with non-zero size.
        Returns a list of positions.
        """
        try:
            positions = self.client.fetch_positions()
            open_positions = []
            for pos in positions:
                size = pos.get('size') or pos.get('contracts') or 0
                try:
                    size = float(size)
                except Exception:
                    size = 0.0
                if size != 0:
                    pos_symbol = (pos.get('info', {}).get('product_symbol') or pos.get('symbol'))
                    if pos_symbol and "BTCUSD" in pos_symbol:
                        open_positions.append(pos)
            return open_positions
        except Exception as e:
            logger.error("Error fetching open positions: %s", e)
            return []

    def compute_profit_pct(self, pos, live_price):
        """
        Compute profit percentage for a given position relative to live price.
        """
        entry = pos.get('entryPrice') or pos.get('entry_price') or pos.get('info', {}).get('entry_price')
        try:
            entry = float(entry)
        except Exception:
            return None
        size = pos.get('size') or pos.get('contracts') or 0
        try:
            size = float(size)
        except Exception:
            size = 0.0
        if size > 0:
            return (live_price - entry) / entry
        else:
            return (entry - live_price) / entry

    def get_trailing_config(self, profit_pct):
        """
        Determine applicable trailing configuration based on the current profit percentage.
        """
        conf = config.PROFIT_TRAILING_CONFIG
        if profit_pct < conf["start_trailing_profit_pct"]:
            return None
        applicable = None
        for level in conf["levels"]:
            if profit_pct >= level["min_profit_pct"]:
                applicable = level
        return applicable

    def update_trailing_stop(self, pos, live_price):
        """
        Calculate and update the trailing stop for a position.
        
        If the maximum profit exceeds 1000 points and the current profit is positive,
          For long positions: new SL = entry + (max_profit_so_far / 2)
          For short positions: new SL = entry - (max_profit_so_far / 2)
        (Once moved in a favorable direction, the SL is locked and never reverses.)
        
        Otherwise, the default stop is used:
          For long: default SL = entry - 500
          For short: default SL = entry + 500
        
        Returns:
            tuple: (new_trailing_stop, profit_ratio, rule)
        """
        order_id = pos.get('id')
        # Retrieve entry price from various possible fields.
        entry = pos.get('entryPrice') or pos.get('entry_price') or pos.get('info', {}).get('entry_price')
        try:
            entry = float(entry)
        except Exception:
            return None, None, None

        # Get position size.
        size = pos.get('size') or pos.get('contracts') or 0
        try:
            size = float(size)
        except Exception:
            size = 0.0

        # Calculate current profit (with sign).
        if size > 0:
            current_profit = live_price - entry
        else:
            current_profit = entry - live_price

        # Track maximum profit achieved (in absolute points) for this position.
        if not hasattr(self, 'position_max_profit'):
            self.position_max_profit = {}
        prev_max = self.position_max_profit.get(order_id, 0)
        new_max_profit = max(prev_max, current_profit)
        self.position_max_profit[order_id] = new_max_profit

        # Apply lock-50% rule only if new_max_profit exceeds 1000 points and current profit is positive.
        if new_max_profit > 1000 and current_profit > 0:
            if size > 0:
                lock_sl = entry + new_max_profit / 2
            else:
                lock_sl = entry - new_max_profit / 2

            stored_trailing = self.position_trailing_stop.get(order_id)
            if stored_trailing is not None:
                # For longs, ensure SL only moves upward; for shorts, only downward.
                if size > 0:
                    new_trailing = max(stored_trailing, lock_sl)
                else:
                    new_trailing = min(stored_trailing, lock_sl)
            else:
                new_trailing = lock_sl

            self.position_trailing_stop[order_id] = new_trailing
            return new_trailing, new_max_profit / entry, "lock_50"

        # Otherwise, use the fixed numeric offset as the default stop loss.
        if size > 0:
            default_sl = entry - 500
        else:
            default_sl = entry + 500

        stored_trailing = self.position_trailing_stop.get(order_id)
        if stored_trailing is not None:
            if size > 0:
                # For long positions, choose the higher stop (it can only move upward).
                new_trailing = max(stored_trailing, default_sl)
            else:
                # For short positions, choose the lower stop (it can only move downward).
                new_trailing = min(stored_trailing, default_sl)
        else:
            new_trailing = default_sl

        self.position_trailing_stop[order_id] = new_trailing
        return new_trailing, current_profit / entry, "fixed_stop"






    def compute_raw_profit(self, pos, live_price):
        """
        Compute raw profit value from the position and live price.
        """
        entry = pos.get('entryPrice') or pos.get('entry_price') or pos.get('info', {}).get('entry_price')
        try:
            entry = float(entry)
        except Exception:
            return None
        size = pos.get('size') or pos.get('contracts') or 0
        try:
            size = float(size)
        except Exception:
            size = 0.0
        if size > 0:
            return (live_price - entry) * size
        else:
            return (entry - live_price) * abs(size)

    def book_profit(self, pos, live_price):
        """
        Check whether the current live price has crossed the trailing stop.
        If so, place a forced market order to close the position.
        Returns True if a closing order was placed.
        """
        order_id = pos.get('id')
        size = pos.get('size') or pos.get('contracts') or 0
        try:
            size = float(size)
        except Exception:
            size = 0.0

        trailing_stop, profit_ratio, rule = self.update_trailing_stop(pos, live_price)

        # Use force=True to bypass pending order checks for immediate closure.
        if rule in ["dynamic", "fixed_stop", "lock_50"]:
            if size > 0 and live_price < trailing_stop:
                close_order = self.trade_manager.place_market_order("BTCUSD", "sell", size,
                                                                      params={"time_in_force": "ioc"},
                                                                      force=True)
                logger.info("Trailing stop triggered for long order %s. Closing position. Close order: %s", order_id, close_order)
                return True
            elif size < 0 and live_price > trailing_stop:
                close_order = self.trade_manager.place_market_order("BTCUSD", "buy", abs(size),
                                                                      params={"time_in_force": "ioc"},
                                                                      force=True)
                logger.info("Trailing stop triggered for short order %s. Closing position. Close order: %s", order_id, close_order)
                return True
        elif rule == "partial_booking":
            try:
                bracket_params = {
                    "bracket_stop_loss_limit_price": str(trailing_stop),
                    "bracket_stop_loss_price": str(trailing_stop),
                    "bracket_stop_trigger_method": "last_traded_price"
                }
                updated_order = self.trade_manager.order_manager.attach_bracket_to_order(
                    order_id=order_id,
                    product_id=27,
                    product_symbol="BTCUSD",
                    bracket_params=bracket_params
                )
                logger.info("Bracket updated for partial booking: %s", updated_order)
            except Exception as e:
                logger.error("Error updating bracket for partial booking: %s", e)
            return False
        return False


    def track(self):
        """
        Main loop to track profit trailing.
        Starts the live price feed and periodically evaluates open positions,
        updating trailing stops and booking profit when conditions are met.
        """
        # Start the Binance WebSocket in a separate thread.
        thread = threading.Thread(target=binance_ws.run_in_thread, daemon=True)
        thread.start()

        # Wait for live price to become available.
        wait_time = 0
        while binance_ws.current_price is None and wait_time < 30:
            logger.info("Waiting for live price update...")
            time.sleep(2)
            wait_time += 2

        if binance_ws.current_price is None:
            logger.warning("Live price not available. Exiting profit trailing tracker.")
            return

        while True:
            current_time = time.time()
            # Refresh position data periodically.
            if current_time - self.last_position_fetch_time >= self.position_fetch_interval:
                self.cached_positions = self.fetch_open_positions()
                self.last_position_fetch_time = current_time
                if not self.cached_positions:
                    self.position_trailing_stop.clear()

            live_price = binance_ws.current_price
            if live_price is None:
                continue

            open_positions = self.cached_positions
            if not open_positions:
                if self.last_had_positions:
                    logger.info("No open positions. Profit trailing paused.")
                    self.last_had_positions = False
                self.position_trailing_stop.clear()
            else:
                if not self.last_had_positions:
                    logger.info("Open positions detected. Profit trailing resumed.")
                    self.last_had_positions = True

                for pos in open_positions:
                    order_id = pos.get('id')
                    try:
                        size = float(pos.get('size') or pos.get('contracts') or 0)
                    except Exception:
                        size = 0.0
                    if size == 0:
                        continue

                    entry = pos.get('entryPrice') or pos.get('entry_price') or pos.get('info', {}).get('entry_price')
                    try:
                        entry_val = float(entry)
                    except Exception:
                        entry_val = None

                    profit_pct = self.compute_profit_pct(pos, live_price)
                    profit_display = profit_pct * 100 if profit_pct is not None else None
                    raw_profit = self.compute_raw_profit(pos, live_price)
                    # For display purposes; you may adjust the conversion factors as needed.
                    profit_usd = raw_profit / 1000 if raw_profit is not None else None
                    trailing_stop, _, rule = self.update_trailing_stop(pos, live_price)

                    display = {
                        "entry": entry_val,
                        "live": live_price,
                        "profit": round(profit_display or 0, 2),
                        "usd": round(profit_usd or 0, 2),
                        "rule": rule,
                        "sl": round(trailing_stop or 0, 2)
                    }

                    if self.last_display.get(order_id) != display:
                        logger.info(
                            f"Order: {order_id} | Entry: {entry_val:.1f} | Live: {live_price:.1f} | "
                            f"PnL: {profit_display:.2f}% | USD: {profit_usd:.2f} | Rule: {rule} | SL: {trailing_stop:.1f}"
                        )
                        self.last_display[order_id] = display

                    if self.book_profit(pos, live_price):
                        logger.info(f"Profit booked for order {order_id}.")

            time.sleep(self.check_interval)

if __name__ == '__main__':
    pt = ProfitTrailing(check_interval=1)
    pt.track()
