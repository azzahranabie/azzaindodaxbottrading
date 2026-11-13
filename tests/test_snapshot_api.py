import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from crypto_bot.exchange import Book
from crypto_bot.storage import LocalStore


class FakeIndodaxClient:
    def pairs(self):
        return {
            symbol: {
                "trade_fee_percent_taker": "0.2",
                "trade_min_base_currency": "10000",
                "trade_min_traded_currency": "0.00001",
                "quantity_increment": "0.00000001",
            }
            for symbol in ("BTCIDR", "ETHIDR", "XRPIDR")
        }

    def depth(self, symbol):
        return Book(
            bids=((Decimal("100"), Decimal("1000000")),),
            asks=((Decimal("101"), Decimal("1000000")),),
        )


class SnapshotEndpointTests(unittest.TestCase):
    def test_snapshot_requires_secret_and_records_raw_books(self):
        with tempfile.TemporaryDirectory() as directory:
            main.app.state.store = LocalStore(os.path.join(directory, "state.json"))
            with patch.dict(os.environ, {"CRON_SECRET": "test-secret"}):
                with patch("main.IndodaxClient", FakeIndodaxClient):
                    with TestClient(main.app) as client:
                        unauthorized = client.post("/api/snapshot", json={})
                        self.assertEqual(unauthorized.status_code, 401)

                        response = client.post(
                            "/api/snapshot",
                            headers={"Authorization": "Bearer test-secret"},
                            json={"order_size_idr": 10_000_000},
                        )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "complete")
            self.assertEqual(len(response.json()["snapshots"]), 3)
            self.assertTrue(response.json()["snapshots"][0]["buy_fill_complete"])
            self.assertGreater(
                response.json()["snapshots"][0]["sell_fee_rate"],
                response.json()["snapshots"][0]["buy_fee_rate"],
            )
            self.assertEqual(len(main.app.state.store.read_snapshots()), 3)

    def test_snapshot_rejects_unreasonable_order_sizes(self):
        with patch.dict(os.environ, {"CRON_SECRET": "test-secret"}):
            with TestClient(main.app) as client:
                response = client.post(
                    "/api/snapshot",
                    headers={"Authorization": "Bearer test-secret"},
                    json={"order_size_idr": 1},
                )
        self.assertEqual(response.status_code, 400)

    def test_dashboard_returns_signal_log_without_forward_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStore(os.path.join(directory, "state.json"))
            signal_event = {
                "type": "call",
                "status": "processed",
                "symbol": "BTCIDR",
                "signal": "hold",
                "candle_time": 1_800_000_000,
                "signal_price": 100_000_000,
                "entry_level": 101_000_000,
                "exit_level": 99_000_000,
                "sma": 100_500_000,
            }
            store.append_event(signal_event)
            main.app.state.store = store

            with patch.dict(os.environ, {"FORWARD_START_AT": ""}):
                with TestClient(main.app) as client:
                    response = client.get("/api/dashboard")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertIsNone(payload["forward_start_at"])
            self.assertEqual(payload["timeframe_seconds"], 4 * 60 * 60)
            self.assertEqual(payload["events"], [signal_event])


if __name__ == "__main__":
    unittest.main()
