import hmac
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from crypto_bot.bot import _pair_minimums, run_tick
from crypto_bot.execution import round_quantity, simulate_market_order
from crypto_bot.exchange import ExchangeError, IndodaxClient
from crypto_bot.settings import SETTINGS
from crypto_bot.storage import LockBusy, StorageError, create_store


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("indodax-dry-run")
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Azza Indodax Dry-Run Bot", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class KillSwitchRequest(BaseModel):
    enabled: bool


class SnapshotRequest(BaseModel):
    order_size_idr: float = 10_000_000


def _store():
    store = getattr(app.state, "store", None)
    if store is not None:
        return store
    try:
        return create_store()
    except StorageError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _authorize(authorization):
    secret = SETTINGS.cron_secret
    if not secret:
        raise HTTPException(status_code=503, detail="CRON_SECRET belum dikonfigurasi")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not hmac.compare_digest(supplied, secret):
        raise HTTPException(status_code=401, detail="Secret tidak valid")


@app.get("/")
def dashboard():
    return FileResponse(BASE_DIR / "Azza Dashboard Trading.backup.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "mode": "dry-run",
        "exchange": "Indodax public API",
        "api_keys_required": False,
        "universe": list(SETTINGS.universe),
        "timeframe": SETTINGS.timeframe_api,
    }


@app.post("/api/tick")
def tick(
    authorization: str | None = Header(default=None),
    x_scheduled_time: str | None = Header(default=None),
):
    _authorize(authorization)
    if x_scheduled_time:
        try:
            parsed_scheduled_time = datetime.fromisoformat(
                x_scheduled_time.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="X-Scheduled-Time harus berupa ISO 8601"
            ) from error
        if parsed_scheduled_time.tzinfo is None:
            raise HTTPException(
                status_code=400, detail="X-Scheduled-Time harus memuat zona waktu"
            )
        scheduled_time = parsed_scheduled_time.isoformat()
    else:
        scheduled_time = None
    store = _store()
    try:
        return run_tick(store, scheduled_time=scheduled_time)
    except LockBusy as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ExchangeError as error:
        logger.exception("Permintaan ke Indodax gagal")
        raise HTTPException(status_code=502, detail=str(error)) from error
    except StorageError as error:
        logger.exception("Penyimpanan state gagal")
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/snapshot")
def snapshot(
    payload: SnapshotRequest,
    authorization: str | None = Header(default=None),
    x_scheduled_time: str | None = Header(default=None),
):
    _authorize(authorization)
    if not 25_000 <= payload.order_size_idr <= 100_000_000:
        raise HTTPException(status_code=400, detail="order_size_idr harus Rp25.000–Rp100.000.000")
    if x_scheduled_time:
        try:
            parsed_scheduled_time = datetime.fromisoformat(
                x_scheduled_time.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="X-Scheduled-Time harus berupa ISO 8601"
            ) from error
        if parsed_scheduled_time.tzinfo is None:
            raise HTTPException(
                status_code=400, detail="X-Scheduled-Time harus memuat zona waktu"
            )
        scheduled_time = parsed_scheduled_time.isoformat()
    else:
        scheduled_time = datetime.now(timezone.utc).isoformat()

    store = _store()
    client = IndodaxClient()
    try:
        pairs = client.pairs()
        snapshots = []
        errors = []
        for symbol in SETTINGS.universe:
            record = {
                "type": "book_snapshot",
                "symbol": symbol,
                "time": datetime.now(timezone.utc).isoformat(),
                "scheduled_time": scheduled_time,
                "order_size_idr": payload.order_size_idr,
            }
            try:
                pair_info = pairs.get(symbol)
                if not pair_info or pair_info.get("is_maintenance") or pair_info.get("is_market_suspended"):
                    raise ExchangeError("Pasangan tidak aktif")
                book = client.depth(symbol)
                if not book.bids or not book.asks or book.spread_fraction is None:
                    raise ExchangeError("Order book kosong")
                minimum, minimum_quantity, quantity_step, buy_fee_rate, sell_fee_rate = (
                    _pair_minimums(pair_info)
                )
                buy_quantity = round_quantity(
                    Decimal(str(payload.order_size_idr)) / book.best_ask, quantity_step
                )
                sell_quantity = round_quantity(
                    Decimal(str(payload.order_size_idr)) / book.best_bid, quantity_step
                )
                buy_fill = simulate_market_order(
                    "buy", buy_quantity, book, buy_fee_rate, minimum, minimum_quantity, quantity_step
                )
                sell_fill = simulate_market_order(
                    "sell", sell_quantity, book, sell_fee_rate, minimum, minimum_quantity, quantity_step
                )
                record.update(
                    {
                        "status": "ok",
                        "best_bid": str(book.best_bid),
                        "best_ask": str(book.best_ask),
                        "spread_fraction": float(book.spread_fraction),
                        "bids": [[str(price), str(amount)] for price, amount in book.bids],
                        "asks": [[str(price), str(amount)] for price, amount in book.asks],
                        "buy_fill_average": str(buy_fill.average_price) if buy_fill else None,
                        "sell_fill_average": str(sell_fill.average_price) if sell_fill else None,
                        "buy_slippage_fraction": (
                            float(buy_fill.average_price / book.best_ask - 1) if buy_fill else None
                        ),
                        "sell_slippage_fraction": (
                            float(1 - sell_fill.average_price / book.best_bid) if sell_fill else None
                        ),
                        "buy_fill_complete": buy_fill is not None,
                        "sell_fill_complete": sell_fill is not None,
                        "buy_fee_rate": buy_fee_rate,
                        "sell_fee_rate": sell_fee_rate,
                    }
                )
            except (ExchangeError, TypeError, ValueError) as error:
                record.update({"status": "error", "error": str(error)})
                errors.append({"symbol": symbol, "message": str(error)})
            try:
                store.append_snapshot(record)
            except StorageError:
                logger.exception("Snapshot order book gagal disimpan")
                raise
            snapshots.append(record)
        store.append_event(
            {
                "type": "snapshot_call",
                "time": datetime.now(timezone.utc).isoformat(),
                "scheduled_time": scheduled_time,
                "status": "complete" if not errors else "partial",
                "symbols": len(snapshots),
                "errors": errors,
            }
        )
        return {"status": "complete" if not errors else "partial", "snapshots": snapshots}
    except ExchangeError as error:
        store.append_event(
            {
                "type": "snapshot_call",
                "time": datetime.now(timezone.utc).isoformat(),
                "scheduled_time": scheduled_time,
                "status": "error",
                "errors": [{"scope": "exchange", "message": str(error)}],
            }
        )
        raise HTTPException(status_code=502, detail=str(error)) from error
    except StorageError as error:
        logger.exception("Penyimpanan snapshot gagal")
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/kill-switch")
def kill_switch(
    payload: KillSwitchRequest,
    authorization: str | None = Header(default=None),
):
    _authorize(authorization)
    store = _store()
    try:
        store.set_kill_switch(payload.enabled)
        store.append_event(
            {
                "type": "kill_switch",
                "enabled": payload.enabled,
                "time": datetime.now(timezone.utc).isoformat(),
            }
        )
    except StorageError as error:
        logger.exception("Perubahan kill switch gagal")
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"kill_switch": payload.enabled}


