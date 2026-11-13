import argparse
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path


def _number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _max_drawdown(values):
    if not values:
        return None
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def recalculate(payload):
    if isinstance(payload, list):
        payload = {"events": payload}
    if not isinstance(payload, dict):
        raise ValueError("Log harus berupa object dashboard atau array events")

    events = payload.get("events") or []
    if not isinstance(events, list):
        raise ValueError("Field events harus berupa array")
    state = payload.get("state") or {}
    if not isinstance(state, dict):
        state = {}
    calls = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("type") == "call"
        and not event.get("symbol")
        and event.get("status") in {"complete", "error", "kill_switch"}
    ]

    start = _time(payload.get("forward_start_at"))
    captured_at = _time(payload.get("generated_at"))
    latest_call = max(
        (
            event
            for event in calls
            if event.get("status") == "complete"
            and math.isfinite(_number(event.get("equity_idr"), math.nan))
        ),
        key=lambda event: _time(event.get("time")) or datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )
    if captured_at is None:
        captured_at = _time(latest_call.get("time")) if latest_call else None
    initial_cash = _number(
        payload.get("initial_cash") or state.get("initial_cash") or 100_000_000
    )
    end_equity = _number(latest_call.get("equity_idr"), math.nan) if latest_call else math.nan
    total_return = end_equity / initial_cash - 1 if math.isfinite(end_equity) and initial_cash > 0 else None
    daily_equity = state.get("daily_equity") or []
    equity_values = [
        _number(point.get("equity"), math.nan)
        for point in daily_equity
        if isinstance(point, dict)
    ]
    equity_values = [value for value in equity_values if math.isfinite(value)]
    drawdown_curve = [initial_cash, *equity_values]
    if math.isfinite(end_equity):
        drawdown_curve.append(end_equity)
    max_drawdown = _max_drawdown(drawdown_curve) if equity_values or math.isfinite(end_equity) else None

    closed_trades = state.get("closed_trades") or []
    closed_trades = [trade for trade in closed_trades if isinstance(trade, dict)]
    has_forward_calls = bool(calls)
    pnl_values = [_number(trade.get("net_pnl"), math.nan) for trade in closed_trades]
    pnl_values = [value for value in pnl_values if math.isfinite(value)]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    if gross_losses:
        profit_factor = gross_wins / gross_losses
    elif gross_wins:
        profit_factor = "Infinity"
    else:
        profit_factor = None

    orders = [
        order
        for event in events
        if isinstance(event, dict)
        for order in event.get("orders", [])
        if isinstance(order, dict)
    ]
    fees = sum(_number(order.get("fee_idr")) for order in orders)
    spread_slippage = 0.0
    for order in orders:
        bid = _number(order.get("best_bid"), math.nan)
        ask = _number(order.get("best_ask"), math.nan)
        fill = _number(order.get("average_fill"), math.nan)
        quantity = _number(order.get("quantity"))
        midpoint = (bid + ask) / 2
        if not all(math.isfinite(value) for value in (midpoint, fill, quantity)):
            continue
        per_unit = fill - midpoint if order.get("side") == "buy" else midpoint - fill
        spread_slippage += max(0.0, per_unit) * quantity
    gross_profit = sum(
        max(
            0.0,
            _number(trade.get("net_pnl"))
            + _number(trade.get("entry_fee"))
            + _number(trade.get("exit_fee")),
        )
        for trade in closed_trades
    )
    cost_ratio = (fees + spread_slippage) / gross_profit if gross_profit > 0 else None
    snapshots = payload.get("snapshots") or []
    if not isinstance(snapshots, list):
        raise ValueError("Field snapshots harus berupa array")
    valid_snapshots = [item for item in snapshots if isinstance(item, dict) and item.get("status") == "ok"]

    def snapshot_median(field):
        values = [
            _number(item.get(field), math.nan)
            for item in valid_snapshots
        ]
        values = [value for value in values if math.isfinite(value)]
        return statistics.median(values) if values else None

    btc = [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("symbol") == "BTCIDR"
        and event.get("signal") in {"hold", "buy", "sell"}
        and math.isfinite(_number(event.get("signal_price"), math.nan))
    ]
    btc.sort(key=lambda event: _number(event.get("candle_time"), 0))
    benchmark = None
    if len(btc) > 1:
        first = _number(btc[0].get("signal_price"))
        last = _number(btc[-1].get("signal_price"))
        if first > 0:
            benchmark = last / first - 1

    interval_hours = _number(payload.get("schedule_interval_hours"), 4)
    planned_calls = None
    completed_slots = 0
    if start and captured_at and interval_hours > 0:
        interval = interval_hours * 3600
        first_slot = math.ceil(start.timestamp() / interval) * interval
        planned_calls = max(0, math.floor((captured_at.timestamp() - first_slot) / interval) + 1)
        scheduled_slots = {
            parsed.timestamp()
            for event in calls
            if (parsed := _time(event.get("scheduled_time") or event.get("time"))) is not None
        }
        completed_slots = sum(
            start.timestamp() <= timestamp <= captured_at.timestamp()
            for timestamp in scheduled_slots
        )

    error_count = sum(
        max(
            len(event.get("errors", [])) if isinstance(event.get("errors"), list) else 0,
            1 if event.get("status") == "error" else 0,
        )
        for event in calls
    )
    kill_switch_count = sum(
        event.get("type") == "kill_switch"
        or event.get("type") == "call" and event.get("status") == "kill_switch"
        for event in events
        if isinstance(event, dict)
    )
    return {
        "captured_at": captured_at.isoformat() if captured_at else None,
        "forward": {
            "initial_cash": initial_cash,
            "ending_equity": end_equity if math.isfinite(end_equity) else None,
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "trade_count": len(closed_trades) if has_forward_calls else None,
            "profit_factor": profit_factor,
            "win_rate": len(wins) / len(closed_trades) if closed_trades else None,
            "average_win": gross_wins / len(wins) if wins else None,
            "average_loss": sum(losses) / len(losses) if losses else None,
        },
        "execution": {
            "order_count": len(orders) if has_forward_calls else None,
            "fees": fees if has_forward_calls else None,
            "spread_slippage_estimate": spread_slippage if has_forward_calls else None,
            "cost_to_gross_profit": cost_ratio,
            "snapshot_count": len(valid_snapshots),
            "median_snapshot_spread": snapshot_median("spread_fraction"),
            "median_snapshot_buy_slippage": snapshot_median("buy_slippage_fraction"),
            "median_snapshot_sell_slippage": snapshot_median("sell_slippage_fraction"),
            "btc_buy_and_hold": benchmark,
            "return_gap_vs_btc": total_return - benchmark if total_return is not None and benchmark is not None else None,
        },
        "operations": {
            "completed_scheduled_slots": completed_slots,
            "planned_slots": planned_calls,
            "uptime": completed_slots / planned_calls if planned_calls else None,
            "api_errors": error_count,
            "kill_switch_uses": kill_switch_count,
        },
        "backtest": payload.get("backtest"),
        "backtest_status": payload.get("backtest_status", "Belum ada hasil terverifikasi"),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Hitung ulang metrik dashboard dari JSON log yang diekspor."
    )
    parser.add_argument("log", type=Path, help="File JSON hasil ekspor dashboard")
    parser.add_argument(
        "--output",
        type=Path,
        help="Lokasi file JSON hasil hitung ulang (default: tampilkan ke terminal)",
    )
    args = parser.parse_args()
    try:
        payload = json.loads(args.log.read_text(encoding="utf-8"))
        result = recalculate(payload)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
