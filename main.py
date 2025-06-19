import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

app = FastAPI()

# --- CONFIGURATION ---
TIMEZONE = "UTC"
TRACKED_COINS = [
    "btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt",
    "trxusdt", "adausdt", "suiusdt", "dogeusdt", "pepeusdt"
]
SOL_SYMBOL = "solusdt"


class Tracker:
    def __init__(self):
        self.tick_directions = {}  # symbol -> {last_price, direction}
        self.last_15min_close = 0
        self.prev_closes = {}  # symbol -> yesterday's close
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None

    async def load_yesterday_closes(self):
        """Load yesterday's close using 24hr ticker from Binance."""
        for symbol in TRACKED_COINS:
            try:
                url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}"
                data = requests.get(url).json()
                self.prev_closes[symbol] = float(data["prevClosePrice"])
            except Exception as e:
                print(f"Failed to fetch prev close for {symbol}: {e}")

    async def process_trade(self, symbol, price, is_buyer_maker, qty):
        symbol = symbol.lower()
        # TICK update
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

    async def run_15min_tick_reset(self):
        while True:
            now = datetime.utcnow()
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())

            self.last_15min_close = sum(d["direction"] for d in self.tick_directions.values())
            print(f"[{datetime.utcnow()}] 15-min TICK close: {self.last_15min_close}")


tracker = Tracker()


@app.on_event("startup")
async def startup():
    await tracker.load_yesterday_closes()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.run_15min_tick_reset())


async def track_live_data():
    while True:
        try:
            stream_names = [f"{s}@trade" for s in TRACKED_COINS]
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
            print(f"WebSocket error: {e}")
            await asyncio.sleep(3)


@app.get("/")
async def index():
    return HTMLResponse("""
    <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Crypto Market Breadth</title>
            <style>
                body { font-family: monospace; background: #111; color: #eee; padding: 30px; text-align: center; }
                #metrics { font-size: 20px; white-space: pre; border: 1px solid #333; padding: 20px; display: inline-block; background: #222; }
            </style>
        </head>
        <body>
            <h1>CRYPTO MARKET BREADTH</h1>
            <div id="metrics">Loading...</div>
            <script>
                const ws = new WebSocket(`wss://${window.location.host}/ws`);
                ws.onmessage = (event) => {
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
        tick = sum(d["direction"] for d in tracker.tick_directions.values())
        tick_15min = tracker.last_15min_close

        # ADD
        advancers = sum(
            1 for symbol in TRACKED_COINS
            if tracker.tick_directions.get(symbol, {}).get("last_price", 0) > tracker.prev_closes.get(symbol, 0)
        )
        decliners = sum(
            1 for symbol in TRACKED_COINS
            if tracker.tick_directions.get(symbol, {}).get("last_price", 0) < tracker.prev_closes.get(symbol, 0)
        )
        add = advancers - decliners

        # SOL CVD
        buy = tracker.sol_buy_volume
        sell = tracker.sol_sell_volume
        ratio_text = "∞" if sell == 0 else f"{(buy/sell):.2f}:1"
        price = tracker.sol_last_price
        prev = tracker.prev_closes.get(SOL_SYMBOL)
        price_status = ""
        if price is not None and prev is not None:
            price_status = "(above yesterday’s close)" if price > prev else "(below yesterday’s close)"

        # Time
        now = datetime.utcnow().strftime("%d-%b-%Y %H:%M UTC")

        # Final display
        await websocket.send_text(
            f"TICK: {tick:+d} (15mins {tick_15min:+d})\n"
            f"ADD:  {add:+d}\n"
            f"SOL CVD: {ratio_text} {price_status}\n\n"
            f"{now}"
        )
        await asyncio.sleep(1)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)