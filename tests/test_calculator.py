import unittest

from core.calculator import Calculator
from models import MarketSettings


class CalculatorTests(unittest.TestCase):
    def test_no_order_when_target_liquidity_is_not_reached(self):
        orderbook = {
            "bids": [[0.14, 100], [0.139, 50]],
            "asks": [
                [0.169, 2117.516],
                [0.170, 246],
                [0.171, 4361],
                [0.999, 5000],
            ],
        }
        settings = MarketSettings(
            market_id="market-1",
            enabled=True,
            target_liquidity=25000.0,
            max_auto_spread=6.0,
        )

        calc = Calculator.calculate(orderbook, settings, 3)

        self.assertIsNotNone(calc)
        self.assertFalse(calc.can_place_no)
        self.assertGreater(calc.buy_no_price, 0.0)


if __name__ == "__main__":
    unittest.main()
