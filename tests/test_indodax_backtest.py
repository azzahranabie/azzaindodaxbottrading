import unittest
from datetime import datetime, timezone

from run_indodax_backtest import (
    backtest,
    compare_forward_fills,
    compare_forward_signals,
    execution_calibration,
)


class ExecutionCalibrationTests(unittest.TestCase):
    def test_requires_observations_on_multiple_days(self):
        snapshots = [
            {
                "symbol": "BTCIDR",
                "status": "ok",
                "time": f"2026-11-12T{hour:02d}:00:00+00:00",
                "spread_fraction": 0.001,
                "buy_slippage_fraction": 0.0001,
                "sell_slippage_fraction": 0.0002,
                "order_size_idr": 10_000_000,
            }
            for hour in (0, 4, 8, 12, 16, 20)
        ]

        with self.assertRaisesRegex(ValueError, "setidaknya 2 hari"):
            execution_calibration(snapshots, ["BTCIDR"], min_samples=6)

    def test_reports_median_costs_for_valid_multiday_snapshots(self):
        snapshots = []
        for day in ("12", "13"):
            for hour in ("00", "08", "16"):
                snapshots.append(
                    {
                        "symbol": "BTCIDR",
                        "status": "ok",
                        "time": f"2026-11-{day}T{hour}:00:00+00:00",
                        "spread_fraction": 0.001,
                        "buy_slippage_fraction": 0.0001,
                        "sell_slippage_fraction": 0.0002,
                        "order_size_idr": 10_000_000,
                    }
                )

        result = execution_calibration(snapshots, ["BTCIDR"], min_samples=6)

        self.assertEqual(result["BTCIDR"]["snapshot_count"], 6)
        self.assertEqual(result["BTCIDR"]["sample_days"], 2)
        self.assertAlmostEqual(result["BTCIDR"]["median_spread_fraction"], 0.001)
        self.assertAlmostEqual(result["BTCIDR"]["median_buy_slippage_fraction"], 0.0001)


