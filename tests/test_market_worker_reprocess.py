import unittest
import time

from core.market_worker import MarketWorker
from models import MarketSettings, OrderRecord


class _FakeOrderManager:
    def __init__(self):
        self.place_calls = []
        self.replace_result = None

    async def place_order(self, market_id, side, price, shares):
        self.place_calls.append((market_id, side, price, shares))
        return OrderRecord(
            order_id=f"{side}-1",
            market_id=market_id,
            side=side,
            price=price,
            shares=shares,
        )

    async def cancel_orders(self, order_ids, market_id=""):
        return True

    async def atomic_replace(self, market_id, side, old_order_id, new_price, new_shares):
        return self.replace_result


class MarketWorkerReprocessTests(unittest.IsolatedAsyncioTestCase):
    async def test_reprocesses_last_orderbook_after_enable(self):
        order_manager = _FakeOrderManager()
        settings = MarketSettings(
            market_id="m1",
            enabled=False,
            target_liquidity=10.0,
            position_size_usdt=10.0,
            min_spread=0.2,
            max_auto_spread=6.0,
        )
        worker = MarketWorker(
            market_id="m1",
            market_info={"decimalPrecision": 3, "title": "Market 1"},
            settings=settings,
            order_manager=order_manager,
            on_state_update=None,
            log_func=lambda *_args, **_kwargs: None,
        )

        orderbook = {
            "bids": [[0.45, 100]],
            "asks": [[0.55, 100]],
        }

        await worker._process(orderbook)
        self.assertEqual(order_manager.place_calls, [])
        self.assertEqual(worker.last_orderbook, orderbook)

        worker.update_settings(settings.model_copy(update={"enabled": True}))
        worker.schedule_reprocess()

        replayed = worker.queue.get_nowait()
        await worker._process(replayed)

        self.assertEqual(len(order_manager.place_calls), 2)
        self.assertIsNotNone(worker.order_yes)
        self.assertIsNotNone(worker.order_no)

    async def test_logs_reposition_failure_when_replace_returns_none(self):
        lines = []
        order_manager = _FakeOrderManager()
        settings = MarketSettings(
            market_id="m2",
            enabled=True,
            target_liquidity=10.0,
            position_size_usdt=10.0,
            min_spread=0.2,
            max_auto_spread=6.0,
        )
        worker = MarketWorker(
            market_id="m2",
            market_info={"decimalPrecision": 3, "title": "Market 2"},
            settings=settings,
            order_manager=order_manager,
            on_state_update=None,
            log_func=lines.append,
        )
        worker.order_yes = OrderRecord(
            order_id="yes-old",
            market_id="m2",
            side="yes",
            price=0.45,
            shares=10,
        )

        orderbook = {
            "bids": [[0.60, 100]],
            "asks": [[0.70, 100]],
        }

        await worker._process(orderbook)

        self.assertIsNone(worker.order_yes)
        self.assertTrue(any("перестановка не удалась" in line for line in lines))

    async def test_logs_stale_orderbook_with_active_orders(self):
        lines = []
        order_manager = _FakeOrderManager()
        settings = MarketSettings(market_id="m3", enabled=True)
        worker = MarketWorker(
            market_id="m3",
            market_info={"decimalPrecision": 3, "title": "Market 3"},
            settings=settings,
            order_manager=order_manager,
            on_state_update=None,
            log_func=lines.append,
        )
        worker.order_no = OrderRecord(
            order_id="no-old",
            market_id="m3",
            side="no",
            price=0.81,
            shares=12,
        )
        worker.last_update = time.time() - 31

        worker._maybe_log_stale_orderbook()

        self.assertTrue(any("Нет свежего стакана" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
