import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

app = FastAPI()

# Configuration
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

    async def load_yesterday_closes(self):
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            try:
                data = requests.get(
                    f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}"
                ).json()
                self.prev_closes[symbol] = float(data["prevClosePrice"])
            except Exception as e:
                print(f"Failed to load prevClosePrice for {symbol}: {e}")

    def reset_daily(self):
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0

    async def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        symbol = symbol.lower()
        # TICK
        if symbol in TRACKED_COINS:
            prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev_price else -1 if price < prev_price else 0
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}
        # CVD
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty

    async def run_15min_close_tracker(self):
        while True:
            now = datetime.now(timezone.utc)
            minutes = now.minute
            next_reset = (now + timedelta(minutes=15 - minutes % 15)).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())
            self.last_15min_close = sum(data["direction"] for data in self.tick_directions.values())

    async def run_daily_reset_utc(self):
        while True:
            now = datetime.now(timezone.utc)
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep((next_midnight - now).total_seconds())
            self.reset_daily()
            await self.load_yesterday_closes()

tracker = Tracker()

@app.on_event("startup")
async def on_startup():
    await tracker.load_yesterday_closes()
    asyncio.create_task(track_trades())
    asyncio.create_task(tracker.run_15min_close_tracker())
    asyncio.create_task(tracker.run_daily_reset_utc())

async def track_trades():
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
            print(f"WebSocket Error: {e}")
            await asyncio.sleep(2)

@app.get("/")
async def homepage():
    return HTMLResponse("""
    <html>
    <head><title>Crypto Market Breadth</title></head>
    <body style="font-family: monospace; text-align: center;">
        <h2>CRYPTO MARKET BREADTH</h2>
        <pre id="output">Loading...</pre>
        <script>
            const ws = new WebSocket(`wss://${window.location.host}/ws`);
            ws.onmessage = (msg) => {
                document.getElementById("output").innerText = msg.data;
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
        current_tick = sum(d["direction"] for d in tracker.tick_directions.values())
        tick_15min = f"(15mins {tracker.last_15min_close:+})" if tracker.last_15min_close is not None else ""

        # ADD
        add_count = 0
        for symbol in TRACKED_COINS:
            last_price = tracker.tick_directions.get(symbol, {}).get("last_price")
            prev_close = tracker.prev_closes.get(symbol)
            if last_price and prev_close:
                if last_price > prev_close:
                    add_count += 1
                elif last_price < prev_close:
                    add_count -= 1

        # CVD Ratio
        if tracker.sol_sell_volume == 0:
            delta_ratio = "∞"
        else:
            ratio = tracker.sol_buy_volume / tracker.sol_sell_volume
            delta_ratio = f"{ratio:.2f}:1" if ratio >= 1 else f"-{(1/ratio):.2f}:1"

        sol_status = ""
        if tracker.sol_last_price and tracker.prev_closes.get(SOL_SYMBOL):
            sol_status = "(above yesterday’s close)" if tracker.sol_last_price > tracker.prev_closes[SOL_SYMBOL] else "(below yesterday’s close)"

        # Send
        await websocket.send_text(
            f"TICK: {current_tick:+} {tick_15min}\n"
            f"ADD: {add_count:+}\n"
            f"SOL CVD: {delta_ratio} {sol_status}"
        )
        await asyncio.sleep(1)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)