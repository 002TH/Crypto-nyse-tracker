import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

# Initialize FastAPI
app = FastAPI()

# Configuration
TIMEZONE = "Africa/Lagos"
TRACKED_COINS = ["btcusdt", "ethusdt"]  # Default coins for TICK/ADD
SOL_SYMBOL = "solusdt"  # Special tracking for CVD

class Tracker:
    def __init__(self):
        # TICK/ADD tracking
        self.tick_directions = {}
        self.prev_closes = {}
        
        # CVD tracking (SOLUSDT only)
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None
        self.sol_prev_close = None
        self.last_reset = datetime.now(pytz.timezone(TIMEZONE))
        
    async def load_historical(self):
        """Fetch yesterday's close prices"""
        # Load for TICK/ADD coins
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            try:
                data = requests.get(
                    f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}"
                ).json()
                self.prev_closes[symbol] = float(data["prevClosePrice"])
                if symbol == SOL_SYMBOL:
                    self.sol_prev_close = float(data["prevClosePrice"])
            except Exception as e:
                print(f"Failed to load data for {symbol}: {e}")

    def reset_if_new_day(self):
        """Reset daily metrics at midnight WAT"""
        now = datetime.now(pytz.timezone(TIMEZONE))
        if now.date() > self.last_reset.date():
            self.sol_buy_volume = 0.0
            self.sol_sell_volume = 0.0
            self.last_reset = now

    def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        """Update all metrics from trade data"""
        symbol = symbol.lower()
        
        # Update TICK for tracked coins
        if symbol in TRACKED_COINS:
            if symbol in self.tick_directions:
                prev_price = self.tick_directions[symbol].get("last_price", price)
                direction = 1 if price > prev_price else (-1 if price < prev_price else 0)
                self.tick_directions[symbol]["direction"] = direction
            self.tick_directions[symbol] = {"last_price": price, "direction": 0}
        
        # Update CVD for SOL only
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty

    def get_metrics(self):
        """Calculate current metrics"""
        self.reset_if_new_day()
        
        # TICK = sum of all directions
        current_tick = sum(
            data["direction"] 
            for data in self.tick_directions.values() 
            if "direction" in data
        )
        
        # ADD = coins above yesterday's close
        add = 0
        for symbol, data in self.tick_directions.items():
            if symbol in self.prev_closes:
                if data["last_price"] > self.prev_closes[symbol]:
                    add += 1
                elif data["last_price"] < self.prev_closes[symbol]:
                    add -= 1
        
        # CVD Ratio (SOL only)
        sol_ratio = "∞" if self.sol_sell_volume == 0 \
                   else f"{self.sol_buy_volume/self.sol_sell_volume:.1f}:1"
        
        # SOL price direction vs yesterday
        sol_direction = "↑" if self.sol_last_price and self.sol_prev_close and \
                          self.sol_last_price > self.sol_prev_close else "↓"
        
        return {
            "tick": current_tick,
            "add": add,
            "cvd": sol_ratio,
            "sol_direction": sol_direction,
            "time": datetime.now(pytz.timezone(TIMEZONE)).strftime("%d-%b-%Y %H:%M WAT")
        }

tracker = Tracker()

@app.on_event("startup")
async def startup():
    await tracker.load_historical()
    asyncio.create_task(track_live_data())

async def track_live_data():
    """Connect to Binance WebSocket"""
    while True:
        try:
            streams = [f"{s}@trade" for s in TRACKED_COINS + [SOL_SYMBOL]]
            async with websockets.connect(
                f"wss://stream.binance.com:9443/ws/{'/'.join(streams)}"
            ) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    tracker.process_trade(
                        symbol=data["s"].lower(),
                        price=float(data["p"]),
                        is_buyer_maker=data["m"],
                        qty=float(data["q"])
                    )
        except Exception as e:
            print(f"WebSocket error: {e}")
            await asyncio.sleep(1)

@app.get("/")
async def get_dashboard():
    return HTMLResponse("""
    <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Crypto NYSE Tracker</title>
            <style>
                body { 
                    font-family: monospace;
                    text-align: center;
                    margin: 0;
                    padding: 10px;
                }
                #metrics {
                    border: 2px solid #000;
                    padding: 15px;
                    margin: 10px auto;
                    max-width: 300px;
                    font-size: 1.2em;
                    white-space: pre;
                }
            </style>
        </head>
        <body>
            <h1>CRYPTO NYSE TRACKER</h1>
            <div id="metrics">Loading...</div>
            <div>Tracking: BTC, ETH (TICK/ADD) • SOL (CVD)</div>
            <script>
                const ws = new WebSocket(`wss://${location.host}/ws`);
                ws.onmessage = (event) => {
                    document.getElementById('metrics').innerText = event.data;
                };
            </script>
        </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        metrics = tracker.get_metrics()
        await websocket.send_text(
            f"TICK:  {metrics['tick']:+4} {'↑' if metrics['tick'] >=0 else '↓'}\n"
            f"ADD:   {metrics['add']:+4} {'↑' if metrics['add'] >=0 else '↓'}\n"
            f"SOL CVD: {metrics['cvd']} {metrics['sol_direction']}\n"
            f"\n{metrics['time']}"
        )
        await asyncio.sleep(1)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
