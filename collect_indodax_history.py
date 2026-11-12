import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://indodax.com"
TIMEFRAME_SECONDS = 4 * 60 * 60
CHUNK_SECONDS = 27 * 24 * 60 * 60
INDICATOR_WARMUP_BARS = 210
PAIRS = ("BTCIDR", "ETHIDR", "XRPIDR")


class CollectionError(RuntimeError):
    pass


class PublicIndodax:
    def __init__(self, timeout=20, minimum_interval=0.4):
        self.timeout = timeout
        self.minimum_interval = minimum_interval
        self.last_request = 0.0

    def get_json(self, path, params=None):
        url = f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        last_error = None
        for attempt in range(3):
            elapsed = time.monotonic() - self.last_request
            if elapsed < self.minimum_interval:
                time.sleep(self.minimum_interval - elapsed)
            request = Request(url, headers={"User-Agent": "AzzaTradingResearch/1.0"})
            self.last_request = time.monotonic()
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return payload
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2**attempt)
        raise CollectionError(f"Gagal membaca {path}: {last_error}") from last_error


def parse_utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("Gunakan waktu ISO 8601, misalnya 2023-11-12T00:00:00Z") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Waktu harus memuat zona waktu")
    return parsed.astimezone(timezone.utc)


def _validated_candle(item, symbol):
    try:
        candle = {
            "time": int(item["Time"]),
            "open": float(item["Open"]),
            "high": float(item["High"]),
            "low": float(item["Low"]),
            "close": float(item["Close"]),
            "volume": float(item["Volume"]),
        }
    except (KeyError, TypeError, ValueError) as error:
        raise CollectionError(f"Candle {symbol} tidak valid: {item}") from error
    values = (candle["open"], candle["high"], candle["low"], candle["close"], candle["volume"])
    if not all(math.isfinite(value) and value > 0 for value in values):
        raise CollectionError(f"Candle {symbol} memiliki harga atau volume nol/tidak valid")
    if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
        raise CollectionError(f"High candle {symbol} tidak konsisten")
    if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
        raise CollectionError(f"Low candle {symbol} tidak konsisten")
    return candle


