import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta
import pytz
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

app = FastAPI()

# CONFIG
TRACKED_COINS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "trxusdt", "adausdt", "suiusdt", "dogeusdt", "pepeusdt"]
SOL_SYMBOL = "solusdt"
UTC = pytz.utc

class Tracker:
    def __init__(self):
        self.tick_directions = {}
        self.last_15min_close = None
        self.prev_closes = {}
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None
        self.cvd_delta = 0.0

    async def load_yesterday_closes(self):
        for symbol in TRACKED_COINS + [SOL_SYMBOL]:
            try:
                res = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}")
                self.prev_closes[symbol] = float(res.json()["prevClosePrice"])
            except Exception as e:
                print(f"Error loading {symbol} close: {e}")

    async def process_trade(self, symbol, price, is_buyer_maker, qty):
        symbol = symbol.lower()

        # TICK logic
        if symbol in TRACKED_COINS:
            prev = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev else (-1 if price < prev else 0)
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}

        # SOL CVD logic
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            delta = qty if not is_buyer_maker else -qty
            self.cvd_delta += delta
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty

    async def run_tick_reset(self):
        while True:
            now = datetime.now(UTC)
            next_reset = (now + timedelta(minutes=15 - now.minute % 15)).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())
            self.last_15min_close = sum(v["direction"] for v in self.tick_directions.values())
            print(f"[15-min close] TICK = {self.last_15min_close}")

    async def run_daily_reset(self):
        while True:
            now = datetime.now(UTC)
            next_reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            await asyncio.sleep((next_reset - now).total_seconds())
            print("[Daily Reset] Resetting CVD volumes.")
            self.sol_buy_volume = 0.0
            self.sol_sell_volume = 0.0
            self.cvd_delta = 0.0
            await self.load_yesterday_closes()

tracker = Tracker()

@app.on_event("startup")
async def startup():
    await tracker.load_yesterday_closes()
    asyncio.create_task(track_websocket())
    asyncio.create_task(tracker.run_tick_reset())
    asyncio.create_task(tracker.run_daily_reset())

async def track_websocket():
    while True:
        try:
            streams = [f"{s}@trade" for s in TRACKED_COINS + [SOL_SYMBOL]]
            url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"
            async with websockets.connect(url) as ws:
                async for msg in ws:
                    data = json.loads(msg)
                    payload = data["data"]
                    await tracker.process_trade(
                        symbol=payload["s"],
                        price=float(payload["p"]),
                        is_buyer_maker=payload["m"],
                        qty=float(payload["q"])
                    )
        except Exception as e:
            print(f"WebSocket error: {e}")
            await asyncio.sleep(1)

@app.get("/")
async def home():
    return HTMLResponse("""
    <html>
        <head><title>Crypto Breadth</title></head>
        <body style="font-family: monospace; text-align: center;">
            <h2>CRYPTO MARKET BREADTH</h2>
            <div id="metrics">Loading...</div>
            <script>
                const ws = new WebSocket(`wss://${location.host}/ws`);
                ws.onmessage = (event) => {
                    document.getElementById("metrics").innerText = event.data;
                };
            </script>
        </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    while True:
        # TICK
        tick = sum(v["direction"] for v in tracker.tick_directions.values())
        tick_15 = tracker.last_15min_close
        tick_str = f"TICK: {tick:+d} (15mins {tick_15:+d})" if tick_15 is not None else f"TICK: {tick:+d}"

        # CVD
        sell = tracker.sol_sell_volume
        buy = tracker.sol_buy_volume
        delta = tracker.cvd_delta
        ratio = "∞" if sell == 0 else f"{(buy/sell):.2f}:1"
        raw = f"{int(delta):+}"  # e.g. +2211
        dir_str = "above" if tracker.sol_last_price and tracker.prev_closes.get(SOL_SYMBOL) and tracker.sol_last_price > tracker.prev_closes[SOL_SYMBOL] else "below"
        cvd_str = f"SOL CVD: {ratio} ({raw}) ({dir_str} yesterday’s close)"

        # Above / Below table
        above, below = [], []
        for coin in TRACKED_COINS:
            current = tracker.tick_directions.get(coin, {}).get("last_price")
            prev = tracker.prev_closes.get(coin)
            if current and prev:
                (above if current > prev else below).append(coin.upper())

        # Format columns
        table = "Above\n" + "\n".join(above or ["-"]) + "\n\nBelow\n" + "\n".join(below or ["-"])
        
        now_str = datetime.now(UTC).strftime('%d-%b-%Y %H:%M UTC')

        await ws.send_text(f"{tick_str}\n{cvd_str}\n\n{table}\n\n{now_str}")
        await asyncio.sleep(1)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)