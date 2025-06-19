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
TRACKED_COINS = ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "bnbusdt", "trxusdt", "adausdt", "suiusdt", "dogeusdt", "pepeusdt"]
SOL_SYMBOL = "solusdt"

class Tracker:
    def __init__(self):
        self.tick_directions = {}  # {symbol: {"last_price": float, "direction": int}}
        self.last_15min_close = None
        self.prev_closes = {}
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0
        self.sol_last_price = None

    async def load_historical(self):
        """Fetch yesterday's close prices"""
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

        # Update TICK
        if symbol in TRACKED_COINS:
            prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev_price else (-1 if price < prev_price else 0)
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}

        # Update CVD
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
            else:
                self.sol_buy_volume += qty

    async def run_15min_reset(self):
        """Handle 15-minute resets for TICK"""
        while True:
            now = datetime.now(pytz.timezone(TIMEZONE))
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())

            current_tick = sum(data["direction"] for data in self.tick_directions.values())
            self.last_15min_close = current_tick
            print(f"15min close: TICK={current_tick}")

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
            async with websockets.connect(f"wss://stream.binance.com:9443/ws/{'/'.join(streams)}") as ws:
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
async def get_dashboard():
    return HTMLResponse("""
    <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Crypto Market Breadth</title>
            <style>
                body { font-family: monospace; text-align: center; padding: 20px; }
                #metrics { border: 2px solid #000; padding: 15px; max-width: 300px; margin: 0 auto; }
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
        current_tick = sum(data["direction"] for data in tracker.tick_directions.values())
        tick_15min = f"(15mins {tracker.last_15min_close:+})" if tracker.last_15min_close is not None else ""

        add = sum(
            1 if tracker.tick_directions.get(symbol, {}).get("last_price", 0) > tracker.prev_closes.get(symbol, 0)
            else -1 if tracker.tick_directions.get(symbol, {}).get("last_price", 0) < tracker.prev_closes.get(symbol, 0)
            else 0
            for symbol in TRACKED_COINS
        )

        # CVD ratio formatting
        if tracker.sol_sell_volume == 0 and tracker.sol_buy_volume > 0:
            sol_ratio = "+∞"
        elif tracker.sol_buy_volume == 0 and tracker.sol_sell_volume > 0:
            sol_ratio = "-∞"
        elif tracker.sol_buy_volume >= tracker.sol_sell_volume:
            ratio = tracker.sol_buy_volume / tracker.sol_sell_volume if tracker.sol_sell_volume > 0 else 0
            sol_ratio = f"+{ratio:.2f}:1"
        else:
            ratio = tracker.sol_sell_volume / tracker.sol_buy_volume if tracker.sol_buy_volume > 0 else 0
            sol_ratio = f"-{ratio:.2f}:1"

        # CVD direction text
        if tracker.sol_last_price and tracker.prev_closes.get(SOL_SYMBOL):
            if tracker.sol_last_price > tracker.prev_closes[SOL_SYMBOL]:
                sol_note = "(above yesterday's close)"
            elif tracker.sol_last_price < tracker.prev_closes[SOL_SYMBOL]:
                sol_note = "(below yesterday's close)"
            else:
                sol_note = "(unchanged)"
        else:
            sol_note = ""

        await websocket.send_text(
            f"TICK: {current_tick:+} {tick_15min}\n"
            f"ADD:  {add:+}\n"
            f"SOL CVD: {sol_ratio} {sol_note}\n\n"
            f"{datetime.now(pytz.timezone(TIMEZONE)).strftime('%d-%b-%Y %H:%M %Z')}"
        )
        await asyncio.sleep(2)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)