@app.get("/api/dashboard")
def dashboard_data():
    store = _store()
    try:
        state = store.load_state()
        events = store.read_events()
    except StorageError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    backtest_path = BASE_DIR / "crypto_bot" / "backtest_result.json"
    backtest = None
    backtest_status = "Belum ada hasil backtest Indodax tervalidasi"
    if backtest_path.exists():
        try:
            backtest = json.loads(backtest_path.read_text(encoding="utf-8"))
            backtest_status = "Hasil backtest dibaca dari backtest_result.json"
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=503, detail=f"Hasil backtest tidak valid: {error}") from error
    return {
        "mode": "dry-run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backtest_status": backtest_status,
        "backtest": backtest,
        "initial_cash": SETTINGS.starting_cash,
        "forward_start_at": os.getenv("FORWARD_START_AT") or None,
        "timeframe_seconds": SETTINGS.timeframe_seconds,
        "schedule_interval_hours": SETTINGS.timeframe_seconds // 3600,
        "state": state,
        "events": events,
        "snapshots": store.read_snapshots(),
        "kill_switch": store.is_kill_switch_on(),
    }


if os.getenv("VERCEL") == "1" and not SETTINGS.cron_secret:
    logger.error("CRON_SECRET belum dikonfigurasi; endpoint tick akan tetap ditutup")