def collect(start, end, output, client=None):
    client = client or PublicIndodax()
    server_time_payload = client.get_json("/api/server_time")
    try:
        server_time = int(server_time_payload["server_time"])
    except (KeyError, TypeError, ValueError) as error:
        raise CollectionError("Waktu server Indodax tidak valid") from error
    if server_time > 10_000_000_000:
        server_time //= 1000
    requested_start = int(start.timestamp())
    requested_end = min(int(end.timestamp()), server_time)
    if requested_end <= requested_start:
        raise CollectionError("Rentang end harus setelah start")
    fetch_start = requested_start - INDICATOR_WARMUP_BARS * TIMEFRAME_SECONDS

    bars_by_symbol = {}
    invalid_candles = []
    for symbol in PAIRS:
        bars = {}
        cursor = fetch_start
        while cursor < requested_end:
            chunk_end = min(cursor + CHUNK_SECONDS, requested_end)
            payload = client.get_json(
                "/tradingview/history_v2",
                {"symbol": symbol, "tf": "240", "from": cursor, "to": chunk_end},
            )
            if not isinstance(payload, list):
                raise CollectionError(f"Respons history_v2 {symbol} bukan array")
            for item in payload:
                try:
                    candle = _validated_candle(item, symbol)
                except CollectionError as error:
                    invalid_candles.append(
                        {
                            "symbol": symbol,
                            "time": item.get("Time") if isinstance(item, dict) else None,
                            "raw": item,
                            "reason": str(error),
                        }
                    )
                    continue
                if fetch_start <= candle["time"] and candle["time"] + TIMEFRAME_SECONDS <= requested_end:
                    bars[candle["time"]] = candle
            cursor = chunk_end
            print(f"{symbol}: {datetime.fromtimestamp(cursor, timezone.utc).isoformat()}", flush=True)
        bars_by_symbol[symbol] = [bars[key] for key in sorted(bars)]

    pairs_response = client.get_json("/api/pairs")
    if not isinstance(pairs_response, list):
        raise CollectionError("Respons /api/pairs bukan array")
    pair_metadata = {
        str(item.get("symbol", "")).upper(): item
        for item in pairs_response
        if isinstance(item, dict)
    }
    selected_pairs = {}
    for symbol in PAIRS:
        pair = pair_metadata.get(symbol)
        if pair is None:
            raise CollectionError(f"Metadata pasangan {symbol} tidak tersedia")
        selected_pairs[symbol] = {
            "trade_fee_percent_taker": pair.get("trade_fee_percent_taker"),
            "trade_min_base_currency": pair.get("trade_min_base_currency"),
            "trade_min_traded_currency": pair.get("trade_min_traded_currency"),
            "quantity_increment": pair.get("quantity_increment"),
            "is_maintenance": pair.get("is_maintenance"),
            "is_market_suspended": pair.get("is_market_suspended"),
        }
    increments_response = client.get_json("/api/price_increments")
    if not isinstance(increments_response, dict) or not isinstance(increments_response.get("increments"), dict):
        raise CollectionError("Kenaikan harga dari Indodax tidak valid")
    increments = increments_response["increments"]

    liquidity_end = requested_start + 30 * 24 * 60 * 60
    liquidity = {}
    for symbol, candles in bars_by_symbol.items():
        sample = [
            candle
            for candle in candles
            if requested_start <= candle["time"] < liquidity_end
        ]
        liquidity[symbol] = {
            "initial_30d_bars": len(sample),
            "initial_30d_notional_idr": sum(item["close"] * item["volume"] for item in sample),
        }
    ranking = sorted(liquidity, key=lambda symbol: liquidity[symbol]["initial_30d_notional_idr"], reverse=True)

    result = {
        "source": "Indodax public API; raw OHLCV response fields retained after validation",
        "history_endpoint": f"{BASE_URL}/tradingview/history_v2",
        "timeframe": "240",
        "timeframe_seconds": TIMEFRAME_SECONDS,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "actual_start": datetime.fromtimestamp(requested_start, timezone.utc).isoformat(),
        "actual_end": datetime.fromtimestamp(requested_end, timezone.utc).isoformat(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "warmup_bars": INDICATOR_WARMUP_BARS,
        "candidate_universe": list(PAIRS),
        "universe_selection": {
            "method": "Rank the predeclared BTCIDR/ETHIDR/XRPIDR candidate set by total close*base-volume IDR notional during the first 30 days of the requested backtest period; retain all three.",
            "initial_30d_liquidity": liquidity,
            "ranking_high_to_low": ranking,
            "survivorship_note": "This fixed three-pair candidate set is not a historical archive of all Indodax listings. Delisted pairs are not represented; this limitation must be stated when interpreting results.",
        },
        "pair_metadata_retrieved_at": datetime.now(timezone.utc).isoformat(),
        "pair_metadata": selected_pairs,
        "price_increments": {symbol: increments.get(f"{symbol[:-3].lower()}_idr") for symbol in PAIRS},
        "invalid_candles": invalid_candles,
        "candles": bars_by_symbol,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "start": result["actual_start"],
        "end": result["actual_end"],
        "bars": {symbol: len(rows) for symbol, rows in bars_by_symbol.items()},
        "invalid_candles": len(invalid_candles),
        "initial_30d_ranking": ranking,
    }, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description="Download original 4-hour OHLCV history from the public Indodax API.")
    parser.add_argument("--start", type=parse_utc, default=parse_utc("2023-11-12T00:00:00Z"))
    parser.add_argument("--end", type=parse_utc, default=datetime.now(timezone.utc))
    parser.add_argument("--output", type=Path, default=Path("data/indodax_4h_history.json"))
    args = parser.parse_args()
    try:
        collect(args.start, args.end, args.output)
    except (CollectionError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
