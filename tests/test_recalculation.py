import unittest

from hitung_ulang import recalculate


class RecalculationTests(unittest.TestCase):
    def test_recalculates_return_and_counts_unique_scheduled_slots(self):
        payload = {
            "initial_cash": 100,
            "forward_start_at": "2026-11-12T00:00:00Z",
            "generated_at": "2026-11-12T08:00:00Z",
            "schedule_interval_hours": 4,
            "events": [
                {
                    "type": "call",
                    "status": "complete",
                    "time": "2026-11-12T04:01:00Z",
                    "scheduled_time": "2026-11-12T04:00:00Z",
                    "equity_idr": 110,
                    "orders": [],
                    "errors": [],
                },
                {
                    "type": "call",
                    "status": "complete",
                    "time": "2026-11-12T04:02:00Z",
                    "scheduled_time": "2026-11-12T04:00:00Z",
                    "equity_idr": 110,
                    "orders": [],
                    "errors": [],
                },
            ],
            "state": {"daily_equity": [{"equity": 100}], "closed_trades": []},
        }

        result = recalculate(payload)

        self.assertAlmostEqual(result["forward"]["total_return"], 0.1)
        self.assertEqual(result["operations"]["completed_scheduled_slots"], 1)
        self.assertEqual(result["operations"]["planned_slots"], 3)

    def test_missing_forward_data_is_not_reported_as_zero_return(self):
        result = recalculate({"events": [], "state": {}})

        self.assertIsNone(result["forward"]["total_return"])
        self.assertIsNone(result["forward"]["max_drawdown"])
        self.assertIsNone(result["forward"]["trade_count"])
        self.assertIsNone(result["execution"]["order_count"])
        self.assertIsNone(result["execution"]["fees"])
        self.assertIsNone(result["execution"]["spread_slippage_estimate"])

    def test_completed_call_with_no_trades_reports_measured_zero_costs(self):
        result = recalculate(
            {
                "events": [
                    {
                        "type": "call",
                        "status": "complete",
                        "time": "2026-11-12T04:00:00Z",
                        "orders": [],
                        "errors": [],
                    }
                ],
                "state": {"closed_trades": []},
            }
        )

        self.assertEqual(result["forward"]["trade_count"], 0)
        self.assertEqual(result["execution"]["fees"], 0)
        self.assertEqual(result["execution"]["spread_slippage_estimate"], 0)


if __name__ == "__main__":
    unittest.main()
