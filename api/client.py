"""
Async API клиент Predict Fun.
Один aiohttp.ClientSession на весь бот — переиспользует TCP соединения.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from api.auth import get_auth_headers, get_auth_jwt
from config import API_BASE_URL, format_proxy_for_aiohttp

PAGE_SIZE = 100


class APIClient:
    def __init__(
        self,
        api_key: str,
        jwt_token: str,
        predict_account_address: str,
        privy_wallet_private_key: str,
        proxy: str | None = None,
        log_func=print,
    ):
        self.api_key = api_key
        self.jwt_token = jwt_token
        self.predict_account_address = predict_account_address
        self.privy_wallet_private_key = privy_wallet_private_key
        self._proxy_raw = proxy  # оригинальная строка для auth
        self.proxy_url = format_proxy_for_aiohttp(proxy)
        self.log_func = log_func
        self._session: aiohttp.ClientSession | None = None

    @property
    def headers(self) -> dict:
        return get_auth_headers(self.jwt_token, self.api_key)

    async def start(self):
        """Создаёт сессию. Вызвать один раз при старте бота."""
        connector = aiohttp.TCPConnector(limit=20, keepalive_timeout=30)
        self._session = aiohttp.ClientSession(connector=connector)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None

    async def _refresh_jwt(self) -> bool:
        try:
            self.log_func("Обновление JWT...")
            new_jwt = await get_auth_jwt(
                self.api_key, self.predict_account_address,
                self.privy_wallet_private_key, proxy=self._proxy_raw,
                log_func=self.log_func,
            )
            if new_jwt:
                self.jwt_token = new_jwt
                return True
        except Exception as e:
            self.log_func(f"✗ JWT refresh: {e}")
        return False

    async def _get(self, path: str, params: dict | None = None, timeout: int = 10) -> Any | None:
        assert self._session, "APIClient не запущен, вызовите start()"
        for attempt in range(3):
            try:
                async with self._session.get(
                    f"{API_BASE_URL}{path}",
                    headers=self.headers,
                    params=params,
                    proxy=self.proxy_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 401:
                        if await self._refresh_jwt():
                            continue
                        return None
                    if not resp.ok:
                        return None
                    data = await resp.json()
                    return data.get("data") if data.get("success") else None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    async def _post(self, path: str, body: dict, timeout: int = 20) -> Any | None:
        assert self._session, "APIClient не запущен, вызовите start()"
        for attempt in range(3):
            try:
                async with self._session.post(
                    f"{API_BASE_URL}{path}",
                    headers=self.headers,
                    json=body,
                    proxy=self.proxy_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    text = await resp.text()
                    if resp.status == 401:
                        if await self._refresh_jwt():
                            continue
                        return None
                    if not resp.ok:
                        return {"ok": False, "status": resp.status, "text": text}
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    # ── Маркеты ──────────────────────────────────────────────────────────────

    async def get_market(self, market_id: str) -> dict | None:
        return await self._get(f"/v1/markets/{market_id}")

    # ── Ордера ───────────────────────────────────────────────────────────────

    async def get_open_orders(self) -> list[dict] | None:
        """Все открытые ордера (с пагинацией, retry + refresh JWT).
        Возвращает None при ошибке API, [] если ордеров нет."""
        all_orders: list[dict] = []
        after: str | None = None
        first_page = True
        while True:
            params: dict = {"status": "OPEN", "first": str(PAGE_SIZE)}
            if after:
                params["after"] = after
            page = await self._get_raw_page("/v1/orders", params)
            if page is None:
                # Ошибка API на первой странице — возвращаем None чтобы
                # отличить от "реально нет ордеров"
                if first_page:
                    return None
                break
            first_page = False
            orders = page.get("data", [])
            all_orders.extend(orders)
            cursor = page.get("cursor")
            if not cursor or len(orders) < PAGE_SIZE:
                break
            after = cursor
        return all_orders

    async def _get_raw_page(self, path: str, params: dict, timeout: int = 12) -> dict | None:
        """GET запрос с retry и refresh JWT, возвращает сырой JSON."""
        assert self._session, "APIClient не запущен, вызовите start()"
        for attempt in range(3):
            try:
                async with self._session.get(
                    f"{API_BASE_URL}{path}",
                    headers=self.headers,
                    params=params,
                    proxy=self.proxy_url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 401:
                        if await self._refresh_jwt():
                            continue
                        return None
                    if not resp.ok:
                        return None
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    async def get_order(self, order_id: str) -> dict | None:
        """Статус конкретного ордера. Поддерживает оба формата ответа API."""
        raw = await self._get_raw_page(f"/v1/orders/{order_id}", {})
        if not raw:
            return None
        # {"success": true, "data": {...}} или просто {"data": {...}}
        data = raw.get("data")
        if isinstance(data, dict):
            return data
        # Иногда API возвращает сам объект ордера без обёртки
        if "status" in raw:
            return raw
        return None

    async def place_order(self, body: dict) -> dict | None:
        """Выставить лимитный ордер. Возвращает сырой ответ API."""
        return await self._post("/v1/orders", body)

    async def cancel_orders(self, order_ids: list[str]) -> bool:
        """Отменить ордера по ID. Возвращает True если успешно."""
        if not order_ids:
            return True
        BATCH = 50
        for i in range(0, len(order_ids), BATCH):
            batch = order_ids[i:i + BATCH]
            result = await self._post(
                "/v1/orders/remove",
                {"data": {"ids": [str(x) for x in batch]}},
                timeout=10,
            )
            if result is None:
                return False
            # _post() возвращает {"ok": False, "status": ...} при HTTP ошибках (нет поля "success")
            if isinstance(result, dict) and (result.get("ok") is False or not result.get("success", True)):
                return False
            if i + BATCH < len(order_ids):
                await asyncio.sleep(0.3)
        return True

    # ── Баланс ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> float | None:
        """Баланс USDT через predict_sdk (блокирующий вызов в thread)."""
        def _sync():
            from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions
            privy_key = self.privy_wallet_private_key
            if privy_key.startswith("0x"):
                privy_key = privy_key[2:]
            builder = OrderBuilder.make(
                ChainId.BNB_MAINNET,
                privy_key,
                OrderBuilderOptions(predict_account=self.predict_account_address),
            )
            try:
                return float(builder.balance_of()) / 10**18
            except Exception:
                return None

        try:
            return await asyncio.to_thread(_sync)
        except Exception:
            return None
