"""
WebSocket клиент Predict Fun.
Dispatch входящих обновлений по очередям воркеров.
Использует aiohttp для поддержки прокси.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

import aiohttp

from config import WS_URL, format_proxy_for_aiohttp


class PredictWebSocket:
    def __init__(self, api_key: str, log_func: Callable = print, proxy: str | None = None):
        url = f"{WS_URL}?apiKey={api_key}" if api_key else WS_URL
        self._url = url
        self._proxy = format_proxy_for_aiohttp(proxy)  # гарантируем http:// префикс
        self.log_func = log_func
        self._queues: dict[str, asyncio.Queue] = {}  # market_id -> Queue воркера
        self._subscriptions: set[str] = set()
        self._ws = None
        self._running = False
        self._connected = False
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def subscribe(self, market_id: str, queue: asyncio.Queue):
        """Привязывает очередь воркера к market_id."""
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
        """Подписывает маркеты батчами, чтобы не заливать WS сотнями subscribe подряд."""
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
        """Получает snapshots для нескольких маркетов через одно WS-соединение."""
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
        """Одноразово получает snapshot стакана через отдельное WS-подключение."""
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
                                                # Дроп старого обновления, кладём новое
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
