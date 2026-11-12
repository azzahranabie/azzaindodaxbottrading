import json
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .strategy import Candle


BASE_URL = "https://indodax.com"


class ExchangeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Book:
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]

    @property
    def best_bid(self):
        return self.bids[0][0] if self.bids else Decimal(0)

    @property
    def best_ask(self):
        return self.asks[0][0] if self.asks else Decimal(0)

    @property
    def spread_fraction(self):
        if self.best_bid <= 0 or self.best_ask <= 0:
            return None
        midpoint = (self.best_bid + self.best_ask) / 2
        return (self.best_ask - self.best_bid) / midpoint


class IndodaxClient:
    def __init__(self, timeout=12, retries=3, backoff=0.5):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def _json(self, path, params=None):
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "AzzaTradingDryRun/1.0"})
        last_error = None
        for attempt in range(self.retries):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise ExchangeError(f"Indodax HTTP {response.status} untuk {path}")
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff * (2**attempt))
        raise ExchangeError(f"Gagal membaca {path} setelah {self.retries} percobaan: {last_error}")

    def server_time(self):
        payload = self._json("/api/server_time")
        try:
            value = int(payload["server_time"])
        except (KeyError, TypeError, ValueError) as error:
            raise ExchangeError("Respons waktu server Indodax tidak valid") from error
        return value // 1000 if value > 10_000_000_000 else value

    def history(self, symbol, timeframe, start, end):
        payload = self._json(
            "/tradingview/history_v2",
            {"symbol": symbol, "tf": timeframe, "from": int(start), "to": int(end)},
        )
        if not isinstance(payload, list):
            raise ExchangeError(f"Format candle {symbol} tidak valid")
        candles = []
        for item in payload:
            try:
                candle = Candle(
                    time=int(item["Time"]),
                    open=float(item["Open"]),
                    high=float(item["High"]),
                    low=float(item["Low"]),
                    close=float(item["Close"]),
                    volume=float(item["Volume"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ExchangeError(f"Bar {symbol} tidak valid: {item}") from error
            candles.append(candle)
        return sorted(candles, key=lambda candle: candle.time)

    def depth(self, symbol):
        payload = self._json(f"/api/depth/{symbol.lower()}")
        try:
            bids = tuple(
                sorted(
                    ((Decimal(str(price)), Decimal(str(amount))) for price, amount in payload["buy"]),
                    key=lambda level: level[0],
                    reverse=True,
                )
            )
            asks = tuple(
                sorted(
                    ((Decimal(str(price)), Decimal(str(amount))) for price, amount in payload["sell"]),
                    key=lambda level: level[0],
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ExchangeError(f"Order book {symbol} tidak valid") from error
        return Book(bids, asks)

    def pairs(self):
        payload = self._json("/api/pairs")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ExchangeError("Respons /api/pairs tidak valid")
        return {str(item.get("symbol", "")).upper(): item for item in payload}

    def price_increments(self):
        payload = self._json("/api/price_increments")
        increments = payload.get("increments") if isinstance(payload, dict) else None
        if not isinstance(increments, dict):
            raise ExchangeError("Respons /api/price_increments tidak valid")
        return increments

    def ticker_all(self):
        return self._json("/api/ticker_all")
