import unittest
from types import SimpleNamespace

from core.engine import BotEngine
from models import AccountInfo, OrderCalculation, OrderRecord


class _DummySettingsStore:
    def all(self):
        return {}


class _DummyEventBus:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _DummyLogger:
    def __init__(self):
        self.lines = []

    def log(self, message):
        self.lines.append(message)

    def __call__(self, message):
        self.log(message)


class _FilledAPI:
    async def get_open_orders(self):
        return []

    async def get_order(self, order_id):
        return {"status": "FILLED"}


class _UnknownStatusAPI:
    async def get_open_orders(self):
        return []

    async def get_order(self, order_id):
        return {"status": "EXPIRED"}


class _HashMissIdHitAPI:
    def __init__(self):
        self.calls = []

    async def get_open_orders(self):
        return []

    async def get_order(self, order_id):
        self.calls.append(order_id)
        if order_id == "hash-o3":
            return None
        if order_id == "o3":
            return {"status": "FILLED"}
        return None


class _FilledListHashAPI:
    async def get_open_orders(self):
        return []

    async def get_order(self, order_id):
        return None

    async def get_recent_filled_orders(self, limit=100):
        return [{"order": {"hash": "hash-o4"}, "status": "FILLED"}]


class _OrderManagerStub:
    def __init__(self):
        self.calls = []

    async def sell_market(self, market_id, side, shares, mid_price=None):
        self.calls.append((market_id, side, shares, mid_price))
        return True


class ExecutionGuardTests(unittest.IsolatedAsyncioTestCase):
    def _make_engine(self):
        return BotEngine(
            account=AccountInfo(
                api_key="k",
                predict_account_address="addr",
                privy_wallet_private_key="pk",
            ),
            settings_store=_DummySettingsStore(),
            event_bus=_DummyEventBus(),
            logger=_DummyLogger(),
        )

    async def _run_single_guard_iteration(self, engine):
        import core.engine as engine_module

        original_sleep = engine_module.asyncio.sleep

        async def fake_sleep(_seconds):
            engine.running = False
            return None

        engine_module.asyncio.sleep = fake_sleep
        try:
            engine.running = True
            await engine._execution_guard_loop()
        finally:
            engine_module.asyncio.sleep = original_sleep

    async def test_guard_clears_order_and_notifies_when_filled(self):
        telegram_msgs = []
        engine = self._make_engine()
        engine.api = _FilledAPI()
        engine.order_manager = _OrderManagerStub()

        async def _capture_telegram(msg):
            telegram_msgs.append(msg)

        engine._send_telegram = _capture_telegram

        worker = SimpleNamespace(
            market_id="m1",
            market_info={"title": "Market 1"},
            order_yes=OrderRecord(order_id="o1", market_id="m1", side="yes", price=0.45, shares=12),
            order_no=None,
            last_calc=OrderCalculation(mid_price_yes=0.5, mid_price_no=0.5),
        )
        engine._workers = {"m1": worker}

        await self._run_single_guard_iteration(engine)

        # Ордер должен быть сброшен
        self.assertIsNone(worker.order_yes)
        # Авто-продажи нет — пользователь закрывает вручную
        self.assertEqual(engine.order_manager.calls, [])
        # Telegram уведомление отправлено
        self.assertEqual(len(telegram_msgs), 1)

    async def test_guard_should_clear_order_for_other_terminal_statuses(self):
        engine = self._make_engine()
        engine.api = _UnknownStatusAPI()
        engine.order_manager = _OrderManagerStub()
        engine._send_telegram = lambda *_args, **_kwargs: _async_noop()

        worker = SimpleNamespace(
            market_id="m2",
            market_info={"title": "Market 2"},
            order_yes=OrderRecord(order_id="o2", market_id="m2", side="yes", price=0.33, shares=5),
            order_no=None,
            last_calc=OrderCalculation(mid_price_yes=0.34, mid_price_no=0.66),
        )
        engine._workers = {"m2": worker}

        await self._run_single_guard_iteration(engine)

        self.assertIsNone(worker.order_yes)

    async def test_guard_falls_back_to_order_id_when_hash_lookup_misses(self):
        telegram_msgs = []
        engine = self._make_engine()
        engine.api = _HashMissIdHitAPI()
        engine.order_manager = _OrderManagerStub()

        async def _capture_telegram(msg):
            telegram_msgs.append(msg)

        engine._send_telegram = _capture_telegram

        worker = SimpleNamespace(
            market_id="m3",
            market_info={"title": "Market 3"},
            order_yes=OrderRecord(
                order_id="o3",
                order_hash="hash-o3",
                market_id="m3",
                side="yes",
                price=0.51,
                shares=7,
            ),
            order_no=None,
            last_calc=OrderCalculation(mid_price_yes=0.52, mid_price_no=0.48),
        )
        engine._workers = {"m3": worker}

        await self._run_single_guard_iteration(engine)

        self.assertIsNone(worker.order_yes)
        self.assertEqual(engine.api.calls, ["hash-o3", "o3"])
        self.assertEqual(len(telegram_msgs), 1)

    async def test_guard_matches_filled_fallback_by_order_hash(self):
        telegram_msgs = []
        engine = self._make_engine()
        engine.api = _FilledListHashAPI()
        engine.order_manager = _OrderManagerStub()

        async def _capture_telegram(msg):
            telegram_msgs.append(msg)

        engine._send_telegram = _capture_telegram

        worker = SimpleNamespace(
            market_id="m4",
            market_info={"title": "Market 4"},
            order_yes=OrderRecord(
                order_id="o4",
                order_hash="hash-o4",
                market_id="m4",
                side="yes",
                price=0.22,
                shares=9,
            ),
            order_no=None,
            last_calc=OrderCalculation(mid_price_yes=0.24, mid_price_no=0.76),
        )
        engine._workers = {"m4": worker}

        await self._run_single_guard_iteration(engine)

        self.assertIsNone(worker.order_yes)
        self.assertEqual(len(telegram_msgs), 1)


async def _async_noop():
    return None


if __name__ == "__main__":
    unittest.main()
