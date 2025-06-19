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
TRACKED_COINS = ["btcusdt", "ethusdt"]  # Start with fewer coins for testing
SOL_SYMBOL = "solusdt"
DEBUG = True

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

    def log(self, message):
        """Synchronous logging"""
        if DEBUG:
            now = datetime.now(pytz.timezone(TIMEZONE)).strftime("%H:%M:%S")
            print(f"[{now}] {message}")

    async def load_historical(self):
        """Async historical data loading"""
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            retries = 3
            while retries > 0:
                try:
                    self.log(f"Fetching historical data for {symbol}...")
                    data = requests.get(
                        f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}",
                        timeout=5
                    ).json()
                    self.prev_closes[symbol] = float(data["prevClosePrice"])
                    if symbol == SOL_SYMBOL:
                        self.sol_prev_close = float(data["prevClosePrice"])
                    self.log(f"{symbol} prev close: {self.prev_closes[symbol]}")
                    break
                except Exception as e:
                    self.log(f"Error loading {symbol}: {str(e)[:100]}... (retries left: {retries-1})")
                    retries -= 1
                    await asyncio.sleep(1)

    async def run_15min_reset(self):
        """Async 15-minute reset"""
        while True:
            now = datetime.now(pytz.timezone(TIMEZONE))
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            wait_seconds = (next_reset - now).total_seconds()
            self.log(f"Next 15min reset at {next_reset.strftime('%H:%M')} (in {wait_seconds:.1f}s)")
            await asyncio.sleep(wait_seconds)
            
            if self.current_15min_ticks:
                final_tick = self.current_15min_ticks[-1]
                self.last_15min_tick = "↑" if final_tick > 0 else "↓" if final_tick < 0 else "→"
                self.log(f"15min period closed: TICK={final_tick} {self.last_15min_tick}")
            else:
                self.log("15min period closed: No trades received")
            self.current_15min_ticks = []

    def reset_if_new_day(self):
        """Synchronous daily reset"""
        now = datetime.now(pytz.timezone(TIMEZONE))
        if now.date() > self.last_reset.date():
            self.log("Performing daily reset...")
            self.sol_buy_volume = 0.0
            self.sol_sell_volume = 0.0
            self.last_reset = now
            self.tick_directions = {}

    async def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        """Async trade processing"""
        symbol = symbol.lower()
        now = datetime.now(pytz.timezone(TIMEZONE))
        
        if symbol not in TRACKED_COINS + [SOL_SYMBOL]:
            self.log(f"Ignoring untracked symbol: {symbol}")
            return

        if symbol in self.last_trade_time and (now - self.last_trade_time[symbol]).total_seconds() > 2:
            self.log(f"Stale price for {symbol}, skipping...")
            return

        if symbol in TRACKED_COINS:
            prev_data = self.tick_directions.get(symbol, {"last_price": price, "direction": 0})
            direction = 1 if price > prev_data["last_price"] else (-1 if price < prev_data["last_price"] else 0)
            
            self.tick_directions[symbol] = {
                "last_price": price,
                "direction": direction
            }
            
            current_tick = sum(data["direction"] for data in self.tick_directions.values())
            self.current_15min_ticks.append(current_tick)
            
            self.log(f"{symbol} {'↑' if direction > 0 else '↓' if direction < 0 else '→'} {prev_data['last_price']}→{price} (TICK={current_tick})")

        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty
            self.log(f"SOL CVD: Buy={self.sol_buy_volume:.2f} Sell={self.sol_sell_volume:.2f}")

        self.last_trade_time[symbol] = now

tracker = Tracker()

@app.on_event("startup")
async def startup():
    tracker.log("🚀 Starting application...")
    await tracker.load_historical()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.run_15min_reset())

async def track_live_data():
    """Main WebSocket connection"""
    while True:
        try:
            tracker.log("🔌 Connecting to Binance WebSocket...")
            streams = [f"{s}@trade" for s in list(set(TRACKED_COINS + [SOL_SYMBOL]))]
            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
            
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                tracker.log(f"✅ Connected to {len(streams)} streams: {', '.join(streams)}")
                
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if 'data' in data:
                            trade = data['data']
                            await tracker.process_trade(
                                symbol=trade["s"],
                                price=float(trade["p"]),
                                is_buyer_maker=trade["m"],
                                qty=float(trade["q"])
                            )
                    except Exception as e:
                        tracker.log(f"⚠️ Error processing trade: {str(e)[:200]}")

        except Exception as e:
            tracker.log(f"🔴 Connection error: {str(e)[:200]}... Retrying in 5s")
            await asyncio.sleep(5)

@app.get("/")
async def get_dashboard():
    return HTMLResponse("""
    <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Crypto NYSE Tracker</title>
            <style>
                body { font-family: monospace; text-align: center; margin: 0; padding: 10px; }
                #metrics { border: 2px solid #000; padding: 15px; margin: 10px auto; max-width: 300px; font-size: 1.2em; white-space: pre; }
                #status { color: #666; font-size: 0.9em; margin-top: 10px; }
            </style>
        </head>
        <body>
            <h1>CRYPTO NYSE TRACKER</h1>
            <div id="metrics">Loading...</div>
            <div id="status">Connecting...</div>
            <script>
                const ws = new WebSocket(`wss://${window.location.host}/ws`);
                const statusEl = document.getElementById('status');
                
                ws.onopen = () => statusEl.textContent = "Connected (Live)";
                ws.onclose = () => statusEl.textContent = "Disconnected - Retrying...";
                ws.onmessage = (event) => {
                    document.getElementById('metrics').innerText = event.data;
                    statusEl.textContent = "Connected (Live)";
                };
            </script>
        </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        try:
            current_tick = sum(data.get("direction", 0) for data in tracker.tick_directions.values())
            add = sum(1 if data["last_price"] > tracker.prev_closes.get(symbol, 0) else -1 
                   for symbol, data in tracker.tick_directions.items() 
                   if symbol in tracker.prev_closes)
            
            sol_ratio = "∞" if tracker.sol_sell_volume == 0 else f"{tracker.sol_buy_volume/tracker.sol_sell_volume:.1f}:1"
            sol_dir = "↑" if tracker.sol_last_price and tracker.sol_prev_close and tracker.sol_last_price > tracker.sol_prev_close else "↓"
            
            await websocket.send_text(
                f"TICK:  {current_tick:+4} {tracker.last_15min_tick or ' '}\n"
                f"ADD:   {add:+4} {'↑' if add >=0 else '↓'}\n"
                f"SOL CVD: {sol_ratio} {sol_dir}\n"
                f"\n{datetime.now(pytz.timezone(TIMEZONE)).strftime('%d-%b-%Y %H:%M WAT')}\n"
                f"Next reset: {(datetime.now(pytz.timezone(TIMEZONE)) + timedelta(minutes=15 - (datetime.now().minute % 15))).strftime('%H:%M WAT')}"
            )
            await asyncio.sleep(1)
        except Exception as e:
            tracker.log(f"WebSocket error: {e}")
            break

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)