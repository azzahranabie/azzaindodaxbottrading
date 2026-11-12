import math
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from .execution import round_quantity, simulate_market_order
from .exchange import ExchangeError, IndodaxClient
from .settings import SETTINGS
from .storage import LockBusy
from .strategy import has_contiguous_candles, signal_for


JAKARTA = timezone(timedelta(hours=7))


def new_state(starting_cash=None):
    starting_cash = starting_cash or SETTINGS.starting_cash
    return {
        "cash": float(starting_cash),
        "initial_cash": float(starting_cash),
        "positions": {},
        "processed": {},
        "daily": None,
        "daily_equity": [],
        "closed_trades": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _valid_candle(candle):
    return all(
        math.isfinite(value) and value > 0
        for value in (candle.open, candle.high, candle.low, candle.close, candle.volume)
    ) and candle.high >= max(candle.open, candle.close, candle.low) and candle.low <= min(
        candle.open, candle.close, candle.high
    )


def _mark_equity(state, books):
    equity = Decimal(str(state["cash"]))
    for symbol, position in state["positions"].items():
        book = books.get(symbol)
        mark = book.best_bid if book is not None and book.best_bid > 0 else position.get("last_best_bid")
        if mark is None or Decimal(str(mark)) <= 0:
            continue
        equity += Decimal(str(position["quantity"])) * Decimal(str(mark))
    return equity


def _pair_minimums(pair_info):
    try:
        minimum_value = max(
            SETTINGS.minimum_order_idr,
            float(pair_info.get("trade_min_base_currency") or 0),
        )
        minimum_quantity = float(pair_info.get("trade_min_traded_currency") or 0)
        quantity_increment = float(pair_info.get("quantity_increment") or 0)
        fee_percent = float(pair_info.get("trade_fee_percent_taker") or 0)
    except (TypeError, ValueError) as error:
        raise ExchangeError("Metadata minimum, increment, atau fee pasangan tidak valid") from error
    if not all(
        math.isfinite(value)
        for value in (minimum_value, minimum_quantity, quantity_increment, fee_percent)
    ) or minimum_value < 0 or minimum_quantity < 0 or quantity_increment <= 0 or fee_percent < 0:
        raise ExchangeError("Metadata minimum, increment, atau fee pasangan di luar batas")
    api_fee_rate = fee_percent / 100
    buy_fee_rate = max(SETTINGS.all_in_taker_buy_fee_rate, api_fee_rate)
    sell_fee_rate = max(SETTINGS.all_in_taker_sell_fee_rate, api_fee_rate)
    return minimum_value, minimum_quantity, quantity_increment, buy_fee_rate, sell_fee_rate


def _order_record(symbol, side, fill, signal, timestamp, reason):
    signal_price = Decimal(str(signal.close))
    return {
        "time": timestamp,
        "symbol": symbol,
        "side": side,
        "quantity": float(fill.quantity),
        "best_bid": float(fill.best_bid),
        "best_ask": float(fill.best_ask),
        "average_fill": float(fill.average_price),
        "fee_idr": float(fill.fee),
        "signal_price": float(signal_price),
        "fill_vs_signal_idr": float(fill.average_price - signal_price),
        "fill_vs_signal_fraction": float(fill.average_price / signal_price - 1),
        "reason": reason,
        "source": "public order book; dry-run only",
    }


def _buy(state, symbol, signal, book, pair_info, books, now):
    minimum, minimum_quantity, quantity_step, buy_fee_rate, _ = _pair_minimums(pair_info)
    equity = _mark_equity(state, books)
    budget = min(equity * Decimal(str(SETTINGS.max_position_fraction)), Decimal(str(state["cash"])))
    budget /= Decimal(1) + Decimal(str(buy_fee_rate))
    quantity = round_quantity(budget / book.best_ask, quantity_step)
    for _ in range(10):
        fill = simulate_market_order(
            "buy", quantity, book, buy_fee_rate, minimum, minimum_quantity, quantity_step
        )
        if fill is None:
            return None
        total_cost = fill.notional + fill.fee
        if total_cost <= Decimal(str(state["cash"])):
            break
        affordable_quantity = round_quantity(
            quantity * Decimal(str(state["cash"])) / total_cost,
            quantity_step,
        )
        quantity = min(
            affordable_quantity,
            round_quantity(quantity - Decimal(str(quantity_step)), quantity_step),
        )
        if quantity <= 0:
            return None
    else:
        return None

    state["cash"] = float(Decimal(str(state["cash"])) - fill.notional - fill.fee)
    state["positions"][symbol] = {
        "quantity": float(fill.quantity),
        "entry_price": float(fill.average_price),
        "entry_notional": float(fill.notional),
        "entry_fee": float(fill.fee),
        "entry_time": signal.candle_time,
        "last_best_bid": float(fill.best_bid),
        "last_mark_time": now,
    }
    return _order_record(symbol, "buy", fill, signal, now, "Donchian breakout + SMA")


def _sell(state, symbol, signal, book, pair_info, now):
    minimum, minimum_quantity, quantity_step, _, sell_fee_rate = _pair_minimums(pair_info)
    position = state["positions"][symbol]
    fill = simulate_market_order(
        "sell",
        position["quantity"],
        book,
        sell_fee_rate,
        minimum,
        minimum_quantity,
        quantity_step,
    )
    if fill is None:
        return None

    proceeds = fill.notional - fill.fee
    net_pnl = proceeds - position["entry_notional"] - position["entry_fee"]
    state["cash"] = float(Decimal(str(state["cash"])) + proceeds)
    state["closed_trades"].append(
        {
            "symbol": symbol,
            "entry_time": position["entry_time"],
            "exit_time": signal.candle_time,
            "quantity": float(fill.quantity),
            "entry_price": position["entry_price"],
            "exit_price": float(fill.average_price),
            "entry_fee": position["entry_fee"],
            "exit_fee": float(fill.fee),
            "net_pnl": float(net_pnl),
        }
    )
    del state["positions"][symbol]
    record = _order_record(symbol, "sell", fill, signal, now, "Donchian exit")
    record["net_pnl_idr"] = float(net_pnl)
    return record


def run_tick(store, client=None, settings=SETTINGS, scheduled_time=None):
    client = client or IndodaxClient()
    token = store.acquire_tick_lock()
    completed = 0
    events = []
    now_utc = datetime.now(timezone.utc)
    now = now_utc.isoformat()
    try:
        state = store.load_state() or new_state(settings.starting_cash)
        state.setdefault("initial_cash", float(settings.starting_cash))
        event_base = {
            "type": "call",
            "time": now,
            "scheduled_time": scheduled_time or now,
            "universe": list(settings.universe),
            "orders": [],
            "errors": [],
        }
        if store.is_kill_switch_on() or settings.kill_switch_default:
            event_base["status"] = "kill_switch"
            event_base["message"] = "Kill switch aktif; tidak ada order yang diproses"
            store.append_event(event_base)
            return {"status": "kill_switch", "processed": 0}

        try:
            server_time = client.server_time()
            pair_map = client.pairs()
            increments = client.price_increments()
        except ExchangeError as error:
            event_base["status"] = "error"
            event_base["errors"].append({"scope": "exchange", "message": str(error)})
            store.append_event(event_base)
            raise
        all_books = {}
        pair_info_map = {}
        price_increment_map = {}
        closed_by_symbol = {}
        decision_time = datetime.fromtimestamp(server_time, JAKARTA).isoformat()
        lookback = max(settings.entry_period, settings.exit_period, settings.sma_period) + 2
        start = server_time - (lookback + 2) * settings.timeframe_seconds

        for symbol in settings.universe:
            pair_info = pair_map.get(symbol)
            if not pair_info or pair_info.get("is_maintenance") or pair_info.get("is_market_suspended"):
                event = dict(event_base, symbol=symbol, status="rejected", reason="Pasangan tidak aktif")
                store.append_event(event)
                continue
            price_increment_key = f"{symbol[:-3].lower()}_idr"
            if price_increment_key not in increments:
                rejected = dict(
                    event_base,
                    symbol=symbol,
                    time=decision_time,
                    status="rejected",
                    reason="Kenaikan harga pasangan tidak tersedia",
                )
                store.append_event(rejected)
                continue
            try:
                _pair_minimums(pair_info)
                candles = client.history(symbol, settings.timeframe_api, start, server_time)
                closed = [
                    candle
                    for candle in candles
                    if candle.time + settings.timeframe_seconds <= server_time
                ]
                if len(closed) < lookback:
                    raise ExchangeError("Candle tertutup belum cukup untuk indikator")
                closed = closed[-lookback:]
                if not all(_valid_candle(candle) for candle in closed):
                    raise ExchangeError("Harga atau volume candle nol/tidak valid")
                if not has_contiguous_candles(closed[-lookback:], settings.timeframe_seconds):
                    raise ExchangeError("Ada candle hilang pada jendela indikator")
                latest = closed[-1]
                if server_time - (latest.time + settings.timeframe_seconds) > 2 * settings.timeframe_seconds:
                    raise ExchangeError("Candle tertutup terakhir lebih tua dari 2 timeframe")
                book = client.depth(symbol)
                spread = book.spread_fraction
                if not book.bids or not book.asks or spread is None:
                    raise ExchangeError("Order book kosong")
                if spread < 0 or spread > settings.max_spread_fraction:
                    raise ExchangeError(f"Spread {float(spread):.4%} melebihi batas")
                all_books[symbol] = book
                pair_info_map[symbol] = pair_info
                try:
                    price_increment = float(increments[price_increment_key])
                except (TypeError, ValueError) as error:
                    raise ExchangeError("Kenaikan harga pasangan tidak valid") from error
                if not math.isfinite(price_increment) or price_increment <= 0:
                    raise ExchangeError("Kenaikan harga pasangan tidak valid")
                price_increment_map[symbol] = price_increment
                closed_by_symbol[symbol] = closed
            except ExchangeError as error:
                event_base["errors"].append(
                    {"scope": "pair", "symbol": symbol, "message": str(error)}
                )
                rejected = dict(
                    event_base,
                    symbol=symbol,
                    time=decision_time,
                    status="rejected",
                    reason=str(error),
                )
                store.append_event(rejected)

        local_now = datetime.now(JAKARTA)
        day = local_now.date().isoformat()
        stale_marks = []
        for symbol, position in state["positions"].items():
            book = all_books.get(symbol)
            if book is not None and book.best_bid > 0:
                position["last_best_bid"] = float(book.best_bid)
                position["last_mark_time"] = decision_time
            else:
                stale_marks.append(
                    {
                        "symbol": symbol,
                        "last_best_bid": position.get("last_best_bid"),
                        "last_mark_time": position.get("last_mark_time"),
                    }
                )
                if position.get("last_best_bid") is None:
                    event_base["errors"].append(
                        {
                            "scope": "mark",
                            "symbol": symbol,
                            "message": "Best bid posisi tidak tersedia; equity tidak dapat dinilai",
                        }
                    )
        equity = _mark_equity(state, all_books)
        daily_equity_capture = None
        if not state.get("daily") or state["daily"].get("date") != day:
            state["daily"] = {"date": day, "start_equity": float(equity)}
            daily_equity_capture = {"date": day, "time": now, "equity": float(equity)}
            state.setdefault("daily_equity", []).append(daily_equity_capture)
            state["daily_equity"] = state["daily_equity"][-400:]
        daily_start = Decimal(str(state["daily"]["start_equity"]))
        daily_loss_hit = daily_start > 0 and equity <= daily_start * Decimal(
            1 - settings.daily_loss_limit
        )

        for symbol, candles in closed_by_symbol.items():
            latest = candles[-1]
            last_processed = state.setdefault("processed", {}).get(symbol, -1)
            if latest.time <= last_processed:
                duplicate = dict(
                    event_base,
                    symbol=symbol,
                    time=decision_time,
                    candle_time=latest.time,
                    status="duplicate",
                    reason="Candle sudah pernah diproses",
                )
                store.append_event(duplicate)
                continue

            signal = signal_for(
                candles,
                settings.entry_period,
                settings.exit_period,
                settings.sma_period,
            )
            book = all_books[symbol]
            pair_info = pair_info_map[symbol]
            order = None
            reason = signal.action
            if signal.action == "sell" and symbol in state["positions"]:
                order = _sell(state, symbol, signal, book, pair_info, decision_time)
                if order is None:
                    reason = "Jual ditolak: likuiditas book atau minimum order tidak cukup"
            elif signal.action == "buy" and symbol not in state["positions"]:
                reason = "Entry ditahan batas rugi harian" if daily_loss_hit else signal.action
                if not daily_loss_hit and len(state["positions"]) < settings.max_positions:
                    order = _buy(state, symbol, signal, book, pair_info, all_books, decision_time)
                    if order is None:
                        reason = "Beli ditolak: kas, likuiditas, atau minimum order tidak cukup"
                elif not daily_loss_hit:
                    reason = "Entry ditahan batas jumlah posisi"

            event = dict(
                event_base,
                symbol=symbol,
                time=decision_time,
                candle_time=latest.time,
                signal=signal.action,
                signal_price=signal.close,
                entry_level=signal.entry_level,
                exit_level=signal.exit_level,
                sma=signal.sma,
                best_bid=float(book.best_bid),
                best_ask=float(book.best_ask),
                spread_fraction=float(book.spread_fraction),
                price_increment=price_increment_map[symbol],
                book_snapshot={
                    "bids": [[str(price), str(amount)] for price, amount in book.bids],
                    "asks": [[str(price), str(amount)] for price, amount in book.asks],
                },
                daily_loss_limit_hit=bool(daily_loss_hit),
                status="processed",
                reason=reason,
                orders=[order] if order else [],
            )
            state["processed"][symbol] = latest.time
            committed = store.commit(state, event, symbol, latest.time)
            if committed:
                completed += 1
                events.append(event)

        store.save_state(state)
        event_base["status"] = "complete"
        event_base["processed_symbols"] = completed
        event_base["equity_idr"] = float(_mark_equity(state, all_books))
        event_base["cash_idr"] = state["cash"]
        event_base["daily_loss_limit_hit"] = bool(daily_loss_hit)
        event_base["daily_equity_capture"] = daily_equity_capture
        event_base["stale_marks"] = stale_marks
        store.append_event(event_base)
        return {
            "status": "complete",
            "time": now,
            "processed": completed,
            "equity_idr": event_base["equity_idr"],
            "cash_idr": state["cash"],
            "orders": sum(len(event["orders"]) for event in events),
        }
    finally:
        store.release_tick_lock(token)
