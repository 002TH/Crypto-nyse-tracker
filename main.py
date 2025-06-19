import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

app = FastAPI()

# Configuration
TIMEZONE = "Africa/Lagos"
TRACKED_COINS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt", "trxusdt", "bnbusdt", "adausdt", "suiusdt", "pepeusdt"]
SOL_SYMBOL = "solusdt"

class Tracker:
    def __init__(self):
        self.tick_directions = {}
        self.prev_closes = {}
        self.last_15min_tick = None
        self.current_15min_ticks = []
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None
        self.sol_prev_close = None
        self.last_reset = datetime.now(pytz.timezone(TIMEZONE))
        self.last_trade_time = {}

    async def load_historical(self):
        """Fetch yesterday's close prices with retry logic"""
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            retries = 3
            while retries > 0:
                try:
                    data = requests.get(
                        f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}",
                        timeout=5
                    ).json()
                    self.prev_closes[symbol] = float(data["prevClosePrice"])
                    if symbol == SOL_SYMBOL:
                        self.sol_prev_close = float(data["prevClosePrice"])
                    break
                except Exception as e:
                    print(f"Failed to load data for {symbol}: {e}")
                    retries -= 1
                    await asyncio.sleep(1)

    async def run_15min_reset(self):
        """More robust 15-minute reset"""
        while True:
            now = datetime.now(pytz.timezone(TIMEZONE))
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())
            
            if self.current_15min_ticks:
                final_tick = self.current_15min_ticks[-1]
                self.last_15min_tick = "↑" if final_tick > 0 else "↓" if final_tick < 0 else "→"
                print(f"15min period closed at: {final_tick} {self.last_15min_tick}")  # Debug log
            self.current_15min_ticks = []

    def reset_if_new_day(self):
        now = datetime.now(pytz.timezone(TIMEZONE))
        if now.date() > self.last_reset.date():
            self.sol_buy_volume = 0.0
            self.sol_sell_volume = 0.0
            self.last_reset = now
            print("Daily reset completed")

    def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        symbol = symbol.lower()
        now = datetime.now(pytz.timezone(TIMEZONE))
        
        # Only process if we have fresh data (avoid stale prices)
        if symbol in self.last_trade_time and (now - self.last_trade_time[symbol]).total_seconds() < 60:
            if symbol in TRACKED_COINS:
                prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
                direction = 1 if price > prev_price else (-1 if price < prev_price else 0)
                self.tick_directions[symbol] = {"last_price": price, "direction": direction}
                
                current_tick = sum(data.get("direction", 0) for data in self.tick_directions.values())
                self.current_15min_ticks.append(current_tick)
                print(f"{symbol} trade: {price} ({'↑' if direction > 0 else '↓' if direction < 0 else '→'})")  # Debug log
            
            if symbol == SOL_SYMBOL:
                self.sol_last_price = price
                if is_buyer_maker:
                    self.sol_sell_volume += qty
                else:
                    self.sol_buy_volume += qty
        
        self.last_trade_time[symbol] = now

tracker = Tracker()

@app.on_event("startup")
async def startup():
    print("Starting up...")
    await tracker.load_historical()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.run_15min_reset())

async def track_live_data():
    """Improved WebSocket connection with heartbeat"""
    while True:
        try:
            print("Connecting to Binance WebSocket...")
            streams = [f"{s}@trade" for s in list(set(TRACKED_COINS + [SOL_SYMBOL]))]  # Remove duplicates
            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
            
            async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                print(f"Connected to: {streams}")
                async for msg in ws:
                    try:
                        data = json.loads(msg)['data']
                        tracker.process_trade(
                            symbol=data["s"],
                            price=float(data["p"]),
                            is_buyer_maker=data["m"],
                            qty=float(data["q"])
                        )
                    except KeyError as e:
                        print(f"Unexpected message format: {msg}")

        except Exception as e:
            print(f"WebSocket error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)

# ... [keep the rest of your code the same] ...