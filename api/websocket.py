"""
WebSocket клиент Predict Fun.
Dispatch входящих обновлений по очередям воркеров.

PredictWebSocket — одиночное соединение (используется для bootstrap snapshots).
WebSocketPool    — пул из N соединений с дедупликацией и ребалансировкой медленных слотов.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

import aiohttp

from config import WS_URL, format_proxy_for_aiohttp


# ─────────────────────────────────────────────────────────────────────────────
# Утилита
# ─────────────────────────────────────────────────────────────────────────────

def _orderbook_fingerprint(ob: dict, depth: int = 10) -> str:
    """Быстрый fingerprint стакана по верхним N уровням — для дедупликации."""
    bids = str(ob.get("bids", [])[:depth])
    asks = str(ob.get("asks", [])[:depth])
    return f"{bids}|{asks}"


# ─────────────────────────────────────────────────────────────────────────────
# Одиночное соединение (оставляем для bootstrap / fallback)
# ─────────────────────────────────────────────────────────────────────────────

class PredictWebSocket:
    def __init__(self, api_key: str, log_func: Callable = print, proxy: str | None = None):
        url = f"{WS_URL}?apiKey={api_key}" if api_key else WS_URL
        self._url = url
        self._proxy = format_proxy_for_aiohttp(proxy)
        self.log_func = log_func
        self._queues: dict[str, asyncio.Queue] = {}
        self._subscriptions: set[str] = set()
        self._ws = None
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def subscribe(self, market_id: str, queue: asyncio.Queue):
        self._queues[market_id] = queue
        self._subscriptions.add(market_id)
        if self._connected and self._ws:
            asyncio.create_task(self._send_subscribe(market_id))

    def unsubscribe(self, market_id: str):
        self._queues.pop(market_id, None)
        self._subscriptions.discard(market_id)

    async def _send_subscribe(self, market_id: str):
        if self._ws:
            msg = {"method": "subscribe", "requestId": id(market_id), "params": [f"predictOrderbook/{market_id}"]}
            try:
                await self._ws.send_str(json.dumps(msg))
            except Exception:
                pass

    async def subscribe_many(self, market_ids: list[str], batch_size: int = 25, pause_sec: float = 0.2):
        if not self._ws or not self._connected:
            return
        mids = [str(mid) for mid in market_ids if mid]
        for i in range(0, len(mids), batch_size):
            batch = mids[i:i + batch_size]
            for mid in batch:
                await self._send_subscribe(mid)
            if i + batch_size < len(mids):
                await asyncio.sleep(pause_sec)

    async def _send_heartbeat(self, ts):
        if self._ws:
            try:
                await self._ws.send_str(json.dumps({"method": "heartbeat", "data": ts}))
            except Exception:
                pass

    @staticmethod
    def _extract_orderbook_message(data: dict) -> tuple[str, dict] | None:
        if data.get("type") != "M":
            return None
        topic = data.get("topic", "")
        if not topic.startswith("predictOrderbook/"):
            return None
        market_id = topic.split("/", 1)[1]
        ob = data.get("data", {})
        if ob and (ob.get("bids") or ob.get("asks")):
            return market_id, ob
        return None

    async def fetch_snapshots_batch(self, market_ids: list[str], timeout: float = 15.0) -> dict[str, dict]:
        if not market_ids:
            return {}
        results: dict[str, dict] = {}
        remaining = set(str(mid) for mid in market_ids)
        try:
            async with asyncio.timeout(timeout):
                ws_kwargs: dict = {"heartbeat": 10.0}
                if self._proxy:
                    ws_kwargs["proxy"] = self._proxy
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self._url, **ws_kwargs) as ws:
                        for mid in list(remaining):
                            msg = {
                                "method": "subscribe",
                                "requestId": f"bootstrap-{mid}",
                                "params": [f"predictOrderbook/{mid}"],
                            }
                            await ws.send_str(json.dumps(msg))
                        async for message in ws:
                            if not remaining:
                                break
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue
                                if data.get("type") == "R":
                                    continue
                                if data.get("topic") == "heartbeat":
                                    try:
                                        await ws.send_str(json.dumps({"method": "heartbeat", "data": data.get("data")}))
                                    except Exception:
                                        pass
                                    continue
                                extracted = self._extract_orderbook_message(data)
                                if extracted:
                                    mid, ob = extracted
                                    if mid in remaining:
                                        results[mid] = ob
                                        remaining.discard(mid)
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
        except Exception:
            pass
        return results

    async def fetch_snapshot(self, market_id: str, timeout: float = 8.0) -> dict | None:
        try:
            async with asyncio.timeout(timeout):
                ws_kwargs: dict = {"heartbeat": 10.0}
                if self._proxy:
                    ws_kwargs["proxy"] = self._proxy
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self._url, **ws_kwargs) as ws:
                        msg = {
                            "method": "subscribe",
                            "requestId": f"bootstrap-{market_id}",
                            "params": [f"predictOrderbook/{market_id}"],
                        }
                        await ws.send_str(json.dumps(msg))
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue
                                if data.get("type") == "R":
                                    continue
                                if data.get("topic") == "heartbeat":
                                    try:
                                        await ws.send_str(json.dumps({"method": "heartbeat", "data": data.get("data")}))
                                    except Exception:
                                        pass
                                    continue
                                extracted = self._extract_orderbook_message(data)
                                if extracted and extracted[0] == market_id:
                                    return extracted[1]
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
        except Exception:
            return None
        return None

    async def _run(self):
        self._running = True
        reconnect_attempt = 0

        while self._running:
            try:
                ws_kwargs: dict = {"heartbeat": 10.0}
                if self._proxy:
                    ws_kwargs["proxy"] = self._proxy

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self._url, **ws_kwargs) as ws:
                        self._ws = ws
                        self._connected = True
                        reconnect_attempt = 0
                        self.log_func("[WS] ✓ Подключено")

                        await asyncio.sleep(0.3)
                        await self.subscribe_many(list(self._subscriptions))

                        async for message in ws:
                            if not self._running:
                                break
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue

                                if data.get("type") == "R":
                                    continue

                                if data.get("type") == "M":
                                    topic = data.get("topic", "")

                                    if topic == "heartbeat":
                                        await self._send_heartbeat(data.get("data"))
                                        continue

                                    extracted = self._extract_orderbook_message(data)
                                    if extracted:
                                        mid, ob = extracted
                                        q = self._queues.get(mid)
                                        if q:
                                            try:
                                                q.put_nowait(ob)
                                            except asyncio.QueueFull:
                                                try:
                                                    q.get_nowait()
                                                except asyncio.QueueEmpty:
                                                    pass
                                                try:
                                                    q.put_nowait(ob)
                                                except asyncio.QueueFull:
                                                    pass

                            elif message.type == aiohttp.WSMsgType.ERROR:
                                raise Exception(f"WS error: {ws.exception()}")
                            elif message.type == aiohttp.WSMsgType.CLOSE:
                                break

            except asyncio.CancelledError:
                self.log_func("[WS] Остановлен")
                break
            except Exception as e:
                reconnect_attempt += 1
                err = str(e) if e else repr(e)
                self.log_func(f"[WS] ✗ Ошибка (попытка {reconnect_attempt}): {err}")
            finally:
                self._ws = None
                self._connected = False

            if self._running:
                delay = min(5 * (1.5 ** reconnect_attempt), 60)
                self.log_func(f"[WS] Повтор через {delay:.0f} сек...")
                await asyncio.sleep(delay)

    def start(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket Pool
# ─────────────────────────────────────────────────────────────────────────────

class _PoolSlot:
    """Одно WS-соединение внутри пула. Вызывает on_update когда приходит обновление стакана."""

    def __init__(
        self,
        slot_id: int,
        url: str,
        proxy: str | None,
        on_update: Callable[[int, str, dict], None],
        log_func: Callable,
    ):
        self.slot_id = slot_id
        self._url = url
        self._proxy = proxy  # уже отформатирован (http://)
        self._on_update = on_update
        self.log_func = log_func
        self._subscriptions: set[str] = set()
        self._ws = None
        self._connected = False
        self._running = False
        self._task: asyncio.Task | None = None
        self.wins: int = 0  # сколько раз этот слот первым доставил уникальное обновление

    @property
    def connected(self) -> bool:
        return self._connected

    def set_subscriptions(self, market_ids: set[str]):
        self._subscriptions = set(market_ids)

    def add_subscription(self, market_id: str):
        self._subscriptions.add(market_id)
        if self._connected and self._ws:
            asyncio.create_task(self._send_subscribe(market_id))

    def remove_subscription(self, market_id: str):
        self._subscriptions.discard(market_id)

    async def _send_subscribe(self, market_id: str):
        if self._ws:
            msg = {
                "method": "subscribe",
                "requestId": f"s{self.slot_id}-{market_id}",
                "params": [f"predictOrderbook/{market_id}"],
            }
            try:
                await self._ws.send_str(json.dumps(msg))
            except Exception:
                pass

    async def _send_heartbeat(self, ts):
        if self._ws:
            try:
                await self._ws.send_str(json.dumps({"method": "heartbeat", "data": ts}))
            except Exception:
                pass

    async def _run(self):
        self._running = True
        reconnect_attempt = 0

        while self._running:
            try:
                ws_kwargs: dict = {"heartbeat": 10.0}
                if self._proxy:
                    ws_kwargs["proxy"] = self._proxy

                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self._url, **ws_kwargs) as ws:
                        self._ws = ws
                        self._connected = True
                        reconnect_attempt = 0
                        self.log_func(f"[WS Pool slot={self.slot_id}] ✓ Подключено")

                        # Подписываемся на все известные маркеты батчами
                        subs = list(self._subscriptions)
                        for i in range(0, len(subs), 25):
                            for mid in subs[i:i + 25]:
                                await self._send_subscribe(mid)
                            if i + 25 < len(subs):
                                await asyncio.sleep(0.2)

                        _msg_count = 0
                        _ob_count = 0
                        async for message in ws:
                            if not self._running:
                                break
                            if message.type == aiohttp.WSMsgType.TEXT:
                                _msg_count += 1
                                try:
                                    data = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue

                                if data.get("type") == "R":
                                    continue

                                if data.get("type") == "M":
                                    topic = data.get("topic", "")
                                    if topic == "heartbeat":
                                        await self._send_heartbeat(data.get("data"))
                                        if _msg_count <= 5:
                                            self.log_func(f"[WS Pool slot={self.slot_id}] heartbeat #{_msg_count}")
                                        continue
                                    if topic.startswith("predictOrderbook/"):
                                        market_id = topic.split("/", 1)[1]
                                        ob = data.get("data", {})
                                        _ob_count += 1
                                        if _ob_count <= 3:
                                            self.log_func(f"[WS Pool slot={self.slot_id}] orderbook {market_id}: bids={len(ob.get('bids') or [])}, asks={len(ob.get('asks') or [])}")
                                        if ob and (ob.get("bids") or ob.get("asks")):
                                            self._on_update(self.slot_id, market_id, ob)

                            elif message.type == aiohttp.WSMsgType.ERROR:
                                raise Exception(f"WS slot={self.slot_id} error: {ws.exception()}")
                            elif message.type == aiohttp.WSMsgType.CLOSE:
                                break

            except asyncio.CancelledError:
                break
            except Exception as e:
                reconnect_attempt += 1
                # Логируем только первые несколько ошибок, потом молчим
                if reconnect_attempt <= 3:
                    self.log_func(f"[WS Pool slot={self.slot_id}] ✗ {e}")
            finally:
                self._ws = None
                self._connected = False

            if self._running:
                delay = min(5 * (1.5 ** reconnect_attempt), 60)
                await asyncio.sleep(delay)

    def start(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._ws = None
        self._connected = False

    async def restart(self):
        """Перезапускает слот (используется при ребалансировке)."""
        self.stop()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self.wins = 0
        self.start()


class WebSocketPool:
    """
    Пул из N WebSocket-соединений.

    Публичный интерфейс совпадает с PredictWebSocket — engine.py не меняет
    логику работы, только подключается к более надёжному источнику данных.

    Защита от бага Predict.fun: каждые rebalance_interval_sec самые медленные
    слоты (по количеству "побед") перезапускаются — свежее соединение всегда
    получает данные, даже если старое замерло.
    """

    def __init__(
        self,
        api_key: str,
        log_func: Callable = print,
        proxy: str | None = None,
        pool_size: int = 20,
        rebalance_interval_sec: float = 120.0,
        slow_slots_per_rebalance: int = 5,
        dedupe_window_sec: float = 0.04,
        connect_stagger_ms: int = 80,
    ):
        url = f"{WS_URL}?apiKey={api_key}" if api_key else WS_URL
        self._url = url
        self._proxy = format_proxy_for_aiohttp(proxy)
        self.log_func = log_func
        self._pool_size = max(1, pool_size)
        self._rebalance_interval_sec = rebalance_interval_sec
        self._slow_slots_per_rebalance = slow_slots_per_rebalance
        self._dedupe_window_sec = dedupe_window_sec
        self._connect_stagger_ms = connect_stagger_ms

        self._queues: dict[str, asyncio.Queue] = {}
        self._subscriptions: set[str] = set()

        # Дедупликация: market_id -> (fingerprint, monotonic_timestamp)
        self._last_seen: dict[str, tuple[str, float]] = {}

        self._slots: list[_PoolSlot] = []
        self._rebalance_task: asyncio.Task | None = None
        self._start_task: asyncio.Task | None = None
        self._running = False
        self._first_connected = False

    # ── Публичный интерфейс (идентичен PredictWebSocket) ────────────────────

    @property
    def connected(self) -> bool:
        return any(s.connected for s in self._slots)

    def subscribe(self, market_id: str, queue: asyncio.Queue):
        self._queues[market_id] = queue
        self._subscriptions.add(market_id)
        for slot in self._slots:
            slot.add_subscription(market_id)

    def unsubscribe(self, market_id: str):
        self._queues.pop(market_id, None)
        self._subscriptions.discard(market_id)
        self._last_seen.pop(market_id, None)
        for slot in self._slots:
            slot.remove_subscription(market_id)

    async def subscribe_many(self, market_ids: list[str], batch_size: int = 25, pause_sec: float = 0.2):
        """Bootstrap: добавляем подписки и переподписываемся через первый живой слот."""
        for mid in market_ids:
            self._subscriptions.add(mid)
            for slot in self._slots:
                slot.add_subscription(mid)

        # Шлём subscribe через один живой слот, чтобы сервер прислал snapshot
        alive = next((s for s in self._slots if s.connected), None)
        if not alive:
            return
        mids = list(market_ids)
        for i in range(0, len(mids), batch_size):
            for mid in mids[i:i + batch_size]:
                await alive._send_subscribe(mid)
            if i + batch_size < len(mids):
                await asyncio.sleep(pause_sec)

    async def fetch_snapshots_batch(self, market_ids: list[str], timeout: float = 15.0) -> dict[str, dict]:
        """Одноразовый batch-fetch через отдельное соединение (для bootstrap)."""
        if not market_ids:
            return {}
        results: dict[str, dict] = {}
        remaining = set(str(mid) for mid in market_ids)
        try:
            async with asyncio.timeout(timeout):
                ws_kwargs: dict = {"heartbeat": 10.0}
                if self._proxy:
                    ws_kwargs["proxy"] = self._proxy
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self._url, **ws_kwargs) as ws:
                        for mid in list(remaining):
                            await ws.send_str(json.dumps({
                                "method": "subscribe",
                                "requestId": f"bootstrap-{mid}",
                                "params": [f"predictOrderbook/{mid}"],
                            }))
                        async for message in ws:
                            if not remaining:
                                break
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue
                                if data.get("type") == "R":
                                    continue
                                if data.get("topic") == "heartbeat":
                                    try:
                                        await ws.send_str(json.dumps({"method": "heartbeat", "data": data.get("data")}))
                                    except Exception:
                                        pass
                                    continue
                                topic = data.get("topic", "")
                                if data.get("type") == "M" and topic.startswith("predictOrderbook/"):
                                    mid = topic.split("/", 1)[1]
                                    ob = data.get("data", {})
                                    if ob and (ob.get("bids") or ob.get("asks")) and mid in remaining:
                                        results[mid] = ob
                                        remaining.discard(mid)
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
        except Exception:
            pass
        return results

    async def fetch_snapshot(self, market_id: str, timeout: float = 8.0) -> dict | None:
        """Одноразовый snapshot через отдельное соединение."""
        try:
            async with asyncio.timeout(timeout):
                ws_kwargs: dict = {"heartbeat": 10.0}
                if self._proxy:
                    ws_kwargs["proxy"] = self._proxy
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self._url, **ws_kwargs) as ws:
                        await ws.send_str(json.dumps({
                            "method": "subscribe",
                            "requestId": f"bootstrap-{market_id}",
                            "params": [f"predictOrderbook/{market_id}"],
                        }))
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(message.data)
                                except json.JSONDecodeError:
                                    continue
                                if data.get("type") == "R":
                                    continue
                                if data.get("topic") == "heartbeat":
                                    try:
                                        await ws.send_str(json.dumps({"method": "heartbeat", "data": data.get("data")}))
                                    except Exception:
                                        pass
                                    continue
                                topic = data.get("topic", "")
                                if data.get("type") == "M" and topic == f"predictOrderbook/{market_id}":
                                    ob = data.get("data", {})
                                    if ob and (ob.get("bids") or ob.get("asks")):
                                        return ob
                            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
        except Exception:
            pass
        return None

    def start(self):
        self._running = True
        self._slots = [
            _PoolSlot(
                slot_id=i,
                url=self._url,
                proxy=self._proxy,
                on_update=self._on_slot_update,
                log_func=self.log_func,
            )
            for i in range(self._pool_size)
        ]
        for slot in self._slots:
            slot.set_subscriptions(self._subscriptions)
        self._start_task = asyncio.create_task(self._start_staggered())

    def stop(self):
        self._running = False
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
        if self._rebalance_task and not self._rebalance_task.done():
            self._rebalance_task.cancel()
        for slot in self._slots:
            slot.stop()

    # ── Внутренняя логика ────────────────────────────────────────────────────

    async def _start_staggered(self):
        """Стартует слоты с паузой между ними, чтобы не получить 429."""
        self.log_func(f"[WS Pool] Запуск {self._pool_size} соединений...")
        for slot in self._slots:
            if not self._running:
                break
            slot.start()
            if self._connect_stagger_ms > 0:
                await asyncio.sleep(self._connect_stagger_ms / 1000)
        if self._running:
            self._rebalance_task = asyncio.create_task(self._rebalance_loop())

    def _on_slot_update(self, slot_id: int, market_id: str, ob: dict):
        """Вызывается каждым слотом при получении обновления стакана."""
        now = time.monotonic()
        fp = _orderbook_fingerprint(ob)

        # Дедупликация: если тот же fingerprint пришёл недавно — дроп
        last = self._last_seen.get(market_id)
        if last is not None:
            last_fp, last_ts = last
            if last_fp == fp and (now - last_ts) < self._dedupe_window_sec:
                return

        # Этот слот выиграл гонку — он первым принёс уникальное обновление
        self._last_seen[market_id] = (fp, now)
        self._slots[slot_id].wins += 1

        if not self._first_connected:
            self._first_connected = True
            self.log_func(f"[WS Pool] ✓ Первое соединение установлено (slot={slot_id})")

        q = self._queues.get(market_id)
        if q:
            try:
                q.put_nowait(ob)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(ob)
                except asyncio.QueueFull:
                    pass

    async def _rebalance_loop(self):
        """Периодически перезапускает самые медленные слоты."""
        while self._running:
            await asyncio.sleep(self._rebalance_interval_sec)
            if not self._running:
                break
            try:
                await self._rebalance()
            except Exception as e:
                self.log_func(f"[WS Pool] Ошибка ребалансировки: {e}")

    async def _rebalance(self):
        if len(self._slots) < 2:
            return

        sorted_slots = sorted(self._slots, key=lambda s: s.wins)
        top_wins = sorted_slots[-1].wins

        # Не принимаем решение если активности ещё мало
        if top_wins < 10:
            return

        n = min(self._slow_slots_per_rebalance, len(self._slots) // 2)
        to_restart = sorted_slots[:n]

        self.log_func(
            f"[WS Pool] Ребалансировка: перезапускаю {len(to_restart)} медленных слотов "
            f"(лучший: {top_wins} побед, худший: {to_restart[0].wins})"
        )

        for slot in to_restart:
            slot.set_subscriptions(self._subscriptions)
            await slot.restart()
            # Пауза между перезапусками чтобы не получить 429
            await asyncio.sleep(self._connect_stagger_ms / 1000 + 1.5)

        # Сбрасываем счётчики — начинаем отсчёт заново
        for slot in self._slots:
            slot.wins = 0
