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
TRACKED_COINS = ["btcusdt", "ethusdt", "xrpusdt", "dogeusdt"]  # Reduced for testing
SOL_SYMBOL = "solusdt"  # Only for CVD
DEBUG = True  # Set to False in production

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
        self.connection_active = False

    async def log(self, message):
        if DEBUG:
            now = datetime.now(pytz.timezone(TIMEZONE)).strftime("%H:%M:%S")
            print(f"[{now}] {message}")

    async def load_historical(self):
        """Fetch yesterday's close prices with retry logic"""
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            retries = 3
            while retries > 0:
                try:
                    await self.log(f"Fetching historical data for {symbol}...")
                    data = requests.get(
                        f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}",
                        timeout=5
                    ).json()
                    self.prev_closes[symbol] = float(data["prevClosePrice"])
                    if symbol == SOL_SYMBOL:
                        self.sol_prev_close = float(data["prevClosePrice"])
                    await self.log(f"{symbol} prev close: {self.prev_closes[symbol]}")
                    break
                except Exception as e:
                    await self.log(f"Error loading {symbol}: {str(e)[:100]}... (retries left: {retries-1})")
                    retries -= 1
                    await asyncio.sleep(1)

    async def run_15min_reset(self):
        """Robust 15-minute reset with logging"""
        while True:
            now = datetime.now(pytz.timezone(TIMEZONE))
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            wait_seconds = (next_reset - now).total_seconds()
            await self.log(f"Next 15min reset at {next_reset.strftime('%H:%M')} (in {wait_seconds:.1f}s)")
            await asyncio.sleep(wait_seconds)
            
            if self.current_15min_ticks:
                final_tick = self.current_15min_ticks[-1]
                self.last_15min_tick = "↑" if final_tick > 0 else "↓" if final_tick < 0 else "→"
                await self.log(f"15min period closed: TICK={final_tick} {self.last_15min_tick}")
            else:
                await self.log("15min period closed: No trades received")
            self.current_15min_ticks = []

    def reset_if_new_day(self):
        now = datetime.now(pytz.timezone(TIMEZONE))
        if now.date() > self.last_reset.date():
            await self.log("Performing daily reset...")
            self.sol_buy_volume = 0.0
            self.sol_sell_volume = 0.0
            self.last_reset = now
            self.tick_directions = {}

    async def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        """Enhanced trade processing with validation"""
        symbol = symbol.lower()
        now = datetime.now(pytz.timezone(TIMEZONE))
        
        # Validate symbol
        if symbol not in TRACKED_COINS + [SOL_SYMBOL]:
            await self.log(f"Ignoring untracked symbol: {symbol}")
            return

        # Skip stale prices (older than 2 seconds)
        if symbol in self.last_trade_time and (now - self.last_trade_time[symbol]).total_seconds() > 2:
            await self.log(f"Stale price for {symbol}, skipping...")
            return

        # Process TICK for tracked coins
        if symbol in TRACKED_COINS:
            prev_data = self.tick_directions.get(symbol, {"last_price": price, "direction": 0})
            direction = 1 if price > prev_data["last_price"] else (-1 if price < prev_data["last_price"] else 0)
            
            self.tick_directions[symbol] = {
                "last_price": price,
                "direction": direction
            }
            
            current_tick = sum(data["direction"] for data in self.tick_directions.values())
            self.current_15min_ticks.append(current_tick)
            
            await self.log(f"{symbol} {'↑' if direction > 0 else '↓' if direction < 0 else '→'} {prev_data['last_price']}→{price} (TICK={current_tick})")

        # Process CVD for SOL
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty
            await self.log(f"SOL CVD: Buy={self.sol_buy_volume:.2f} Sell={self.sol_sell_volume:.2f}")

        self.last_trade_time[symbol] = now

tracker = Tracker()

@app.on_event("startup")
async def startup():
    await tracker.log("🚀 Starting application...")
    await tracker.load_historical()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.run_15min_reset())

async def track_live_data():
    """Robust WebSocket connection with error handling"""
    while True:
        try:
            await tracker.log("🔌 Connecting to Binance WebSocket...")
            streams = [f"{s}@trade" for s in list(set(TRACKED_COINS + [SOL_SYMBOL]))]
            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
            
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                tracker.connection_active = True
                await tracker.log(f"✅ Connected to {len(streams)} streams: {', '.join(streams)}")
                
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
                    except KeyError as e:
                        await tracker.log(f"⚠️ Unexpected message format: {str(e)} in {msg[:100]}...")
                    except Exception as e:
                        await tracker.log(f"⚠️ Trade processing error: {str(e)}")

        except websockets.exceptions.ConnectionClosed as e:
            tracker.connection_active = False
            await tracker.log(f"🔴 Connection closed: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            tracker.connection_active = False
            await tracker.log(f"🔴 Critical error: {str(e)[:200]}... Reconnecting in 10s...")
            await asyncio.sleep(10)

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
            metrics = tracker.get_metrics()
            await websocket.send_text(
                f"TICK:  {metrics['tick']:+4} {metrics['tick_arrow']}\n"
                f"ADD:   {metrics['add']:+4} {'↑' if metrics['add'] >=0 else '↓'}\n"
                f"SOL CVD: {metrics['cvd']} {metrics['sol_direction']}\n"
                f"\n{metrics['time']}\n"
                f"Next reset: {metrics['next_reset']}"
            )
            await asyncio.sleep(1)
        except Exception as e:
            await tracker.log(f"WS endpoint error: {e}")
            break

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, ws_ping_interval=20)