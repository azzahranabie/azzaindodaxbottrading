from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    action: str
    candle_time: int
    close: float
    entry_level: float | None = None
    exit_level: float | None = None
    sma: float | None = None


def signal_for(
    candles: Sequence[Candle],
    entry_period: int = 40,
    exit_period: int = 30,
    sma_period: int = 150,
) -> Signal:
    """Evaluate one closed candle; both backtest and live bot call this function."""
    required = max(entry_period, exit_period, sma_period)
    if len(candles) < required + 1:
        raise ValueError(f"Sinyal memerlukan sedikitnya {required + 1} candle tertutup")

    current = candles[-1]
    prior = candles[:-1]
    entry = max(candle.close for candle in prior[-entry_period:])
    exit_ = min(candle.close for candle in prior[-exit_period:])
    average = sum(candle.close for candle in candles[-sma_period:]) / sma_period

    if current.close > entry and current.close > average:
        action = "buy"
    elif current.close < exit_:
        action = "sell"
    else:
        action = "hold"
    return Signal(action, current.time, current.close, entry, exit_, average)


def has_contiguous_candles(candles: Sequence[Candle], timeframe_seconds: int) -> bool:
    return all(
        later.time - earlier.time == timeframe_seconds
        for earlier, later in zip(candles, candles[1:])
    )
