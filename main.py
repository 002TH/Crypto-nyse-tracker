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
TRACKED_COINS = ["btcusdt", "ethusdt", "solusdt"]
SOL_SYMBOL = "solusdt"

class Tracker:
    def __init__(self):
        self.tick_directions = {}
        self.last_15min_close = None
        self.prev_closes = {}
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None

    async def load_historical(self):
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            try:
                res = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}")
                self.prev_closes[symbol] = float(res.json()["prevClosePrice"])
            except Exception as e:
                print(f"Failed to load prev close for {symbol}: {e}")

    async def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        symbol = symbol.lower()
        if symbol in TRACKED_COINS:
            prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev_price else -1 if price < prev_price else 0
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty

    async def run_15min_reset(self):
        while True:
            now = datetime.utcnow()
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())
            self.last_15min_close = sum(data["direction"] for data in self.tick_directions.values())
            print(f"[15min TICK] Snapshot: {self.last_15min_close:+d}")

tracker = Tracker()

@app.on_event("startup")
async def on_start():
    await tracker.load_historical()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.run_15min_reset())

async def track_live_data():
    while True:
        try:
            stream_names = [f"{s}@trade" for s in TRACKED_COINS + [SOL_SYMBOL]]
            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(stream_names)}"
            async with websockets.connect(url) as ws:
                async for msg in ws:
                    data = json.loads(msg)["data"]
                    await tracker.process_trade(
                        symbol=data["s"],
                        price=float(data["p"]),
                        is_buyer_maker=data["m"],
                        qty=float(data["q"])
                    )
        except Exception as e:
            print(f"[WebSocket] Error: {e}")
            await asyncio.sleep(1)

@app.get("/")
async def home():
    return HTMLResponse("""
    <html>
    <head>
        <title>Crypto Market Breadth</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { font-family: monospace; text-align: center; padding: 20px; }
            #metrics { font-size: 1.5em; margin-top: 20px; white-space: pre-line; }
        </style>
    </head>
    <body>
        <h2>CRYPTO MARKET BREADTH</h2>
        <div id="metrics">Connecting...</div>
        <script>
            const ws = new WebSocket(`wss://${window.location.host}/ws`);
            ws.onmessage = (e) => {
                document.getElementById("metrics").innerText = e.data;
            };
        </script>
    </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        current_tick = sum(data["direction"] for data in tracker.tick_directions.values())
        tick_15min = tracker.last_15min_close
        tick_line = f"{current_tick:+d} (15mins {tick_15min:+d})" if tick_15min is not None else f"{current_tick:+d}"

        add = sum(
            1 if tracker.tick_directions.get(sym, {}).get("last_price", 0) > tracker.prev_closes.get(sym, 0)
            else -1 if tracker.tick_directions.get(sym, {}).get("last_price", 0) < tracker.prev_closes.get(sym, 0)
            else 0
            for sym in TRACKED_COINS
        )

        sol_ratio = "â" if tracker.sol_sell_volume == 0 else f"{tracker.sol_buy_volume / tracker.sol_sell_volume:.2f}:1"
        sol_close = tracker.prev_closes.get(SOL_SYMBOL)
        sol_price = tracker.sol_last_price
        sol_status = "above yesterdayâs close" if sol_price and sol_close and sol_price > sol_close else "below yesterdayâs close"

        timestamp = datetime.utcnow().strftime("%d-%b-%Y %H:%M UTC")
        await websocket.send_text(
            f"TICK: {tick_line}\n"
            f"ADD:  {add:+d}\n"
            f"SOL CVD: {sol_ratio} ({sol_status})\n\n"
            f"{timestamp}"
        )
        await asyncio.sleep(1)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
