import websockets
import asyncio
import json
import requests
from datetime import datetime, timedelta, time
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
        # TICK Tracking
        self.tick_directions = {}  # {symbol: {"last_price": float, "direction": int}}
        self.last_15min_close = None

        # CVD Tracking
        self.prev_closes = {}
        self.sol_last_price = None
        self.sol_cvd = 0.0  # cumulative delta
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0

    async def load_historical(self):
        for symbol in TRACKED_COINS:
            try:
                data = requests.get(
                    f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol.upper()}"
                ).json()
                self.prev_closes[symbol] = float(data["prevClosePrice"])
            except Exception as e:
                print(f"Error loading {symbol}: {e}")

    def reset_daily(self):
        self.sol_cvd = 0.0
        self.sol_buy_volume = 0.0
        self.sol_sell_volume = 0.0

    async def daily_reset_loop(self):
        while True:
            now = datetime.now(pytz.utc)
            next_reset = datetime.combine(now.date() + timedelta(days=1), time(0, 0, tzinfo=pytz.utc))
            await asyncio.sleep((next_reset - now).total_seconds())
            self.reset_daily()

    async def run_15min_reset(self):
        while True:
            now = datetime.now(pytz.utc)
            next_reset = (now + timedelta(minutes=15 - (now.minute % 15))).replace(second=0, microsecond=0)
            await asyncio.sleep((next_reset - now).total_seconds())
            current_tick = sum(d["direction"] for d in self.tick_directions.values())
            self.last_15min_close = current_tick

    async def process_trade(self, symbol: str, price: float, is_buyer_maker: bool, qty: float):
        symbol = symbol.lower()

        # TICK update
        if symbol in TRACKED_COINS:
            prev_price = self.tick_directions.get(symbol, {}).get("last_price", price)
            direction = 1 if price > prev_price else -1 if price < prev_price else 0
            self.tick_directions[symbol] = {"last_price": price, "direction": direction}

        # CVD for SOL
        if symbol == SOL_SYMBOL:
            self.sol_last_price = price
            if is_buyer_maker:
                self.sol_sell_volume += qty
                self.sol_cvd -= qty
            else:
                self.sol_buy_volume += qty
                self.sol_cvd += qty

tracker = Tracker()

@app.on_event("startup")
async def startup():
    await tracker.load_historical()
    asyncio.create_task(track_live_data())
    asyncio.create_task(tracker.daily_reset_loop())
    asyncio.create_task(tracker.run_15min_reset())

async def track_live_data():
    while True:
        try:
            streams = [f"{s}@trade" for s in TRACKED_COINS]
            async with websockets.connect(f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}") as ws:
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
                body { font-family: monospace; text-align: center; padding: 20px; }
                table { margin: 10px auto; border-collapse: collapse; }
                td, th { border: 1px solid #aaa; padding: 6px 12px; }
                .green { color: green; }
                .red { color: red; }
            </style>
        </head>
        <body>
            <h2>CRYPTO MARKET BREADTH</h2>
            <div id="metrics">Loading...</div>
            <script>
                const ws = new WebSocket(`wss://${window.location.host}/ws`);
                ws.onmessage = (event) => {
                    document.getElementById("metrics").innerHTML = event.data;
                };
            </script>
        </body>
    </html>
    """)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    while True:
        tick_value = sum(v["direction"] for v in tracker.tick_directions.values())
        tick_change = tracker.last_15min_close
        tick_display = f"{tick_value:+d}" + (f" (15mins {tick_change:+d})" if tick_change is not None else "")

        # CVD ratio
        buy = tracker.sol_buy_volume
        sell = tracker.sol_sell_volume
        ratio = "∞" if sell == 0 else f"{buy/sell:.2f}:1"
        cvd_value = f"{tracker.sol_cvd:+.0f}"
        sol_price = tracker.sol_last_price
        prev_close = tracker.prev_closes.get(SOL_SYMBOL)
        price_pos = sol_price > prev_close if sol_price and prev_close else False
        price_note = "(above yesterday’s close)" if price_pos else "(below yesterday’s close)"

        cvd_line = f"SOL CVD: {ratio} ({cvd_value}) {price_note}"

        # Advancers / Decliners
        advancers = []
        decliners = []
        for symbol in TRACKED_COINS:
            price = tracker.tick_directions.get(symbol, {}).get("last_price", 0)
            yclose = tracker.prev_closes.get(symbol)
            if yclose and price:
                (advancers if price > yclose else decliners).append(symbol.upper())

        max_rows = max(len(advancers), len(decliners))
        advancers += [""] * (max_rows - len(advancers))
        decliners += [""] * (max_rows - len(decliners))
        rows = "".join(
            f"<tr><td>{a}</td><td>{d}</td></tr>"
            for a, d in zip(advancers, decliners)
        )

        table_html = f"""
        <table>
            <tr><th>Advancers</th><th>Decliners</th></tr>
            {rows}
        </table>
        """

        now = datetime.now(pytz.utc).strftime("%d-%b-%Y %H:%M UTC")
        display = f"""
        TICK: {tick_display}<br>
        {cvd_line}<br><br>
        {table_html}
        <br>{now}
        """
        await websocket.send_text(display)
        await asyncio.sleep(1)