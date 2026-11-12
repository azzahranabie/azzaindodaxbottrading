import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    timeframe_seconds: int = 4 * 60 * 60
    timeframe_api: str = "240"
    entry_period: int = 40
    exit_period: int = 30
    sma_period: int = 150
    starting_cash: float = 100_000_000
    max_positions: int = 3
    max_position_fraction: float = 0.10
    daily_loss_limit: float = 0.02
    max_spread_fraction: float = 0.02
    minimum_order_idr: float = 25_000
    all_in_taker_buy_fee_rate: float = float(
        os.getenv("INDODAX_ALL_IN_TAKER_BUY_FEE_RATE", "0.0020555")
    )
    all_in_taker_sell_fee_rate: float = float(
        os.getenv("INDODAX_ALL_IN_TAKER_SELL_FEE_RATE", "0.0041555")
    )
    max_history_bars: int = 210

    @property
    def universe(self):
        pairs = tuple(
            item.strip().upper()
            for item in os.getenv("INDODAX_UNIVERSE", "BTCIDR,ETHIDR,XRPIDR").split(",")
            if item.strip()
        )
        if not 3 <= len(pairs) <= 10 or len(set(pairs)) != len(pairs):
            raise ValueError("INDODAX_UNIVERSE harus berisi 3-10 pasangan unik")
        if any(not item.endswith("IDR") or not item[:-3].isalnum() for item in pairs):
            raise ValueError("Universe hanya boleh berisi pasangan IDR Indodax yang valid")
        return pairs

    @property
    def cron_secret(self):
        return os.getenv("CRON_SECRET", "")

    @property
    def kill_switch_default(self):
        return os.getenv("KILL_SWITCH", "false").strip().lower() in {"1", "true", "yes"}


SETTINGS = Settings()
