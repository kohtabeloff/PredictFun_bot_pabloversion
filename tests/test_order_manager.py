import unittest

from core.order_manager import OrderManager


class _FakeAPI:
    async def cancel_orders(self, order_ids):
        return False


class OrderManagerTests(unittest.IsolatedAsyncioTestCase):
    @unittest.expectedFailure
    async def test_atomic_replace_should_not_accept_new_order_when_cancel_failed(self):
        manager = OrderManager(api_client=_FakeAPI(), market_info_cache={}, log_func=lambda *_: None)

        async def fake_place_order(market_id, side, new_price, new_shares):
            return {
                "market_id": market_id,
                "side": side,
                "price": new_price,
                "shares": new_shares,
            }

        manager.place_order = fake_place_order

        result = await manager.atomic_replace("123", "yes", "old-order", 0.45, 10)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
