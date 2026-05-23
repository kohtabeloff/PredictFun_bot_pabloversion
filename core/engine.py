"""
BotEngine: главный координатор.
Управляет воркерами, WebSocket, инспектором, execution guard.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable

from api.client import APIClient
from api.websocket import WebSocketPool
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
        self.ws: WebSocketPool | None = None
        self.order_manager: OrderManager | None = None

        self._workers: dict[str, MarketWorker] = {}
        self._market_info_cache: dict[str, dict] = {}
        self._market_states: dict[str, MarketState] = {}

        self._inspector_task: asyncio.Task | None = None
        self._execution_guard_task: asyncio.Task | None = None
        self._balance_task: asyncio.Task | None = None
        self._bootstrap_task: asyncio.Task | None = None
        self._points_filter_task: asyncio.Task | None = None
        self._auto_sell_monitor_task: asyncio.Task | None = None

        # Points filter: markets temporarily paused because they have no active reward
        self._points_blocked: set[str] = set()
        self._market_points_status: dict[str, bool | None] = {}  # None = not checked yet

        # Auto-sell: pending sell limit orders placed after fill detection
        # order_hash -> {market_id, side, fill_price, sell_price, shares, placed_at, title}
        self._auto_sell_pending: dict[str, dict] = {}

        self.running = False
        self._state = "stopped"  # stopped | starting | running | stopping
        self._state_lock = asyncio.Lock()
        self.balance: float | None = None
        self._inactivity_alert_sent: bool = False  # флаг: уведомление о зависании уже отправлено
        self._inactivity_alert_task: asyncio.Task | None = None
        self.ws_connected = False
        self._guard_failures: dict[str, int] = {}  # order_id -> consecutive fail count
        self._global_defaults: dict = {}  # настройки по умолчанию для новых маркетов
        self._bootstrap_trigger: asyncio.Event = asyncio.Event()
        self._bootstrap_fails: dict[str, int] = {}  # market_id -> кол-во неудачных попыток
        self._cancelling: bool = False  # флаг: cancel_all в процессе — новые маркеты добавляются disabled

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

            # WebSocket Pool
            from config import (
                WS_POOL_SIZE, WS_POOL_REBALANCE_INTERVAL_SEC,
                WS_POOL_SLOW_SLOTS_PER_REBALANCE, WS_POOL_DEDUPE_WINDOW_SEC,
                WS_POOL_CONNECT_STAGGER_MS,
            )
            self.ws = WebSocketPool(
                api_key=self.account.api_key,
                log_func=self.logger,
                proxy=self.account.proxy or None,
                pool_size=WS_POOL_SIZE,
                rebalance_interval_sec=WS_POOL_REBALANCE_INTERVAL_SEC,
                slow_slots_per_rebalance=WS_POOL_SLOW_SLOTS_PER_REBALANCE,
                dedupe_window_sec=WS_POOL_DEDUPE_WINDOW_SEC,
                connect_stagger_ms=WS_POOL_CONNECT_STAGGER_MS,
            )
            self.ws.start()

            # Фоновые задачи
            self._inspector_task = asyncio.create_task(self._inspector_loop())
            self._execution_guard_task = asyncio.create_task(self._execution_guard_loop())
            self._balance_task = asyncio.create_task(self._balance_loop())
            self._bootstrap_task = asyncio.create_task(self._bootstrap_orderbooks_loop())
            self._points_filter_task = asyncio.create_task(self._points_filter_loop())
            self._auto_sell_monitor_task = asyncio.create_task(self._auto_sell_monitor_loop())
            self._inactivity_alert_task = asyncio.create_task(self._inactivity_alert_loop())

            self.running = True
            self._inactivity_alert_sent = False
            self._state = "running"
            self.logger.log("✓ Бот запущен")
            self._broadcast_state()
            await self._send_telegram("✅ PredictFun Bot запущен")
        except Exception as e:
            self.logger.log(f"✗ Ошибка запуска: {e}")
            # Cleanup частично поднятых ресурсов
            for task in [self._inspector_task, self._execution_guard_task, self._balance_task,
                         self._bootstrap_task, self._points_filter_task,
                         self._auto_sell_monitor_task, self._inactivity_alert_task]:
                if task and not task.done():
                    task.cancel()
            self._inspector_task = self._execution_guard_task = self._balance_task = \
                self._bootstrap_task = self._points_filter_task = \
                self._auto_sell_monitor_task = self._inactivity_alert_task = None
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
        for task in [self._inspector_task, self._execution_guard_task, self._balance_task,
                     self._bootstrap_task, self._points_filter_task,
                     self._auto_sell_monitor_task, self._inactivity_alert_task]:
            if task and not task.done():
                task.cancel()

        if self.ws:
            self.ws.stop()

        if self.api:
            await self.api.close()

        # Сбрасываем состояние фильтра поинтов — при следующем старте
        # воркеры пересоздаются из settings.json, фильтр перепроверит всё заново
        self._points_blocked.clear()
        self._market_points_status.clear()

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
                self._bootstrap_fails.pop(mid, None)
                await self._start_worker(mid)
                # force_disabled явный ИЛИ cancel_all запущен в этот момент — выключаем воркер
                if force_disabled or self._cancelling:
                    worker = self._workers.get(mid)
                    if worker:
                        worker.settings = worker.settings.model_copy(update={"enabled": False})
                results[mid] = "ok"
            except Exception as e:
                results[mid] = f"error: {e}"
        if any(v == "ok" for v in results.values()):
            self._bootstrap_trigger.set()
        return results

    async def remove_market(self, market_id: str) -> bool:
        """Останавливает воркер и отменяет все ордера маркета.
        Возвращает False если ордера не удалось отменить — маркет остаётся под управлением."""
        worker = self._workers.get(market_id)
        if worker:
            ids = worker.get_active_order_ids()
            if ids and self.order_manager and self.order_manager.api.is_active:
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
        self._market_info_cache.pop(market_id, None)
        self.settings_store.remove(market_id)
        self._broadcast_state()
        return True

    async def cancel_all(self) -> list[str]:
        """Отменяет все ордера и приостанавливает стратегии в памяти.
        enabled не пишется в settings.json — при перезапуске всё восстановится.
        Возвращает список market_id у которых ордера не удалось снять."""
        self._cancelling = True  # блокируем запуск новых воркеров в enabled=True
        failed: list[str] = []
        for worker in list(self._workers.values()):
            worker.settings = worker.settings.model_copy(update={"enabled": False})
            ids = worker.get_active_order_ids()
            if ids and self.order_manager:
                ok = await self.order_manager.cancel_orders(ids, market_id=worker.market_id)
                if not ok:
                    await asyncio.sleep(1)
                    ok = await self.order_manager.cancel_orders(ids, market_id=worker.market_id)
                if ok:
                    worker.order_yes = None
                    worker.order_no = None
                else:
                    failed.append(worker.market_id)
                    self.logger.log(f"[{worker.market_id}] ✗ Не удалось отменить ордера — закрой вручную на бирже!")
            else:
                worker.order_yes = None
                worker.order_no = None
        if failed:
            self.logger.log(f"⚠ Стратегии приостановлены, но ордера не сняты в {len(failed)} маркетах: {failed[:10]}")
            await self._send_telegram(
                f"⚠ Cancel All: стратегии остановлены, но ордера не удалось снять в маркетах: {', '.join(failed[:10])}"
            )
        else:
            self.logger.log("✓ Все ордера отменены, стратегии приостановлены")
        self._cancelling = False  # снимаем блокировку — добавление новых маркетов снова в обычном режиме
        self._broadcast_state()
        return failed

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
                has_points=self._market_points_status.get(mid),
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
                            # Backoff: если API уже много раз не отвечал — замедляемся
                            fail_count = self._guard_failures.get(order.order_id, 0)
                            if fail_count > 0 and fail_count % 10 != 0:
                                continue  # проверяем каждый 10-й цикл вместо каждого

                            _TERMINAL_STATUSES = {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}

                            lookup_ids: list[str] = []
                            if order.order_hash:
                                lookup_ids.append(order.order_hash)
                            if order.order_id and order.order_id not in lookup_ids:
                                lookup_ids.append(order.order_id)

                            detail = None
                            for lookup_id in lookup_ids:
                                detail = await self.api.get_order(lookup_id)
                                if detail is not None:
                                    break
                            if detail and detail.get("status") == "FILLED":
                                self._guard_failures.pop(order.order_id, None)

                                # Сколько времени ордер жил
                                filled_after = time.time() - order.placed_at
                                life_str = f"{int(filled_after // 60)}м {int(filled_after % 60)}с"

                                # Состояние рынка на момент исполнения
                                market_ctx = ""
                                mid_price = None
                                if worker.last_calc:
                                    lc = worker.last_calc
                                    mid_price = lc.mid_price_yes if side == "yes" else lc.mid_price_no
                                    spread = lc.spread_yes if side == "yes" else lc.spread_no
                                    market_ctx = f", mid={mid_price*100:.1f}¢, спред={spread*100:.1f}%"
                                worker_last_update = getattr(worker, "last_update", 0.0)
                                if worker_last_update:
                                    ws_age = time.time() - worker_last_update
                                    market_ctx += f", последний стакан {ws_age:.1f}с назад"

                                self.logger.log(
                                    f"⚠ [{worker.market_id}] {side.upper()} ИСПОЛНИЛАСЬ! "
                                    f"Цена {order.price*100:.1f}¢ × {order.shares:.1f} шт "
                                    f"(жила {life_str}{market_ctx})"
                                )
                                # Сохраняем заполненный ордер до сброса
                                filled_order = order

                                # Сбрасываем запись об ордере
                                if side == "yes":
                                    worker.order_yes = None
                                else:
                                    worker.order_no = None

                                # Запускаем авто-продажу если включена
                                import config as cfg
                                auto_sell_active = cfg.AUTO_SELL_ENABLED
                                if auto_sell_active:
                                    asyncio.create_task(self._trigger_auto_sell(
                                        worker, side, filled_order
                                    ))

                                # Telegram уведомление
                                tg_msg = (
                                    f"⚠ Лимитка исполнилась!\n"
                                    f"Маркет: {worker.market_info.get('title', worker.market_id)}\n"
                                    f"Сторона: {side.upper()}\n"
                                    f"Цена: {order.price*100:.1f}¢ × {order.shares:.1f} шт\n"
                                    f"Сумма: ${order.price * order.shares:.2f}\n"
                                    f"Жила: {life_str}\n"
                                )
                                if market_ctx:
                                    tg_msg += f"Рынок: {market_ctx.lstrip(', ')}\n"
                                tg_msg += "🔄 Авто-продажа запускается..." if auto_sell_active else "❗ Закрой позицию вручную"
                                await self._send_telegram(tg_msg)

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
                                # Ошибка API — пробуем фолбек через список filled ордеров
                                if fail_count == 0:
                                    filled_list = await self.api.get_recent_filled_orders(limit=100)
                                    if filled_list is not None:
                                        filled_refs = set()
                                        for filled in filled_list:
                                            order_ref = filled.get("order") if isinstance(filled.get("order"), dict) else {}
                                            for candidate in (
                                                filled.get("id"),
                                                filled.get("orderId"),
                                                filled.get("hash"),
                                                order_ref.get("hash"),
                                            ):
                                                if candidate:
                                                    filled_refs.add(str(candidate))
                                        if order.order_id in filled_refs or (order.order_hash and order.order_hash in filled_refs):
                                            self._guard_failures.pop(order.order_id, None)
                                            filled_after = time.time() - order.placed_at
                                            life_str = f"{int(filled_after // 60)}м {int(filled_after % 60)}с"
                                            ws_ctx = ""
                                            worker_last_update = getattr(worker, "last_update", 0.0)
                                            if worker_last_update:
                                                ws_age = time.time() - worker_last_update
                                                ws_ctx = f", последний стакан {ws_age:.1f}с назад"
                                            self.logger.log(
                                                f"⚠ [{worker.market_id}] {side.upper()} ИСПОЛНИЛАСЬ! "
                                                f"(фолбек) Цена {order.price*100:.1f}¢ × {order.shares:.1f} шт "
                                                f"(жила {life_str}{ws_ctx})"
                                            )
                                            filled_order = order
                                            if side == "yes":
                                                worker.order_yes = None
                                            else:
                                                worker.order_no = None
                                            import config as cfg
                                            if cfg.AUTO_SELL_ENABLED:
                                                asyncio.create_task(self._trigger_auto_sell(
                                                    worker, side, filled_order
                                                ))
                                            tg_msg = (
                                                f"⚠ Лимитка исполнилась!\n"
                                                f"Маркет: {worker.market_info.get('title', worker.market_id)}\n"
                                                f"Сторона: {side.upper()}\n"
                                                f"Цена: {order.price*100:.1f}¢ × {order.shares:.1f} шт\n"
                                                f"Сумма: ${order.price * order.shares:.2f}\n"
                                                f"Жила: {life_str}\n"
                                            )
                                            tg_msg += "🔄 Авто-продажа запускается..." if cfg.AUTO_SELL_ENABLED else "❗ Закрой позицию вручную"
                                            await self._send_telegram(tg_msg)
                                            self.event_bus.emit({
                                                "type": "execution_alert",
                                                "market_id": worker.market_id,
                                                "side": side,
                                                "price": order.price,
                                                "shares": order.shares,
                                            })
                                            continue

                                # НЕ сбрасываем, проверим в след. цикле
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
        """Каждые 30 сек обновляет баланс."""
        while self.running:
            try:
                if self.api:
                    balance = await self.api.get_balance()
                    if balance is not None:
                        self.balance = balance
                        self.event_bus.emit({"type": "balance", "balance": balance})
            except Exception as e:
                self.logger.log(f"[Balance] ⚠ Не удалось обновить баланс: {e}", level="WARN")
            await asyncio.sleep(30)

    async def _bootstrap_orderbooks_loop(self):
        """Подтягивает стартовые snapshots для маркетов, по которым WS ещё не прислал стакан."""
        _BOOTSTRAP_MAX_FAILS = 15   # после 15 неудач (~15 мин) перестаём пробовать
        _BOOTSTRAP_BATCH = 50       # маркетов на одно WS-соединение
        _BOOTSTRAP_TIMEOUT = 12.0   # секунд ждём snapshots в одном batch

        await asyncio.sleep(5)
        while self.running:
            try:
                # Ждём триггера (новые маркеты добавлены) или таймаута 30 сек
                try:
                    await asyncio.wait_for(self._bootstrap_trigger.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
                self._bootstrap_trigger.clear()

                if not self.ws or not self.ws.connected:
                    await asyncio.sleep(5)
                    continue

                missing = [
                    worker for worker in self._workers.values()
                    if (worker.settings.enabled
                        and worker.last_orderbook is None
                        and self._bootstrap_fails.get(worker.market_id, 0) < _BOOTSTRAP_MAX_FAILS)
                ]
                if not missing:
                    continue

                self.logger.log(f"[Bootstrap] Нет стакана для {len(missing)} маркетов, запрашиваю snapshot")
                await self.ws.subscribe_many([w.market_id for w in missing], batch_size=20, pause_sec=0.25)
                await asyncio.sleep(2)

                still_missing = [w for w in missing if w.last_orderbook is None]
                if not still_missing:
                    continue

                # Batch-fetch: одно WS-соединение на _BOOTSTRAP_BATCH маркетов
                for i in range(0, len(still_missing), _BOOTSTRAP_BATCH):
                    batch = still_missing[i:i + _BOOTSTRAP_BATCH]
                    snapshots = await self.ws.fetch_snapshots_batch(
                        [w.market_id for w in batch], timeout=_BOOTSTRAP_TIMEOUT
                    )
                    for worker in batch:
                        ob = snapshots.get(worker.market_id)
                        if ob:
                            self._bootstrap_fails.pop(worker.market_id, None)
                            try:
                                worker.queue.put_nowait(ob)
                            except asyncio.QueueFull:
                                try:
                                    worker.queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                                try:
                                    worker.queue.put_nowait(ob)
                                except asyncio.QueueFull:
                                    pass
                        else:
                            self._bootstrap_fails[worker.market_id] = (
                                self._bootstrap_fails.get(worker.market_id, 0) + 1
                            )

                # Логируем маркеты, которые сдались
                gave_up = [
                    w.market_id for w in still_missing
                    if self._bootstrap_fails.get(w.market_id, 0) >= _BOOTSTRAP_MAX_FAILS
                ]
                if gave_up:
                    self.logger.log(
                        f"[Bootstrap] Нет активности в стакане, пропускаем {len(gave_up)} маркет(ов): "
                        + ", ".join(gave_up[:10]) + ("..." if len(gave_up) > 10 else "")
                    )

            except Exception as e:
                self.logger.log(f"[Bootstrap] ✗ {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Auto-sell
    # ─────────────────────────────────────────────────────────────────────────

    async def _trigger_auto_sell(self, worker, side: str, order):
        """
        Запускается как task после исполнения лимитки.
        Ждёт delay, затем выставляет sell limit по формуле:
        sell_price = fill_price * (1 - max_loss_pct / 100), округлено вверх до тика.
        """
        import config as cfg
        delay = cfg.AUTO_SELL_DELAY_SEC
        max_loss = cfg.AUTO_SELL_MAX_LOSS_PCT

        if delay > 0:
            await asyncio.sleep(delay)

        if not self.running or not self.order_manager:
            return

        title = worker.market_info.get("question") or worker.market_info.get("title", worker.market_id)
        result = await self.order_manager.place_sell_limit_auto(
            market_id=worker.market_id,
            side=side,
            shares=order.shares,
            fill_price=order.price,
            max_loss_pct=max_loss,
        )
        if result:
            order_hash, server_id = result
            market_info = self._market_info_cache.get(worker.market_id, {})
            dp = market_info.get("decimalPrecision", 3)
            import math
            tick = 1 / (10 ** dp)
            min_sell = order.price * (1 - max_loss / 100)
            sell_price = round(math.ceil(min_sell / tick) * tick, dp)

            self._auto_sell_pending[order_hash] = {
                "market_id": worker.market_id,
                "title": title,
                "side": side,
                "fill_price": order.price,
                "sell_price": sell_price,
                "shares": order.shares,
                "placed_at": time.time(),
                "server_id": server_id,
            }
            await self._send_telegram(
                f"📤 Авто-продажа выставлена!\n"
                f"Маркет: {title[:60]}\n"
                f"Продажа: {sell_price*100:.1f}¢ × {order.shares:.1f} шт\n"
                f"Куплено: {order.price*100:.1f}¢ | Макс.потери: {max_loss}%"
            )
        if not result:
            await self._send_telegram(
                f"⚠ Авто-продажа НЕ УДАЛАСЬ!\n"
                f"Маркет: {title[:60]}\n"
                f"Закрой позицию {side.upper()} вручную: {order.shares:.1f} шт"
            )

    async def _auto_sell_monitor_loop(self):
        """Каждые 5 сек: проверяет исполнились ли ордера авто-продажи."""
        while self.running:
            await asyncio.sleep(5)
            if not self._auto_sell_pending or not self.api:
                continue
            try:
                open_orders = await self.api.get_open_orders()
                if open_orders is None:
                    continue
                open_ids = {str(o.get("id") or o.get("orderId")) for o in open_orders}

                import config as cfg
                _AUTO_SELL_TTL = 86400  # 24 часа — после этого запись удаляется
                expiry_sec = cfg.AUTO_SELL_ORDER_EXPIRY_SEC
                for order_hash in list(self._auto_sell_pending.keys()):
                    entry = self._auto_sell_pending.get(order_hash, {})
                    age = time.time() - entry.get("placed_at", 0)
                    # TTL: удаляем записи старше 24 часов
                    if age > _AUTO_SELL_TTL:
                        self._auto_sell_pending.pop(order_hash, None)
                        self.logger.log(
                            f"[AutoSell] [{entry.get('market_id')}] ⚠ Ордер на продажу не найден "
                            f"за 24 часа — удаляем из мониторинга. Проверь позицию вручную!",
                            level="WARN"
                        )
                        continue
                    # server_id — числовой ID для сравнения с open_ids и отмены
                    sell_server_id = entry.get("server_id") or order_hash
                    # Срок жизни ордера: если задан и истёк — отменяем
                    if expiry_sec > 0 and age > expiry_sec and sell_server_id in open_ids:
                        cancel_ok = False
                        if self.order_manager:
                            cancel_ok = await self.order_manager.cancel_orders([sell_server_id], market_id=entry.get("market_id"))
                        if cancel_ok:
                            self._auto_sell_pending.pop(order_hash, None)
                            self.logger.log(
                                f"[AutoSell] [{entry.get('market_id')}] ⏱ Ордер на продажу не исполнен за "
                                f"{expiry_sec}с — отменён. Закрой позицию {entry.get('side','').upper()} вручную!",
                                level="WARN"
                            )
                            await self._send_telegram(
                                f"⏱ Ордер авто-продажи отменён по истечении {expiry_sec}с\n"
                                f"Маркет: {entry.get('title','')[:60]}\n"
                                f"Закрой позицию {entry.get('side','').upper()} вручную: {entry.get('shares',0):.1f} шт"
                            )
                        else:
                            self.logger.log(
                                f"[AutoSell] [{entry.get('market_id')}] ⏱ Срок истёк, но отменить ордер не удалось — продолжаем мониторить",
                                level="WARN"
                            )
                        continue
                    if sell_server_id in open_ids:
                        continue
                    detail = await self.api.get_order(order_hash)
                    if not detail:
                        continue

                    status = detail.get("status")
                    info = self._auto_sell_pending.get(order_hash, {})

                    if status == "FILLED":
                        self._auto_sell_pending.pop(order_hash, None)
                        self.logger.log(
                            f"[AutoSell] [{info.get('market_id')}] ✓ Позиция продана! "
                            f"{info.get('side','').upper()} по {info.get('sell_price',0)*100:.1f}¢"
                        )
                        await self._send_telegram(
                            f"✅ Авто-продажа исполнена!\n"
                            f"Маркет: {info.get('title','')[:60]}\n"
                            f"Продано: {info.get('sell_price',0)*100:.1f}¢ × {info.get('shares',0):.1f} шт"
                        )
                    elif status in {"CANCELLED", "EXPIRED", "REJECTED"}:
                        self._auto_sell_pending.pop(order_hash, None)
                        self.logger.log(
                            f"[AutoSell] [{info.get('market_id')}] Ордер на продажу {status} — "
                            f"закрой позицию {info.get('side','').upper()} вручную!"
                        )
                        await self._send_telegram(
                            f"⚠ Ордер авто-продажи {status}!\n"
                            f"Маркет: {info.get('title','')[:60]}\n"
                            f"Закрой позицию {info.get('side','').upper()} вручную: "
                            f"{info.get('shares',0):.1f} шт"
                        )
            except Exception as e:
                self.logger.log(f"[AutoSell Monitor] ✗ {e}")

    async def _inactivity_alert_loop(self):
        """Каждую минуту проверяет: если не было новых ордеров 10+ минут — шлёт Telegram-уведомление."""
        _INACTIVITY_THRESHOLD = 600  # 10 минут
        _CHECK_INTERVAL = 60

        # Ждём 10 минут с момента старта прежде чем начинать проверку
        await asyncio.sleep(_INACTIVITY_THRESHOLD)

        while self.running:
            await asyncio.sleep(_CHECK_INTERVAL)
            try:
                if not self.order_manager:
                    continue
                last_at = self.order_manager.last_order_at
                if last_at == 0.0:
                    continue
                inactive_sec = time.time() - last_at
                if inactive_sec >= _INACTIVITY_THRESHOLD and not self._inactivity_alert_sent:
                    self._inactivity_alert_sent = True
                    mins = int(inactive_sec // 60)
                    self.logger.log(
                        f"[Inactivity] ⚠ Нет новых ордеров уже {mins} мин — возможны проблемы с биржей",
                        level="WARN"
                    )
                    await self._send_telegram(
                        f"⚠ Бот не выставляет ордера уже {mins} мин\n"
                        f"Возможны проблемы с API или WebSocket PredictFun.\n"
                        f"Проверь состояние бота в менеджере."
                    )
                elif inactive_sec < _INACTIVITY_THRESHOLD and self._inactivity_alert_sent:
                    # Бот снова активен — сбрасываем флаг чтобы следующий сбой снова дал уведомление
                    self._inactivity_alert_sent = False
            except Exception as e:
                self.logger.log(f"[Inactivity] ✗ {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Points filter
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _has_active_points(market_info: dict) -> bool:
        """Возвращает True если у маркета есть активная почасовая награда в поинтах."""
        from datetime import datetime, timezone
        rewards = market_info.get("rewards") or {}
        current = rewards.get("current") or {}
        hourly_rate = current.get("hourlyRate") or 0
        if hourly_rate <= 0:
            return False
        starts_at = current.get("startsAt")
        ends_at = current.get("endsAt")
        if starts_at and ends_at:
            try:
                now = datetime.now(timezone.utc)
                start = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                end = datetime.fromisoformat(ends_at.replace("Z", "+00:00"))
                return start <= now < end
            except Exception:
                pass
        return hourly_rate > 0

    async def _points_filter_loop(self):
        """
        Проверяет поинты сразу после старта (через 5 сек), потом каждые POINTS_POLL_INTERVAL_SEC.
        Если POINTS_FILTER_ENABLED=False — пропускает проверку и спит.
        Если поинты пропали — отменяет ордера и приостанавливает маркет.
        Если поинты вернулись — возобновляет маркет (восстанавливает enabled из settings).
        """
        await asyncio.sleep(5)  # минимальная пауза чтобы маркеты успели загрузиться

        while self.running:
            try:
                import config as cfg
                if cfg.POINTS_FILTER_ENABLED:
                    await self._check_all_markets_points()
            except Exception as e:
                self.logger.log(f"[PointsFilter] ✗ {e}")

            import config as cfg
            interval = max(60, cfg.POINTS_POLL_INTERVAL_SEC)  # минимум 60 сек
            await asyncio.sleep(interval)

    async def _check_all_markets_points(self):
        """Проверяет поинты для всех активных маркетов и обновляет статус."""
        if not self.api:
            return

        for market_id, worker in list(self._workers.items()):
            if not self.running:
                break

            info = await self.api.get_market(market_id)
            if info is None:
                await asyncio.sleep(0.5)
                continue

            self._market_info_cache[market_id] = info
            has_points = self._has_active_points(info)
            was_blocked = market_id in self._points_blocked
            self._market_points_status[market_id] = has_points

            if not has_points and not was_blocked:
                # Поинты пропали — приостанавливаем маркет
                self._points_blocked.add(market_id)
                self.logger.log(
                    f"[PointsFilter] [{market_id}] Нет активных поинтов — "
                    f"приостанавливаю (ордера отменяю)"
                )
                worker.settings = worker.settings.model_copy(update={"enabled": False})
                ids = worker.get_active_order_ids()
                if ids and self.order_manager:
                    await self.order_manager.cancel_orders(ids, market_id=market_id)
                worker.order_yes = None
                worker.order_no = None

            elif has_points and was_blocked:
                # Поинты вернулись — восстанавливаем
                self._points_blocked.discard(market_id)
                saved = self.settings_store.get(market_id)
                self.logger.log(
                    f"[PointsFilter] [{market_id}] Поинты появились — возобновляю"
                )
                worker.settings = worker.settings.model_copy(update={"enabled": saved.enabled})
                if saved.enabled:
                    worker.schedule_reprocess()

            self._broadcast_state()
            await asyncio.sleep(0.5)  # небольшая пауза между запросами

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
