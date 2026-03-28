"""
MarketWorker: один на каждый маркет, своя asyncio.Task.
Получает orderbook-обновления из очереди, считает и выставляет ордера.
100 маркетов = 100 независимых воркеров.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from core.calculator import Calculator
from core.order_manager import OrderManager
from models import MarketSettings, OrderRecord, OrderCalculation, MarketState


REPOSITION_THRESHOLD = 0.001  # минимальный сдвиг цены для переставления


class MarketWorker:
    def __init__(
        self,
        market_id: str,
        market_info: dict,
        settings: MarketSettings,
        order_manager: OrderManager,
        on_state_update: Callable[[MarketState], None] | None = None,
        log_func: Callable = print,
    ):
        self.market_id = market_id
        self.market_info = market_info
        self.settings = settings
        self.order_manager = order_manager
        self.on_state_update = on_state_update
        self.log_func = log_func

        self.queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._task: asyncio.Task | None = None
        self._running = False

        self.order_yes: OrderRecord | None = None
        self.order_no: OrderRecord | None = None
        self.last_calc: OrderCalculation | None = None
        self.last_update = 0.0

        # Счётчик переставлений (для защиты от волатильности)
        self._reposition_times: list[float] = []
        self._volatile_until: float = 0.0  # cooldown — держать safe цену до этого времени
        # ID ордеров в процессе замены — инспектор не должен их трогать
        self._pending_cancel_ids: set[str] = set()

    def get_active_order_ids(self) -> list[str]:
        ids = []
        if self.order_yes and self.order_yes.order_id:
            ids.append(self.order_yes.order_id)
        if self.order_no and self.order_no.order_id:
            ids.append(self.order_no.order_id)
        # Добавляем ID в процессе замены — инспектор их не должен трогать
        ids.extend(self._pending_cancel_ids)
        return ids

    def update_settings(self, settings: MarketSettings):
        self.settings = settings

    def mark_order_cancelled(self, order_id: str):
        """Вызывается извне (inspector/execution_guard) когда ордер исчез."""
        if self.order_yes and self.order_yes.order_id == order_id:
            self.order_yes = None
        if self.order_no and self.order_no.order_id == order_id:
            self.order_no = None

    def _should_reposition(self, side: str, new_price: float) -> bool:
        """Нужно ли переставить ордер?"""
        order = self.order_yes if side == "yes" else self.order_no
        if order is None:
            return False
        return abs(order.price - new_price) > REPOSITION_THRESHOLD

    def _liquidity_dropped(self, side: str, orderbook: dict) -> bool:
        """Упала ли ликвидность перед нашим ордером ниже target_liquidity?"""
        order = self.order_yes if side == "yes" else self.order_no
        if order is None:
            return False
        target = self.settings.target_liquidity or 1000.0
        dp = self.market_info.get("decimalPrecision", 3)
        tick = 1 / (10 ** dp)
        current_liq = Calculator.cumulative_depth(orderbook, side, order.price + tick)
        return current_liq < target

    def _is_volatile(self) -> bool:
        """Слишком много переставлений за короткое время?"""
        limit = self.settings.volatile_reposition_limit
        if not limit:
            return False
        window = self.settings.volatile_window_seconds
        cooldown = self.settings.volatile_cooldown_seconds
        now = time.time()

        # Ещё в cooldown периоде — продолжаем держать safe цену
        if now < self._volatile_until:
            return True

        # Чистим старые записи вне окна
        self._reposition_times = [t for t in self._reposition_times if now - t < window]

        if len(self._reposition_times) >= limit:
            # Порог превышен — запускаем cooldown
            self._volatile_until = now + cooldown
            return True

        return False

    def _record_reposition(self):
        self._reposition_times.append(time.time())

    def _get_safe_price(self, calc: OrderCalculation, side: str) -> float:
        """При волатильности возвращает цену на максимальном расстоянии от mid."""
        max_spread = (self.settings.max_auto_spread or 6.0) / 100.0
        mid = calc.mid_price_yes if side == "yes" else calc.mid_price_no
        dp = self.market_info.get("decimalPrecision", 3)
        tick = 1 / (10 ** dp)
        safe = round(mid - max_spread, dp)
        from config import MIN_ORDER_PRICE, MAX_ORDER_PRICE
        return max(MIN_ORDER_PRICE, min(safe, MAX_ORDER_PRICE - tick))

    async def _process(self, orderbook: dict):
        """Обрабатывает одно обновление стакана."""
        if not self.settings.enabled:
            return

        decimal_precision = self.market_info.get("decimalPrecision", 3)
        calc = Calculator.calculate(orderbook, self.settings, decimal_precision)
        if calc is None:
            return

        self.last_calc = calc
        self.last_update = time.time()

        is_volatile = self._is_volatile()

        # --- YES ---
        want_yes = self.settings.side in ("both", "yes")
        if want_yes:
            if is_volatile:
                target_yes = self._get_safe_price(calc, "yes")
            else:
                target_yes = calc.buy_yes_price

            if self.order_yes is None and calc.can_place_yes:
                # Ставим новый ордер
                new_order = await self.order_manager.place_order(
                    self.market_id, "yes", target_yes, calc.buy_yes_shares
                )
                if new_order:
                    self.order_yes = new_order
                    self._record_reposition()
            elif self.order_yes is not None:
                if not calc.can_place_yes:
                    # Отменяем — условия не выполнены
                    ok = await self.order_manager.cancel_orders(
                        [self.order_yes.order_id], self.market_id
                    )
                    if ok:
                        self.order_yes = None
                elif self._should_reposition("yes", target_yes) or self._liquidity_dropped("yes", orderbook):
                    old_id = self.order_yes.order_id
                    self._pending_cancel_ids.add(old_id)
                    self.order_yes = None
                    try:
                        new_order = await self.order_manager.atomic_replace(
                            self.market_id, "yes", old_id, target_yes, calc.buy_yes_shares
                        )
                    finally:
                        self._pending_cancel_ids.discard(old_id)
                    self.order_yes = new_order
                    if new_order:
                        self._record_reposition()

        elif self.order_yes is not None:
            # Настройки изменились — отменяем YES
            ok = await self.order_manager.cancel_orders([self.order_yes.order_id], self.market_id)
            if ok:
                self.order_yes = None

        # --- NO ---
        want_no = self.settings.side in ("both", "no")
        if want_no:
            if is_volatile:
                target_no = self._get_safe_price(calc, "no")
            else:
                target_no = calc.buy_no_price

            if self.order_no is None and calc.can_place_no:
                new_order = await self.order_manager.place_order(
                    self.market_id, "no", target_no, calc.buy_no_shares
                )
                if new_order:
                    self.order_no = new_order
                    self._record_reposition()
            elif self.order_no is not None:
                if not calc.can_place_no:
                    ok = await self.order_manager.cancel_orders(
                        [self.order_no.order_id], self.market_id
                    )
                    if ok:
                        self.order_no = None
                elif self._should_reposition("no", target_no) or self._liquidity_dropped("no", orderbook):
                    old_id = self.order_no.order_id
                    self._pending_cancel_ids.add(old_id)
                    self.order_no = None
                    try:
                        new_order = await self.order_manager.atomic_replace(
                            self.market_id, "no", old_id, target_no, calc.buy_no_shares
                        )
                    finally:
                        self._pending_cancel_ids.discard(old_id)
                    self.order_no = new_order
                    if new_order:
                        self._record_reposition()

        elif self.order_no is not None:
            ok = await self.order_manager.cancel_orders([self.order_no.order_id], self.market_id)
            if ok:
                self.order_no = None

        # Уведомляем UI
        if self.on_state_update:
            state = MarketState(
                market_id=self.market_id,
                title=self.market_info.get("title", self.market_id),
                status=self.market_info.get("status", ""),
                image_url=self.market_info.get("imageUrl", ""),
                settings=self.settings,
                order_yes=self.order_yes,
                order_no=self.order_no,
                last_calculation=calc,
                ws_connected=True,
                last_update=self.last_update,
            )
            try:
                self.on_state_update(state)
            except Exception:
                pass

    async def run(self):
        """Главный цикл воркера — ждёт обновления из очереди."""
        self._running = True
        self.log_func(f"[{self.market_id}] Воркер запущен")
        try:
            while self._running:
                try:
                    orderbook = await asyncio.wait_for(self.queue.get(), timeout=60)
                    await self._process(orderbook)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    self.log_func(f"[{self.market_id}] ✗ Ошибка воркера: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            self.log_func(f"[{self.market_id}] Воркер остановлен")

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
