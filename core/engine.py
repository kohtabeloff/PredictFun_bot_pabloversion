"""
BotEngine: главный координатор.
Управляет воркерами, WebSocket, инспектором, execution guard.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from api.client import APIClient
from api.websocket import PredictWebSocket
from core.calculator import Calculator
from core.market_worker import MarketWorker
from core.order_manager import OrderManager
from models import AccountInfo, BotState, MarketSettings, MarketState
from storage.settings_store import SettingsStore
from utils.logger import BotLogger, EventBus


class BotEngine:
    def __init__(
        self,
        account: AccountInfo,
        settings_store: SettingsStore,
        event_bus: EventBus,
        logger: BotLogger,
    ):
        self.account = account
        self.settings_store = settings_store
        self.event_bus = event_bus
        self.logger = logger

        self.api: APIClient | None = None
        self.ws: PredictWebSocket | None = None
        self.order_manager: OrderManager | None = None

        self._workers: dict[str, MarketWorker] = {}
        self._market_info_cache: dict[str, dict] = {}
        self._market_states: dict[str, MarketState] = {}

        self._inspector_task: asyncio.Task | None = None
        self._execution_guard_task: asyncio.Task | None = None
        self._balance_task: asyncio.Task | None = None
        self._bootstrap_task: asyncio.Task | None = None

        self.running = False
        self._state = "stopped"  # stopped | starting | running | stopping
        self._state_lock = asyncio.Lock()
        self.balance: float | None = None
        self.ws_connected = False
        self._guard_failures: dict[str, int] = {}  # order_id -> consecutive fail count
        self._global_defaults: dict = {}  # настройки по умолчанию для новых маркетов

    # ─────────────────────────────────────────────────────────────────────────
    # Старт / стоп
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self):
        async with self._state_lock:
            if self._state != "stopped":
                return
            self._state = "starting"
            self._broadcast_state()

        try:
            self.logger.log("Запуск бота...")

            # API клиент
            from api.auth import get_auth_jwt
            jwt = await get_auth_jwt(
                self.account.api_key,
                self.account.predict_account_address,
                self.account.privy_wallet_private_key,
                proxy=self.account.proxy,
                log_func=self.logger,
            )

            self.api = APIClient(
                api_key=self.account.api_key,
                jwt_token=jwt,
                predict_account_address=self.account.predict_account_address,
                privy_wallet_private_key=self.account.privy_wallet_private_key,
                proxy=self.account.proxy,
                log_func=self.logger,
            )
            await self.api.start()

            # Order manager
            self.order_manager = OrderManager(
                api_client=self.api,
                market_info_cache=self._market_info_cache,
                log_func=self.logger,
            )

            # WebSocket
            self.ws = PredictWebSocket(
                api_key=self.account.api_key,
                log_func=self.logger,
                proxy=self.account.proxy or None,
            )
            self.ws.start()

            # Фоновые задачи
            self._inspector_task = asyncio.create_task(self._inspector_loop())
            self._execution_guard_task = asyncio.create_task(self._execution_guard_loop())
            self._balance_task = asyncio.create_task(self._balance_loop())
            self._bootstrap_task = asyncio.create_task(self._bootstrap_orderbooks_loop())

            self.running = True
            self._state = "running"
            self.logger.log("✓ Бот запущен")
            self._broadcast_state()
            await self._send_telegram("✅ PredictFun Bot запущен")
        except Exception as e:
            self.logger.log(f"✗ Ошибка запуска: {e}")
            # Cleanup частично поднятых ресурсов
            for task in [self._inspector_task, self._execution_guard_task, self._balance_task, self._bootstrap_task]:
                if task and not task.done():
                    task.cancel()
            self._inspector_task = self._execution_guard_task = self._balance_task = self._bootstrap_task = None
            if self.ws:
                self.ws.stop()
                self.ws = None
            if self.api:
                await self.api.close()
                self.api = None
            self.order_manager = None
            self._state = "stopped"
            self.running = False
            self._broadcast_state()
            raise

    async def stop(self):
        async with self._state_lock:
            if self._state != "running":
                return
            self._state = "stopping"

        self.logger.log("Остановка бота...")
        self.running = False

        # Отменяем ордера перед остановкой — без надзора они опасны
        for worker in list(self._workers.values()):
            ids = worker.get_active_order_ids()
            if ids and self.order_manager:
                ok = await self.order_manager.cancel_orders(ids, market_id=worker.market_id)
                if not ok:
                    # Одна повторная попытка через секунду
                    await asyncio.sleep(1)
                    ok = await self.order_manager.cancel_orders(ids, market_id=worker.market_id)
                if ok:
                    worker.order_yes = None
                    worker.order_no = None
                else:
                    self.logger.log(
                        f"[{worker.market_id}] ⚠ ВНИМАНИЕ: ордера не удалось отменить при остановке — "
                        f"закрой вручную на бирже! IDs: {ids}"
                    )
                    await self._send_telegram(
                        f"⚠ Бот остановлен, но ордера маркета {worker.market_id} "
                        f"не удалось отменить — закрой вручную!"
                    )

        # Останавливаем воркеры
        for worker in list(self._workers.values()):
            await worker.stop()
        self._workers.clear()

        # Отменяем фоновые задачи
        for task in [self._inspector_task, self._execution_guard_task, self._balance_task, self._bootstrap_task]:
            if task and not task.done():
                task.cancel()

        if self.ws:
            self.ws.stop()

        if self.api:
            await self.api.close()

        self._state = "stopped"
        self.logger.log("Бот остановлен")
        self._broadcast_state()

    # ─────────────────────────────────────────────────────────────────────────
    # Управление маркетами
    # ─────────────────────────────────────────────────────────────────────────

    async def add_markets(self, market_ids: list[str], force_disabled: bool = False) -> dict[str, str]:
        """
        Загружает маркеты по ID и запускает воркеры.
        Возвращает {market_id: "ok" | "error: ..."}
        force_disabled=True — воркер стартует с enabled=False (не сохраняется в settings.json).
        """
        results = {}
        for mid in market_ids:
            mid = str(mid).strip()
            if mid in self._workers:
                results[mid] = "already_exists"
                continue
            try:
                info = await self._load_market_info(mid)
                if info is None:
                    results[mid] = "error: не удалось загрузить маркет"
                    continue
                if info.get("status") != "REGISTERED":
                    results[mid] = f"error: статус {info.get('status')} (нужен REGISTERED)"
                    continue
                self._market_info_cache[mid] = info
                await self._start_worker(mid)
                if force_disabled:
                    worker = self._workers.get(mid)
                    if worker:
                        worker.settings = worker.settings.model_copy(update={"enabled": False})
                results[mid] = "ok"
            except Exception as e:
                results[mid] = f"error: {e}"
        return results

    async def remove_market(self, market_id: str) -> bool:
        """Останавливает воркер и отменяет все ордера маркета.
        Возвращает False если ордера не удалось отменить — маркет остаётся под управлением."""
        worker = self._workers.get(market_id)
        if worker:
            ids = worker.get_active_order_ids()
            if ids and self.order_manager:
                ok = await self.order_manager.cancel_orders(ids, market_id=market_id)
                if not ok:
                    self.logger.log(
                        f"[{market_id}] ✗ Не удалось отменить ордера — маркет остаётся под управлением"
                    )
                    self._broadcast_state()
                    return False
            self._workers.pop(market_id, None)
            await worker.stop()
            if self.ws:
                self.ws.unsubscribe(market_id)
        self._market_states.pop(market_id, None)
        self.settings_store.remove(market_id)
        self._broadcast_state()
        return True

    async def cancel_all(self):
        """Отменяет все ордера и приостанавливает стратегии в памяти.
        enabled не пишется в settings.json — при перезапуске всё восстановится."""
        for worker in self._workers.values():
            worker.settings = worker.settings.model_copy(update={"enabled": False})
            ids = worker.get_active_order_ids()
            if ids and self.order_manager:
                ok = await self.order_manager.cancel_orders(ids, market_id=worker.market_id)
                if ok:
                    worker.order_yes = None
                    worker.order_no = None
                else:
                    self.logger.log(f"[{worker.market_id}] ✗ Не удалось отменить ордера — состояние сохранено")
            else:
                worker.order_yes = None
                worker.order_no = None
        self.logger.log("✓ Все ордера отменены, стратегии приостановлены")
        self._broadcast_state()

    def set_global_defaults(self, **kwargs):
        """Сохраняет настройки как дефолтные для новых маркетов (применяются при добавлении)."""
        self._global_defaults.update(kwargs)

    def update_market_settings(self, market_id: str, **kwargs) -> MarketSettings:
        settings = self.settings_store.update(market_id, **kwargs)
        worker = self._workers.get(market_id)
        if worker:
            prev_enabled = worker.settings.enabled
            # Если enabled не передан явно — сохраняем текущий статус воркера в памяти.
            # Это нужно чтобы пауза после cancel_all не сбрасывалась при изменении других настроек.
            if "enabled" not in kwargs:
                settings = settings.model_copy(update={"enabled": worker.settings.enabled})
            worker.update_settings(settings)
            should_reprocess = settings.enabled and (
                "enabled" in kwargs or
                any(k != "enabled" for k in kwargs) or
                (not prev_enabled and settings.enabled)
            )
            if should_reprocess:
                worker.schedule_reprocess()
        return settings

    def get_state(self) -> BotState:
        markets = {}
        for mid, worker in self._workers.items():
            info = self._market_info_cache.get(mid, {})
            state = MarketState(
                market_id=mid,
                title=info.get("question") or info.get("title", mid),
                status=info.get("status", ""),
                image_url=info.get("imageUrl", ""),
                settings=worker.settings,
                order_yes=worker.order_yes,
                order_no=worker.order_no,
                last_calculation=worker.last_calc,
                diagnostic=worker.diagnostic,
                ws_connected=self.ws.connected if self.ws else False,
                last_update=worker.last_update,
            )
            markets[mid] = state

        total_orders = sum(
            (1 if w.order_yes else 0) + (1 if w.order_no else 0)
            for w in self._workers.values()
        )
        return BotState(
            running=self.running,
            ws_connected=self.ws.connected if self.ws else False,
            account_address=self.account.predict_account_address,
            balance_usdt=self.balance,
            markets=markets,
            total_open_orders=total_orders,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Приватные методы
    # ─────────────────────────────────────────────────────────────────────────

    async def _load_market_info(self, market_id: str) -> dict | None:
        if not self.api:
            return None
        return await self.api.get_market(market_id)

    async def _start_worker(self, market_id: str):
        is_new = not self.settings_store.has(market_id)
        settings = self.settings_store.get(market_id)
        # Для новых маркетов применяем глобальные дефолты (заданные через "Общие настройки")
        if is_new and self._global_defaults:
            settings = self.settings_store.update(market_id, **self._global_defaults)
        info = self._market_info_cache[market_id]

        worker = MarketWorker(
            market_id=market_id,
            market_info=info,
            settings=settings,
            order_manager=self.order_manager,
            on_state_update=self._on_market_state,
            log_func=self.logger,
        )
        self._workers[market_id] = worker

        if self.ws:
            self.ws.subscribe(market_id, worker.queue)

        worker.start()
        self.logger.log(f"[{market_id}] Запущен: {(info.get('question') or info.get('title', market_id))[:50]}")

    def _on_market_state(self, state: MarketState):
        """Вызывается воркером при каждом обновлении."""
        self._market_states[state.market_id] = state
        self.event_bus.emit({
            "type": "market_update",
            "market_id": state.market_id,
            "data": state.model_dump(),
        })

    def _broadcast_state(self):
        self.event_bus.emit({
            "type": "bot_state",
            "data": self.get_state().model_dump(),
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Фоновые задачи
    # ─────────────────────────────────────────────────────────────────────────

    async def _inspector_loop(self):
        """Каждые 10 сек: ищет orphan ордера и отменяет их."""
        from config import INSPECTOR_INTERVAL_SEC
        while self.running:
            await asyncio.sleep(INSPECTOR_INTERVAL_SEC)
            try:
                if not self.api:
                    continue
                open_orders = await self.api.get_open_orders()
                if open_orders is None:
                    continue

                # Собираем известные ID
                known_ids: set[str] = set()
                managed_markets: set[str] = set(self._workers.keys())
                for worker in self._workers.values():
                    for oid in worker.get_active_order_ids():
                        known_ids.add(oid)

                # Ищем orphans
                orphans = []
                for o in open_orders:
                    mid = str(o.get("marketId", ""))
                    if mid not in managed_markets:
                        continue
                    oid = str(o.get("id") or o.get("orderId") or "")
                    if oid and oid not in known_ids:
                        orphans.append(oid)

                if orphans:
                    self.logger.log(f"[Inspector] Orphan ордеров: {len(orphans)}, отменяем")
                    await self.order_manager.cancel_orders(orphans)

                # Обновляем счётчик для UI
                total = len(open_orders)
                self.event_bus.emit({"type": "orders_count", "count": total})

            except Exception as e:
                self.logger.log(f"[Inspector] ✗ {e}")

    async def _execution_guard_loop(self):
        """Каждые 3 сек: проверяет не исполнились ли наши ордера."""
        from config import EXECUTION_GUARD_INTERVAL_SEC
        while self.running:
            await asyncio.sleep(EXECUTION_GUARD_INTERVAL_SEC)
            try:
                if not self.api:
                    continue
                open_orders = await self.api.get_open_orders()
                if open_orders is None:
                    # API недоступен — не трогаем состояние ордеров
                    continue
                open_ids = {str(o.get("id") or o.get("orderId")) for o in open_orders}

                for worker in list(self._workers.values()):
                    for side in ("yes", "no"):
                        order = worker.order_yes if side == "yes" else worker.order_no
                        if order is None:
                            continue
                        # Ордер исчез из open — проверим статус
                        if order.order_id not in open_ids:
                            # Grace period: ордер только что выставлен — API ещё не обновился
                            if time.time() - order.placed_at < 15:
                                continue
                            # Backoff: если API уже много раз не отвечал — замедляемся
                            fail_count = self._guard_failures.get(order.order_id, 0)
                            if fail_count > 0 and fail_count % 10 != 0:
                                continue  # проверяем каждый 10-й цикл вместо каждого

                            _TERMINAL_STATUSES = {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}

                            detail = await self.api.get_order(order.order_id)
                            if detail and detail.get("status") == "FILLED":
                                self._guard_failures.pop(order.order_id, None)
                                self.logger.log(
                                    f"⚠ [{worker.market_id}] {side.upper()} ИСПОЛНИЛАСЬ! "
                                    f"Цена {order.price*100:.1f}¢ × {order.shares:.1f} шт"
                                )
                                # Сбрасываем запись об ордере
                                if side == "yes":
                                    worker.order_yes = None
                                else:
                                    worker.order_no = None

                                # Авто-продажа — передаём последнюю известную цену
                                mid_price = None
                                if worker.last_calc:
                                    mid_price = (
                                        worker.last_calc.mid_price_yes
                                        if side == "yes"
                                        else worker.last_calc.mid_price_no
                                    )
                                sell_ok = await self.order_manager.sell_market(
                                    worker.market_id, side, order.shares, mid_price=mid_price
                                )
                                if not sell_ok:
                                    self.logger.log(
                                        f"[{worker.market_id}] ✗ Авто-продажа {side.upper()} НЕ УДАЛАСЬ "
                                        f"— позиция требует ручного закрытия!"
                                    )

                                # Telegram уведомление
                                sell_status = "✅ Продажа выполнена" if sell_ok else "❌ Продажа НЕ удалась — закрой вручную!"
                                await self._send_telegram(
                                    f"⚠ Лимитка исполнилась!\n"
                                    f"Маркет: {worker.market_info.get('title', worker.market_id)}\n"
                                    f"Сторона: {side.upper()}\n"
                                    f"Цена: {order.price*100:.1f}¢ × {order.shares:.1f} шт\n"
                                    f"Сумма: ${order.price * order.shares:.2f}\n"
                                    f"{sell_status}"
                                )

                                self.event_bus.emit({
                                    "type": "execution_alert",
                                    "market_id": worker.market_id,
                                    "side": side,
                                    "price": order.price,
                                    "shares": order.shares,
                                })
                            elif detail is not None and detail.get("status") in _TERMINAL_STATUSES:
                                # Любой терминальный статус (CANCELLED, EXPIRED, REJECTED и др.)
                                # — ордера уже нет на бирже, сбрасываем из памяти
                                status = detail.get("status")
                                self._guard_failures.pop(order.order_id, None)
                                self.logger.log(
                                    f"[{worker.market_id}] {side.upper()} ордер {order.order_id} "
                                    f"завершён со статусом {status} — сбрасываем"
                                )
                                if side == "yes":
                                    worker.order_yes = None
                                else:
                                    worker.order_no = None
                            elif detail is None:
                                # Ошибка API — НЕ сбрасываем, проверим в след. цикле
                                self._guard_failures[order.order_id] = fail_count + 1
                                new_count = self._guard_failures[order.order_id]
                                if new_count == 1:
                                    self.logger.log(
                                        f"[ExecutionGuard] [{worker.market_id}] {side.upper()} "
                                        f"не удалось проверить статус ордера, пропускаем"
                                    )
                                elif new_count == 10:
                                    self.logger.log(
                                        f"[ExecutionGuard] [{worker.market_id}] {side.upper()} "
                                        f"ордер {order.order_id} не верифицируется уже {new_count} раз — "
                                        f"переключаюсь на проверку раз в 10 циклов"
                                    )
                                elif new_count % 50 == 0:
                                    self.logger.log(
                                        f"[ExecutionGuard] [{worker.market_id}] {side.upper()} "
                                        f"ордер {order.order_id} не верифицируется ({new_count} попыток)"
                                    )

            except Exception as e:
                self.logger.log(f"[ExecutionGuard] ✗ {e}")

    async def _balance_loop(self):
        """Каждые 5 мин обновляет баланс."""
        while self.running:
            try:
                if self.api:
                    balance = await self.api.get_balance()
                    if balance is not None:
                        self.balance = balance
                        self.event_bus.emit({"type": "balance", "balance": balance})
            except Exception:
                pass
            await asyncio.sleep(300)

    async def _bootstrap_orderbooks_loop(self):
        """Подтягивает стартовые snapshots для маркетов, по которым WS ещё не прислал стакан."""
        await asyncio.sleep(5)
        while self.running:
            try:
                if not self.ws or not self.ws.connected:
                    await asyncio.sleep(5)
                    continue

                missing = [
                    worker for worker in self._workers.values()
                    if worker.settings.enabled and worker.last_orderbook is None
                ]
                if not missing:
                    await asyncio.sleep(30)
                    continue

                self.logger.log(f"[Bootstrap] Нет стакана для {len(missing)} маркетов, запрашиваю snapshot")
                await self.ws.subscribe_many([worker.market_id for worker in missing], batch_size=20, pause_sec=0.25)
                await asyncio.sleep(2)

                still_missing = [worker for worker in missing if worker.last_orderbook is None]
                if not still_missing:
                    await asyncio.sleep(30)
                    continue

                sem = asyncio.Semaphore(8)

                async def _bootstrap_one(worker: MarketWorker):
                    async with sem:
                        snapshot = await self.ws.fetch_snapshot(worker.market_id, timeout=8.0)
                        if snapshot:
                            try:
                                worker.queue.put_nowait(snapshot)
                            except asyncio.QueueFull:
                                try:
                                    worker.queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                                try:
                                    worker.queue.put_nowait(snapshot)
                                except asyncio.QueueFull:
                                    pass

                await asyncio.gather(*[_bootstrap_one(worker) for worker in still_missing], return_exceptions=True)
            except Exception as e:
                self.logger.log(f"[Bootstrap] ✗ {e}")

            await asyncio.sleep(30)

    async def _send_telegram(self, message: str):
        from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
        import aiohttp
        token, chat_id = TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
        if not token or not chat_id:
            return
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception:
            pass