class ForwardComparisonTests(unittest.TestCase):
    def setUp(self):
        self.start = "2026-11-12T00:00:00+00:00"
        self.end = "2026-11-12T08:00:00+00:00"
        self.first_candle = int(datetime(2026, 11, 12, 0, tzinfo=timezone.utc).timestamp())
        self.second_candle = self.first_candle + 4 * 60 * 60

    def test_identical_full_window_signals_pass_at_one_hundred_percent(self):
        expected = [
            {"symbol": "BTCIDR", "candle_time": self.first_candle, "signal": "hold"},
            {"symbol": "BTCIDR", "candle_time": self.second_candle, "signal": "buy"},
        ]
        log = {
            "forward_start_at": self.start,
            "generated_at": self.end,
            "events": [
                {
                    "symbol": item["symbol"],
                    "candle_time": item["candle_time"],
                    "signal": item["signal"],
                    "status": "processed",
                }
                for item in expected
            ],
        }

        result = compare_forward_signals(expected, log, 4 * 60 * 60)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["signal_match_rate"], 1.0)
        self.assertEqual(result["coverage_rate"], 1.0)

    def test_missing_and_different_signals_are_not_reported_as_a_pass(self):
        expected = [
            {"symbol": "BTCIDR", "candle_time": self.first_candle, "signal": "hold"},
            {"symbol": "BTCIDR", "candle_time": self.second_candle, "signal": "buy"},
        ]
        log = {
            "forward_start_at": self.start,
            "generated_at": self.end,
            "events": [
                {
                    "symbol": "BTCIDR",
                    "candle_time": self.first_candle,
                    "signal": "sell",
                    "status": "processed",
                }
            ],
        }

        result = compare_forward_signals(expected, log, 4 * 60 * 60)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["coverage_rate"], 0.5)
        self.assertEqual(result["signal_match_rate"], 0.0)
        self.assertEqual(len(result["mismatches"]), 1)
        self.assertEqual(len(result["missing_signals"]), 1)

    def test_fill_difference_includes_spread_and_execution_delay(self):
        close_time = self.first_candle + 4 * 60 * 60
        log = {
            "forward_start_at": self.start,
            "generated_at": self.end,
            "events": [
                {
                    "status": "processed",
                    "symbol": "BTCIDR",
                    "candle_time": self.first_candle,
                    "spread_fraction": 0.001,
                    "orders": [
                        {
                            "symbol": "BTCIDR",
                            "side": "buy",
                            "time": datetime.fromtimestamp(
                                close_time + 60, timezone.utc
                            ).isoformat(),
                            "average_fill": 101.0,
                            "fee_idr": 0.5,
                            "fill_vs_signal_fraction": 0.01,
                        }
                    ],
                }
            ],
        }

        result = compare_forward_fills(
            [
                {
                    "symbol": "BTCIDR",
                    "side": "buy",
                    "time": close_time,
                    "fill_price": 100.0,
                    "fee_idr": 0.4,
                }
            ],
            log,
            4 * 60 * 60,
        )

        self.assertEqual(result["matched_orders"], 1)
        comparison = result["comparisons"][0]
        self.assertEqual(comparison["fill_difference_idr"], 1.0)
        self.assertAlmostEqual(comparison["fill_difference_fraction"], 0.01)
        self.assertEqual(comparison["forward_spread_fraction"], 0.001)
        self.assertAlmostEqual(comparison["fee_difference_idr"], 0.1)
        self.assertEqual(comparison["delay_seconds_after_candle_close"], 60)

    def test_backtest_runs_shared_signals_and_attaches_forward_comparison(self):
        timeframe = 4 * 60 * 60
        first_candle = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp())
        candles = []
        for index in range(152):
            close = 101.0 if index == 151 else 100.0
            candles.append(
                {
                    "time": first_candle + index * timeframe,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1.0,
                }
            )
        snapshots = [
            {
                "symbol": "BTCIDR",
                "status": "ok",
                "time": f"2026-11-{day}T{hour:02d}:00:00+00:00",
                "spread_fraction": 0.001,
                "buy_slippage_fraction": 0.0001,
                "sell_slippage_fraction": 0.0002,
                "order_size_idr": 10_000_000,
            }
            for day in ("12", "13")
            for hour in (0, 8, 16)
        ]
        start = datetime.fromtimestamp(
            first_candle + 150 * timeframe + timeframe, timezone.utc
        )
        end = datetime.fromtimestamp(
            first_candle + 151 * timeframe + timeframe, timezone.utc
        )
        forward_log = {
            "forward_start_at": start.isoformat(),
            "generated_at": end.isoformat(),
            "events": [
                {
                    "type": "call",
                    "symbol": "BTCIDR",
                    "candle_time": candles[index]["time"],
                    "signal": "hold" if index == 150 else "buy",
                    "status": "processed",
                    "spread_fraction": 0.001,
                    "orders": (
                        [
                            {
                                "symbol": "BTCIDR",
                                "side": "buy",
                                "time": datetime.fromtimestamp(
                                    int(candles[index]["time"]) + timeframe + 60,
                                    timezone.utc,
                                ).isoformat(),
                                "average_fill": 101.1,
                                "fee_idr": 20_000,
                                "fill_vs_signal_fraction": 0.001,
                            }
                        ]
                        if index == 151
                        else []
                    ),
                }
                for index in (150, 151)
            ],
        }
        payload = {
            "candidate_universe": ["BTCIDR"],
            "timeframe_seconds": timeframe,
            "candles": {"BTCIDR": candles},
            "pair_metadata": {
                "BTCIDR": {
                    "quantity_increment": 0.001,
                    "trade_min_traded_currency": 0.001,
                    "trade_min_base_currency": 25_000,
                    "trade_fee_percent_taker": 0.2,
                }
            },
        }

        result = backtest(
            payload,
            snapshots,
            start,
            end,
            min_samples=6,
            forward_log=forward_log,
        )

        self.assertEqual(result["signal_comparison"]["status"], "complete")
        self.assertEqual(result["signal_comparison"]["expected_signals"], 2)
        self.assertEqual(result["signal_comparison"]["signal_match_rate"], 1.0)
        self.assertEqual(result["metrics"]["trade_count"], 0)
        self.assertEqual(result["execution_comparison"]["matched_orders"], 1)
        self.assertEqual(
            result["execution_comparison"]["comparisons"][0][
                "delay_seconds_after_candle_close"
            ],
            60,
        )


if __name__ == "__main__":
    unittest.main()
