from binance import AsyncClient, BinanceSocketManager
import pandas as pd
import numpy as np
from collections import deque
import asyncio

# ======== CONFIGURATION ========
SYMBOL = 'BTCUSDT'
POSITION_SIZE = 0.01  # BTC
ORDERBOOK_DEPTH = 5
TRADE_WINDOW = 15  # seconds
SKEW_INTERVALS = 3
SKEW_INTERVAL_DURATION = 5  # seconds

# Thresholds from enhanced framework
ASK_BID_RATIO_THRESHOLD = 1.5
BID_COLLAPSE_PCT_THRESHOLD = 0.4
AGGRESSIVE_SELL_RATIO_THRESHOLD = 2.5
NET_SELL_DELTA_THRESHOLD = 0.4
EXIT_VOLUME_RATIO_THRESHOLD = 2.0

class FuturesTradingBot:
    def __init__(self):
        self.client = None
        self.orderbook = {'bids': [], 'asks': []}
        self.trade_history = deque(maxlen=1000)
        self.position_open = False
        self.bid_volume_history = deque(maxlen=10)
        self.skew_windows = deque(maxlen=SKEW_INTERVALS)
        self.volume_profile = {'buy': deque(maxlen=1000), 'sell': deque(maxlen=1000)}
        self.lock = asyncio.Lock()
        self.last_trade_time = 0
        
    async def initialize(self):
        self.client = await AsyncClient.create()
        print("Bot initialized")

    async def safe_feed(self, coro_func):
        try:
            await coro_func()
        except Exception as e:
            print(f"Feed error: {e}. Restarting in 5 seconds...")
            await asyncio.sleep(5)
            asyncio.create_task(self.safe_feed(coro_func))

    async def start_orderbook_feed(self):
        bm = BinanceSocketManager(self.client)
        ts = bm.futures_depth_socket(SYMBOL, depth=10)
        async with ts as tscm:
            while True:
                res = await tscm.recv()
                async with self.lock:
                    self.process_orderbook(res)

    async def start_trade_feed(self):
        bm = BinanceSocketManager(self.client)
        ts = bm.futures_trade_socket(SYMBOL)
        async with ts as tscm:
            while True:
                res = await tscm.recv()
                async with self.lock:
                    self.process_trade(res)

    def process_orderbook(self, data):
        # Process orderbook snapshot and updates
        bids = sorted([(float(p), float(q)) for p, q in data['b']], reverse=True)[:ORDERBOOK_DEPTH]
        asks = sorted([(float(p), float(q)) for p, q in data['a']])[:ORDERBOOK_DEPTH]
        
        # Store current bid volume for collapse detection
        current_bid_vol = sum(q for _, q in bids)
        self.bid_volume_history.append(current_bid_vol)
        
        self.orderbook = {'bids': bids, 'asks': asks}

    def process_trade(self, data):
        # Update last trade time from exchange data
        self.last_trade_time = data['T']
        
        # Classify aggressive trades
        trade = {
            'price': float(data['p']),
            'qty': float(data['q']),
            'time': data['T'],
            'aggressive': self.is_aggressive_trade(float(data['p']), data['m'])
        }
        self.trade_history.append(trade)
        self.update_volume_profile(trade)

    def is_aggressive_trade(self, price, is_buyer_maker):
        # Determine if trade is aggressive (market taker)
        if is_buyer_maker:
            return 'sell'  # Seller hit bid
        else:
            return 'buy'   # Buyer hit ask

    def update_volume_profile(self, trade):
        if trade['aggressive'] == 'buy':
            self.volume_profile['buy'].append(trade)
        else:
            self.volume_profile['sell'].append(trade)

    # ======== SIGNAL DETECTION ========
    def detect_orderbook_imbalance(self):
        """Check orderbook thresholds from framework"""
        bid_vol = sum(q for _, q in self.orderbook['bids'])
        ask_vol = sum(q for _, q in self.orderbook['asks'])
        
        # Ask-side thickening detection
        if ask_vol > 0 and bid_vol > 0:
            ask_bid_ratio = ask_vol / bid_vol
            if ask_bid_ratio > ASK_BID_RATIO_THRESHOLD:
                print(f"Ask thickening detected: {ask_bid_ratio:.2f}")
                return True
        
        # Bid-side collapse detection
        if len(self.bid_volume_history) >= 4:
            current = self.bid_volume_history[-1]
            prev = self.bid_volume_history[-4]  # 3 seconds ago
            if prev > 0:
                pct_change = (prev - current) / prev
                if pct_change > BID_COLLAPSE_PCT_THRESHOLD:
                    print(f"Bid collapse detected: {pct_change:.2%}")
                    return True
        return False

    def detect_trade_delta_skew(self):
        """Implement trade delta analysis with duration requirement"""
        # Check skew in latest interval
        current_skew = self.calculate_current_skew()
        self.skew_windows.append(current_skew)
        
        # Check if we have enough intervals
        if len(self.skew_windows) < SKEW_INTERVALS:
            return False
        
        # Count valid skew periods
        valid_count = 0
        for sell_ratio, net_delta in self.skew_windows:
            if sell_ratio > AGGRESSIVE_SELL_RATIO_THRESHOLD:
                valid_count += 1
            elif net_delta > NET_SELL_DELTA_THRESHOLD:
                valid_count += 1
        
        return valid_count >= 2  # 2 out of 3 conditions met

    def calculate_current_skew(self):
        """Calculate metrics for current time window using exchange timestamps"""
        if not self.last_trade_time:
            return (0, 0)
            
        window_start = self.last_trade_time - (SKEW_INTERVAL_DURATION * 1000)
        
        # Filter recent trades
        recent_trades = [t for t in self.trade_history 
                         if t['time'] >= window_start]
        
        # Calculate aggressive volumes
        agg_sell = sum(t['qty'] for t in recent_trades if t['aggressive'] == 'sell')
        agg_buy = sum(t['qty'] for t in recent_trades if t['aggressive'] == 'buy')
        total_vol = agg_sell + agg_buy
        
        # Compute metrics safely
        if agg_buy > 0 and total_vol > 0:
            sell_ratio = agg_sell / agg_buy
            net_delta = (agg_sell - agg_buy) / total_vol
            return (sell_ratio, net_delta)
        return (0, 0)

    # ======== TRADING LOGIC ========
    async def check_entry_conditions(self):
        """Enhanced entry logic with thresholds"""
        ob_imbalance = self.detect_orderbook_imbalance()
        trade_skew = self.detect_trade_delta_skew()
        
        if ob_imbalance and trade_skew:
            print("Entry conditions met - opening short position")
            await self.open_position('SHORT')

    async def check_exit_conditions(self):
        """Spoof-resistant exit logic"""
        # Bullish reversal signals (simplified)
        reversal_signals = self.check_reversal_signals()
        
        # Volume confirmation
        vol_confirmed = self.check_volume_support()
        
        # Anti-spoof verification
        no_spoof = self.verify_no_spoof()
        
        if reversal_signals >= 2 and vol_confirmed and no_spoof:
            print("Exit conditions met - closing position")
            await self.close_position()

    def check_reversal_signals(self):
        """Check technical reversal signals (stub implementation)"""
        # Implement actual VWAP reclaim, RSI, MACD logic here
        return 2  # Placeholder

    def check_volume_support(self):
        """Confirm genuine volume support"""
        # Calculate recent buy volume vs historical average
        if not self.volume_profile['buy']:
            return False
            
        recent_buy_vol = sum(t['qty'] for t in list(self.volume_profile['buy'])[-60:])
        avg_buy_vol = np.mean([t['qty'] for t in list(self.volume_profile['buy'])[-300:]]) 
        
        if avg_buy_vol > 0:
            return recent_buy_vol / avg_buy_vol > EXIT_VOLUME_RATIO_THRESHOLD
        return False

    def verify_no_spoof(self):
        """Anti-manipulation checks for exit"""
        # Implement actual orderbook analysis for spoof detection
        return True  # Placeholder

    # ======== ORDER EXECUTION ========
    async def open_position(self, side):
        # Implement order placement with risk management
        print(f"Opening {side} position for {POSITION_SIZE}")
        self.position_open = True

    async def close_position(self):
        # Implement position closing logic
        print("Closing position")
        self.position_open = False

    # ======== MAIN LOOP ========
    async def run(self):
        await self.initialize()
        asyncio.create_task(self.safe_feed(self.start_orderbook_feed))
        asyncio.create_task(self.safe_feed(self.start_trade_feed))
        
        while True:
            async with self.lock:
                if not self.position_open:
                    await self.check_entry_conditions()
                else:
                    await self.check_exit_conditions()
            await asyncio.sleep(0.5)

if __name__ == "__main__":
    bot = FuturesTradingBot()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        loop.run_until_complete(bot.client.close_connection())
        loop.close() 
