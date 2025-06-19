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
TIMEZONE = "UTC"
TRACKED_COINS = [
    "btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt",
    "trxusdt", "adausdt", "suiusdt", "dogeusdt", "pepeusdt"
]
SOL_SYMBOL = "solusdt"

class Tracker:
    def __init__(self):
        self.tick_directions = {}  # TICK data
        self.prev_closes = {}      # Yesterday's closes
        self.last_15min_close = None

        # CVD data for SOL
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None

    async def load_historical(self):
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            try:
                data = requests.get(
                    f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}"
                ).json()
                self.prev_closes[symbol] = float(data["prevClosePrice"])
            except Exception as e:
                print(f"Error loading {symbol}: {e}")

    async def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        symbol = symbol.lower()

        # TICK
        if symbol in TRACKED_COINS:
            prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev_price else -1 if price < prev_price else 0
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}

        # CVD (SOL only)
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty

    async def run_15min_reset(self):
        while True:
            now = datetime.now(pytz.timezone(TIMEZONE))
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())

            tick_value = sum(data["direction"] for data in self.tick_directions.values())
            self.last_15min_close = tick_value
            print(f"15-min TICK close: {tick_value}")

tracker = Tracker()

@app.on_event("startup")
async def startup():
    await tracker.load_historical()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.run_15min_reset())

async def track_live_data():
    while True:
        try:
            streams = [f"{s}@trade" for s in TRACKED_COINS + [SOL_SYMBOL]]
            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
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
            print(f"WebSocket error: {e}")
            await asyncio.sleep(1)

@app.get("/")
async def get_dashboard():
    return HTMLResponse("""
    <html>
        <head>
            <title>Crypto Market Breadth</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body { font-family: monospace; background: #f4f4f4; text-align: center; padding: 30px; }
                #metrics { border: 2px solid #000; padding: 20px; background: #fff; display: inline-block; }
            </style>
        </head>
        <body>
            <h1>CRYPTO MARKET BREADTH</h1>
            <div id="metrics">Loading...</div>
            <script>
                const ws = new WebSocket(`wss://${window.location.host}/ws`);
                ws.onmessage = event => {
                    document.getElementById("metrics").innerText = event.data;
                };
            </script>
        </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        # TICK
        tick = sum(data["direction"] for data in tracker.tick_directions.values())
        tick_15min = f"(15mins {tracker.last_15min_close:+d})" if tracker.last_15min_close is not None else ""

        # ADD
        add = sum(
            1 if tracker.tick_directions.get(symbol, {}).get("last_price", 0) > tracker.prev_closes.get(symbol, 0)
            else -1 if tracker.tick_directions.get(symbol, {}).get("last_price", 0) < tracker.prev_closes.get(symbol, 0)
            else 0
            for symbol in TRACKED_COINS
        )

        # CVD
        buy = tracker.sol_buy_volume
        sell = tracker.sol_sell_volume
        ratio = (buy / sell) if sell > 0 else float("inf")
        ratio_str = f"{ratio:.2f}:1" if ratio != float("inf") else "∞"
        sol_close = tracker.prev_closes.get(SOL_SYMBOL, 0)
        direction = "above" if tracker.sol_last_price and tracker.sol_last_price > sol_close else "below"

        now = datetime.now(pytz.timezone(TIMEZONE)).strftime('%d-%b-%Y %H:%M UTC')

        await websocket.send_text(
            f"TICK: {tick:+d} {tick_15min}\n"
            f"ADD:  {add:+d}\n"
            f"SOL CVD: {ratio_str} ({direction} yesterday’s close)\n\n"
            f"{now}"
        )
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)