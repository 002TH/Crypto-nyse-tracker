import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

app = FastAPI()

# Use UTC time
TIMEZONE = "UTC"
TRACKED_COINS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "trxusdt", "adausdt", "suiusdt", "dogeusdt", "pepeusdt"]
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
                url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}"
                data = requests.get(url).json()
                self.prev_closes[symbol] = float(data["prevClosePrice"])
            except Exception as e:
                print(f"Error fetching prev close for {symbol}: {e}")

    async def process_trade(self, symbol, price, is_buyer_maker, qty):
        symbol = symbol.lower()

        # TICK
        if symbol in TRACKED_COINS:
            prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev_price else (-1 if price < prev_price else 0)
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}

        # CVD
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
            current_tick = sum(data["direction"] for data in self.tick_directions.values())
            self.last_15min_close = current_tick

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
            url = f"wss://stream.binance.com:9443/ws/{'/'.join(streams)}"
            async with websockets.connect(url) as ws:
                async for msg in ws:
                    data = json.loads(msg)
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
async def dashboard():
    return HTMLResponse("""
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Crypto Market Breadth</title>
        <style>
            body { font-family: monospace; text-align: center; padding: 20px; }
            #metrics { border: 2px solid #000; padding: 15px; max-width: 320px; margin: 0 auto; }
        </style>
    </head>
    <body>
        <h1>CRYPTO MARKET BREADTH</h1>
        <div id="metrics">Loading...</div>
        <script>
            const ws = new WebSocket(`wss://${window.location.host}/ws`);
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
        # TICK
        current_tick = sum(data["direction"] for data in tracker.tick_directions.values())
        last_close = tracker.last_15min_close
        tick_line = f"TICK: {current_tick:+} (15mins {last_close:+})" if last_close is not None else f"TICK: {current_tick:+}"

        # ADD
        add = 0
        for symbol in TRACKED_COINS:
            last = tracker.tick_directions.get(symbol, {}).get("last_price")
            prev = tracker.prev_closes.get(symbol)
            if last and prev:
                if last > prev:
                    add += 1
                elif last < prev:
                    add -= 1

        # CVD
        buy = tracker.sol_buy_volume
        sell = tracker.sol_sell_volume
        ratio = "∞" if sell == 0 else f"{buy/sell:.2f}:1"
        price = tracker.sol_last_price
        prev = tracker.prev_closes.get(SOL_SYMBOL)
        cvd_note = "(above yesterday’s close)" if price and prev and price > prev else "(below yesterday’s close)"
        cvd_line = f"SOL CVD: {ratio} {cvd_note}"

        timestamp = datetime.utcnow().strftime('%d-%b-%Y %H:%M UTC')
        await websocket.send_text(f"{tick_line}\nADD: {add:+}\n{cvd_line}\n\n{timestamp}")
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)