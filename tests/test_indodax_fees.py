import unittest

from crypto_bot.bot import _pair_minimums
from crypto_bot.exchange import ExchangeError
from crypto_bot.settings import SETTINGS


class IndodaxFeeTests(unittest.TestCase):
    def test_all_in_taker_fee_is_selected_per_side(self):
        pair = {
            "trade_fee_percent_taker": "0.2",
            "trade_min_base_currency": "10000",
            "trade_min_traded_currency": "0.00001",
            "quantity_increment": "0.00000001",
        }

        minimum, minimum_quantity, step, buy_fee, sell_fee = _pair_minimums(pair)

        self.assertEqual(minimum, 25_000)
        self.assertEqual(minimum_quantity, 0.00001)
        self.assertEqual(step, 0.00000001)
        self.assertEqual(buy_fee, SETTINGS.all_in_taker_buy_fee_rate)
        self.assertEqual(sell_fee, SETTINGS.all_in_taker_sell_fee_rate)
        self.assertGreater(sell_fee, buy_fee)

    def test_invalid_quantity_increment_is_rejected(self):
        with self.assertRaisesRegex(ExchangeError, "increment"):
            _pair_minimums(
                {
                    "trade_fee_percent_taker": "0.2",
                    "trade_min_base_currency": "10000",
                    "quantity_increment": "0",
                }
            )


if __name__ == "__main__":
    unittest.main()
