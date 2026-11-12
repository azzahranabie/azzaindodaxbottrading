import unittest

from run_indodax_backtest import execution_calibration


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


if __name__ == "__main__":
    unittest.main()
