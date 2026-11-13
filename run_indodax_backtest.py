import argparse
import json
import math
import statistics
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_bot.execution import round_quantity
from crypto_bot.settings import SETTINGS
from crypto_bot.strategy import Candle, has_contiguous_candles, signal_for


JAKARTA = timezone(timedelta(hours=7))


def _dt(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Waktu tidak berisi zona waktu: {value}")
    return parsed.astimezone(timezone.utc)


def _median(values):
    valid = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(valid) if valid else None


def execution_calibration(snapshots, symbols, min_samples):
    calibration = {}
    failures = []
    for symbol in symbols:
        samples = [
            item
            for item in snapshots
            if item.get("symbol") == symbol and item.get("status") == "ok"
        ]
        days = {
            _dt(item["time"]).date().isoformat()
            for item in samples
            if item.get("time")
        }
        buy_slippage = _median(item.get("buy_slippage_fraction") for item in samples)
        sell_slippage = _median(item.get("sell_slippage_fraction") for item in samples)
        spread = _median(item.get("spread_fraction") for item in samples)
        if len(samples) < min_samples or len(days) < 2:
            failures.append(
                f"{symbol}: perlu minimal {min_samples} snapshot valid pada setidaknya 2 hari; "
                f"tersedia {len(samples)} snapshot dari {len(days)} hari"
            )
            continue
        if None in (buy_slippage, sell_slippage, spread):
            failures.append(f"{symbol}: data slippage/spread tidak lengkap")
            continue
        calibration[symbol] = {
            "snapshot_count": len(samples),
            "sample_days": len(days),
            "median_spread_fraction": spread,
            "median_buy_slippage_fraction": buy_slippage,
            "median_sell_slippage_fraction": sell_slippage,
            "order_size_idr": _median(item.get("order_size_idr") for item in samples),
            "sample_start": min(_dt(item["time"]) for item in samples).isoformat(),
            "sample_end": max(_dt(item["time"]) for item in samples).isoformat(),
        }
    if failures:
        raise ValueError("Snapshot book belum cukup untuk backtest berbiaya: " + "; ".join(failures))
    return calibration


def _candle(item):
    return Candle(
        time=int(item["time"]),
        open=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        close=float(item["close"]),
        volume=float(item["volume"]),
    )


def _marked_equity(cash, positions, marks, calibration):
    total = Decimal(str(cash))
    for symbol, position in positions.items():
        close = marks.get(symbol)
        if close is None:
            close = Decimal(str(position["last_close"]))
        spread = Decimal(str(calibration[symbol]["median_spread_fraction"]))
        bid = close * (Decimal(1) - spread / Decimal(2))
        total += Decimal(str(position["quantity"])) * bid
    return total


def _metrics(equity_curve, trades, orders, start_equity, end_equity, start_time, end_time):
    values = [float(value) for value in equity_curve]
    peak = values[0] if values else float(start_equity)
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    pnl = [float(trade["net_pnl"]) for trade in trades]
    winners = [value for value in pnl if value > 0]
    losers = [value for value in pnl if value < 0]
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    days = max((end_time - start_time).total_seconds() / 86400, 0)
    years = days / 365.25
    total_return = float(end_equity / Decimal(str(start_equity)) - 1)
    cagr = (float(end_equity / Decimal(str(start_equity))) ** (1 / years) - 1) if years > 0 and end_equity > 0 else None
    gross_profit = sum(
        max(
            0.0,
            float(trade["net_pnl"]) + float(trade["entry_fee"]) + float(trade["exit_fee"]),
        )
        for trade in trades
    )
    fees = sum(float(order["fee_idr"]) for order in orders)
    spread_slippage = sum(float(order["spread_slippage_idr"]) for order in orders)
    return {
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "profit_factor": gross_wins / gross_losses if gross_losses else ("Infinity" if gross_wins else None),
        "win_rate": len(winners) / len(trades) if trades else None,
        "average_win_idr": sum(winners) / len(winners) if winners else None,
        "average_loss_idr": sum(losers) / len(losers) if losers else None,
        "trade_count": len(trades),
        "order_count": len(orders),
        "gross_profit_idr": gross_profit,
        "fee_idr": fees,
        "spread_slippage_idr": spread_slippage,
        "cost_to_gross_profit": (fees + spread_slippage) / gross_profit if gross_profit > 0 else None,
    }


def compare_forward_signals(expected_signals, forward_log, timeframe_seconds):
    start = _dt(forward_log.get("forward_start_at"))
    end = _dt(forward_log.get("generated_at"))
    if end <= start:
        raise ValueError("generated_at harus setelah forward_start_at")
    start_ts = start.timestamp()
    end_ts = end.timestamp()
    expected = {
        (item["symbol"], int(item["candle_time"])): item["signal"]
        for item in expected_signals
        if start_ts <= int(item["candle_time"]) + timeframe_seconds <= end_ts
    }
    actual = {}
    duplicate_count = 0
    events = forward_log.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Field events pada forward log harus berupa array")
    for event in events:
        if (
            not isinstance(event, dict)
            or event.get("status") != "processed"
            or event.get("signal") not in {"buy", "sell", "hold"}
            or not event.get("symbol")
            or event.get("candle_time") is None
        ):
            continue
        key = (event["symbol"], int(event["candle_time"]))
        close_time = key[1] + timeframe_seconds
        if not start_ts <= close_time <= end_ts:
            continue
        if key in actual:
            duplicate_count += 1
            continue
        actual[key] = event["signal"]

    compared = sorted(expected.keys() & actual.keys())
    matched = sum(expected[key] == actual[key] for key in compared)
    mismatches = [
        {
            "symbol": symbol,
            "candle_time": candle_time,
            "backtest_signal": expected[(symbol, candle_time)],
            "forward_signal": actual[(symbol, candle_time)],
        }
        for symbol, candle_time in compared
        if expected[(symbol, candle_time)] != actual[(symbol, candle_time)]
    ]
    missing = [
        {"symbol": symbol, "candle_time": candle_time}
        for symbol, candle_time in sorted(expected.keys() - actual.keys())
    ]
    extra = [
        {"symbol": symbol, "candle_time": candle_time}
        for symbol, candle_time in sorted(actual.keys() - expected.keys())
    ]
    expected_count = len(expected)
    compared_count = len(compared)
    if expected_count and matched == expected_count and not extra and not duplicate_count:
        status = "complete"
    elif compared_count < expected_count or not expected_count:
        status = "incomplete"
    else:
        status = "mismatch"
    return {
        "status": status,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "expected_signals": expected_count,
        "observed_signals": len(actual),
        "compared_signals": compared_count,
        "matched_signals": matched,
        "coverage_rate": compared_count / expected_count if expected_count else None,
        "signal_match_rate": matched / expected_count if expected_count and compared_count else None,
        "duplicate_forward_signals": duplicate_count,
        "mismatches": mismatches,
        "missing_signals": missing,
        "extra_signals": extra,
    }


def compare_forward_fills(backtest_orders, forward_log, timeframe_seconds):
    forward_orders = {}
    start = _dt(forward_log.get("forward_start_at")).timestamp()
    end = _dt(forward_log.get("generated_at")).timestamp()
    for event in forward_log.get("events", []):
        if (
            not isinstance(event, dict)
            or event.get("status") != "processed"
            or event.get("candle_time") is None
        ):
            continue
        candle_close = int(event["candle_time"]) + timeframe_seconds
        if not start <= candle_close <= end:
            continue
        for order in event.get("orders", []):
            if not isinstance(order, dict) or order.get("side") not in {"buy", "sell"}:
                continue
            key = (order.get("symbol") or event.get("symbol"), order["side"], candle_close)
            if key[0] and key not in forward_orders:
                forward_orders[key] = (event, order)

    comparisons = []
    matched_keys = set()
    delay_unavailable = 0
    for order in backtest_orders:
        key = (order["symbol"], order["side"], int(order["time"]))
        match = forward_orders.get(key)
        if match is None:
            continue
        matched_keys.add(key)
        event, forward_order = match
        modeled_fill = float(order["fill_price"])
        actual_fill = float(forward_order["average_fill"])
        delay_seconds = None
        try:
            delay_seconds = (
                _dt(forward_order["time"]).timestamp() - int(event["candle_time"]) - timeframe_seconds
            )
        except (KeyError, TypeError, ValueError):
            delay_unavailable += 1
        comparisons.append(
            {
                "symbol": key[0],
                "side": key[1],
                "candle_close_time": key[2],
                "backtest_fill": modeled_fill,
                "forward_fill": actual_fill,
                "fill_difference_idr": actual_fill - modeled_fill,
                "fill_difference_fraction": actual_fill / modeled_fill - 1 if modeled_fill else None,
                "backtest_fee_idr": float(order.get("fee_idr") or 0),
                "forward_fee_idr": float(forward_order.get("fee_idr") or 0),
                "fee_difference_idr": float(forward_order.get("fee_idr") or 0)
                - float(order.get("fee_idr") or 0),
                "forward_spread_fraction": event.get("spread_fraction"),
                "forward_fill_vs_signal_fraction": forward_order.get("fill_vs_signal_fraction"),
                "delay_seconds_after_candle_close": delay_seconds,
            }
        )
    return {
        "matched_orders": len(comparisons),
        "unmatched_backtest_orders": max(0, len(backtest_orders) - len(comparisons)),
        "unmatched_forward_orders": len(forward_orders.keys() - matched_keys),
        "delay_unavailable_orders": delay_unavailable,
        "comparisons": comparisons,
    }


def backtest(
    payload,
    snapshots,
    start,
    end,
    starting_cash=100_000_000,
    min_samples=6,
    forward_log=None,
):
    symbols = list(payload["candidate_universe"])
    calibration = execution_calibration(snapshots, symbols, min_samples)
    bars = {
        symbol: [_candle(item) for item in payload["candles"].get(symbol, [])]
        for symbol in symbols
    }
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    bars = {
        symbol: [
            candle
            for candle in items
            if candle.time + payload["timeframe_seconds"] <= end_ts
        ]
        for symbol, items in bars.items()
    }
    timeframes = {int(payload["timeframe_seconds"])}
    if timeframes != {4 * 60 * 60}:
        raise ValueError("Backtest ini disamakan dengan bot 4 jam; timeframe data tidak cocok")
    all_times = sorted(
        {
            candle.time
            for items in bars.values()
            for candle in items
            if candle.time + payload["timeframe_seconds"] >= start_ts
        }
    )
    if not all_times:
        raise ValueError("Tidak ada candle tertutup pada window backtest")

    lookup = {symbol: {item.time: index for index, item in enumerate(items)} for symbol, items in bars.items()}
    cash = Decimal(str(starting_cash))
    positions = {}
    marks = {}
    daily = None
    daily_equity = []
    equity_curve = [float(cash)]
    trades = []
    orders = []
    signal_rows = []
    skipped_gaps = 0
    last_equity = Decimal(str(starting_cash))
    for candle_time in all_times:
        candle_close_time = candle_time + payload["timeframe_seconds"]
        if candle_close_time < start_ts:
            continue
        day = datetime.fromtimestamp(candle_close_time, JAKARTA).date().isoformat()
        current_indices = {}
        signals = {}
        for symbol in symbols:
            index = lookup[symbol].get(candle_time)
            if index is None:
                continue
            current_indices[symbol] = index
            marks[symbol] = Decimal(str(bars[symbol][index].close))
            history = bars[symbol][: index + 1]
            required = max(SETTINGS.entry_period, SETTINGS.exit_period, SETTINGS.sma_period) + 1
            if len(history) < required:
                continue
            window = history[-required:]
            if not has_contiguous_candles(window, payload["timeframe_seconds"]):
                skipped_gaps += 1
                continue
            signal = signal_for(
                window,
                SETTINGS.entry_period,
                SETTINGS.exit_period,
                SETTINGS.sma_period,
            )
            signals[symbol] = signal
            signal_rows.append(
                {
                    "symbol": symbol,
                    "candle_time": signal.candle_time,
                    "signal": signal.action,
                }
            )

        equity_before_orders = _marked_equity(cash, positions, marks, calibration)
        if daily is None or daily["date"] != day:
            daily = {"date": day, "start_equity": equity_before_orders}
            daily_equity.append(
                {
                    "date": day,
                    "time": datetime.fromtimestamp(candle_close_time, timezone.utc).isoformat(),
                    "equity": float(equity_before_orders),
                }
            )
        daily_loss_hit = (
            daily["start_equity"] > 0
            and equity_before_orders
            <= daily["start_equity"] * Decimal(str(1 - SETTINGS.daily_loss_limit))
        )

        for symbol in symbols:
            signal = signals.get(symbol)
            if signal is None:
                continue
            market = calibration[symbol]
            spread_cost = Decimal(str(market["median_spread_fraction"])) / Decimal(2)
            if signal.action == "sell" and symbol in positions:
                position = positions[symbol]
                close = Decimal(str(signal.close))
                slip = Decimal(str(market["median_sell_slippage_fraction"]))
                fill_price = close * (Decimal(1) - spread_cost - slip)
                quantity = Decimal(str(position["quantity"]))
                notional = quantity * fill_price
                if notional < Decimal(str(SETTINGS.minimum_order_idr)):
                    continue
                increment = Decimal(str(payload["pair_metadata"][symbol]["quantity_increment"]))
                quantity = round_quantity(quantity, increment)
                fee_rate = max(
                    Decimal(str(SETTINGS.all_in_taker_sell_fee_rate)),
                    Decimal(str(payload["pair_metadata"][symbol]["trade_fee_percent_taker"])) / Decimal(100),
                )
                notional = quantity * fill_price
                fee = notional * fee_rate
                proceeds = notional - fee
                net_pnl = proceeds - Decimal(str(position["entry_notional"])) - Decimal(str(position["entry_fee"]))
                orders.append(
                    {
                        "time": candle_close_time,
                        "symbol": symbol,
                        "side": "sell",
                        "quantity": float(quantity),
                        "signal_price": signal.close,
                        "fill_price": float(fill_price),
                        "fee_idr": float(fee),
                        "spread_slippage_idr": float(quantity * close * (spread_cost + slip)),
                    }
                )
                trades.append(
                    {
                        "symbol": symbol,
                        "entry_time": position["entry_time"],
                        "exit_time": candle_close_time,
                        "quantity": float(quantity),
                        "entry_notional": position["entry_notional"],
                        "exit_notional": float(notional),
                        "entry_fee": position["entry_fee"],
                        "exit_fee": float(fee),
                        "net_pnl": float(net_pnl),
                    }
                )
                cash += proceeds
                del positions[symbol]
            elif signal.action == "buy" and symbol not in positions and not daily_loss_hit:
                if len(positions) >= SETTINGS.max_positions:
                    continue
                close = Decimal(str(signal.close))
                slip = Decimal(str(market["median_buy_slippage_fraction"]))
                fill_price = close * (Decimal(1) + spread_cost + slip)
                equity = _marked_equity(cash, positions, marks, calibration)
                budget = min(
                    equity * Decimal(str(SETTINGS.max_position_fraction)),
                    cash,
                )
                fee_rate = max(
                    Decimal(str(SETTINGS.all_in_taker_buy_fee_rate)),
                    Decimal(str(payload["pair_metadata"][symbol]["trade_fee_percent_taker"])) / Decimal(100),
                )
                budget /= Decimal(1) + fee_rate
                increment = Decimal(str(payload["pair_metadata"][symbol]["quantity_increment"]))
                minimum_qty = Decimal(str(payload["pair_metadata"][symbol]["trade_min_traded_currency"]))
                minimum_value = max(
                    Decimal(str(SETTINGS.minimum_order_idr)),
                    Decimal(str(payload["pair_metadata"][symbol]["trade_min_base_currency"])),
                )
                quantity = round_quantity(budget / fill_price, increment)
                notional = quantity * fill_price
                fee = notional * fee_rate
                if quantity < minimum_qty or notional < minimum_value or notional + fee > cash:
                    continue
                positions[symbol] = {
                    "quantity": float(quantity),
                    "entry_time": candle_close_time,
                    "entry_notional": float(notional),
                    "entry_fee": float(fee),
                    "last_close": float(close),
                }
                orders.append(
                    {
                        "time": candle_close_time,
                        "symbol": symbol,
                        "side": "buy",
                        "quantity": float(quantity),
                        "signal_price": signal.close,
                        "fill_price": float(fill_price),
                        "fee_idr": float(fee),
                        "spread_slippage_idr": float(quantity * close * (spread_cost + slip)),
                    }
                )
                cash -= notional + fee

        for symbol, position in positions.items():
            if symbol in marks:
                position["last_close"] = float(marks[symbol])
        last_equity = _marked_equity(cash, positions, marks, calibration)
        equity_curve.append(float(last_equity))

    timeframe_seconds = payload["timeframe_seconds"]
    actual_start = datetime.fromtimestamp(all_times[0] + timeframe_seconds, timezone.utc)
    end_time = datetime.fromtimestamp(all_times[-1] + timeframe_seconds, timezone.utc)
    metrics = _metrics(equity_curve, trades, orders, starting_cash, last_equity, actual_start, end_time)
    btc = bars.get("BTCIDR", [])
    btc_window = [
        item
        for item in btc
        if start_ts <= item.time + payload["timeframe_seconds"] <= end_ts
    ]
    benchmark = (
        btc_window[-1].close / btc_window[0].close - 1
        if len(btc_window) >= 2 and btc_window[0].close > 0
        else None
    )
    metrics["btc_buy_and_hold"] = benchmark
    result = {
        "source": "Indodax public history_v2 candles; measured current order-book medians; dry-run fee model",
        "start": actual_start.isoformat(),
        "end": end_time.isoformat(),
        "timeframe": "4h",
        "universe": symbols,
        "starting_cash": starting_cash,
        "metrics": metrics,
        "execution_calibration": calibration,
        "daily_equity": daily_equity,
        "trades": trades,
        "orders": orders,
        "candle_gaps_skipped": skipped_gaps,
        "limitations": [
            "Measured live order-book spread and slippage are applied as median fractions across historical bars; historical order-book snapshots are unavailable.",
            "The public API metadata is current, not a historical archive of fee or listing changes.",
            "The archived candidate universe contains BTCIDR, ETHIDR, and XRPIDR only; pairs delisted before collection are not included.",
        ],
    }
    if forward_log is not None:
        result["signal_comparison"] = compare_forward_signals(
            signal_rows, forward_log, payload["timeframe_seconds"]
        )
        result["execution_comparison"] = compare_forward_fills(
            orders, forward_log, payload["timeframe_seconds"]
        )
    return result


def main():
    parser = argparse.ArgumentParser(description="Backtest the shared Donchian/SMA strategy with measured Indodax execution costs.")
    parser.add_argument("--data", type=Path, default=Path("data/indodax_4h_history.json"))
    parser.add_argument("--snapshots", type=Path, default=Path("data/orderbook_snapshots.json"))
    parser.add_argument("--start", type=_dt, default=_dt("2023-11-12T00:00:00Z"))
    parser.add_argument("--end", type=_dt, default=datetime.now(timezone.utc))
    parser.add_argument("--minimum-snapshots", type=int, default=6)
    parser.add_argument(
        "--forward-log",
        type=Path,
        help="Dashboard export forward test; adds 14-day signal and fill comparisons",
    )
    parser.add_argument("--output", type=Path, default=Path("crypto_bot/backtest_result.json"))
    args = parser.parse_args()
    try:
        history = json.loads(args.data.read_text(encoding="utf-8"))
        raw_snapshots = json.loads(args.snapshots.read_text(encoding="utf-8"))
        snapshots = raw_snapshots.get("snapshots", raw_snapshots) if isinstance(raw_snapshots, dict) else raw_snapshots
        if not isinstance(snapshots, list):
            raise ValueError("Snapshot harus berupa array atau export dashboard dengan field snapshots")
        result = backtest(history, snapshots, args.start, args.end, min_samples=args.minimum_snapshots)
        if args.forward_log:
            forward_log = json.loads(args.forward_log.read_text(encoding="utf-8"))
            if not isinstance(forward_log, dict):
                raise ValueError("Forward log harus berupa object hasil ekspor dashboard")
            forward_start = _dt(forward_log.get("forward_start_at"))
            forward_end = _dt(forward_log.get("generated_at"))
            if forward_end <= forward_start:
                raise ValueError("Forward log harus memuat generated_at setelah forward_start_at")
            forward_result = backtest(
                history,
                snapshots,
                forward_start,
                forward_end,
                min_samples=args.minimum_snapshots,
                forward_log=forward_log,
            )
            comparison = forward_result["signal_comparison"]
            fill_comparison = forward_result["execution_comparison"]
            result["forward_window"] = {
                "start": forward_result["start"],
                "end": forward_result["end"],
                "total_return": forward_result["metrics"]["total_return"],
                "max_drawdown": forward_result["metrics"]["max_drawdown"],
                "trade_count": forward_result["metrics"]["trade_count"],
                "signal_match_rate": comparison["signal_match_rate"],
                "signal_comparison_status": comparison["status"],
                "signal_comparison": comparison,
                "execution_comparison": fill_comparison,
            }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.output), "metrics": result["metrics"], "calibration": result["execution_calibration"]}, indent=2))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
