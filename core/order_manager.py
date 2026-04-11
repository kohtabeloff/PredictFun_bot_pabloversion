"""
OrderManager: подпись и размещение ордеров через predict_sdk.
Атомарный replace: cancel и place летят параллельно через asyncio.gather.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Callable

from api.client import APIClient
from models import OrderRecord
from config import AMOUNT_PRECISION, MIN_ORDER_VALUE_USD, MIN_ORDER_PRICE, MAX_ORDER_PRICE

PRECISION_RETRY_LIMIT = 3
PRECISION_BLOCK_HOURS = 24


def _round_wei(val) -> int:
    v = int(val)
    return (v // AMOUNT_PRECISION) * AMOUNT_PRECISION


def _is_precision_error(text: str) -> bool:
    return "InvalidPrecisionError" in text or (
        "Price precision" in text and "Max allowed is" in text
    )


def _parse_allowed_decimals(text: str) -> int | None:
    m = re.search(r"Max allowed is (\d+) decimal points", text)
    return int(m.group(1)) if m else None


def _get_token_id(market_info: dict, side: str) -> str | None:
    outcomes = market_info.get("outcomes", [])
    target = side.lower()
    for out in outcomes:
        name = (out.get("name") or "").lower()
        if (target == "yes" and name in ("yes", "y")) or (target == "no" and name in ("no", "n")) or name == target:
            tid = out.get("onChainId") or out.get("on_chain_id") or out.get("tokenId") or out.get("token_id") or out.get("id")
            if tid:
                return str(tid)
    # Fallback: yes=0, no=1
    idx = 0 if target == "yes" else 1
    if idx < len(outcomes):
        out = outcomes[idx]
        tid = out.get("onChainId") or out.get("on_chain_id") or out.get("tokenId") or out.get("token_id") or out.get("id")
        return str(tid) if tid else None
    return None


class OrderManager:
    def __init__(self, api_client: APIClient, market_info_cache: dict, log_func: Callable = print):
        self.api = api_client
        self.market_info_cache = market_info_cache  # market_id -> dict
        self.log_func = log_func
        self._blocked: dict[tuple, float] = {}  # (market_id, side) -> until_ts
        self._precision_errors: dict[tuple, int] = {}

    def _make_builder(self):
        """Создаёт новый экземпляр OrderBuilder (thread-safe, без shared state)."""
        from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions
        privy_key = self.api.privy_wallet_private_key
        if privy_key.startswith("0x"):
            privy_key = privy_key[2:]
        return OrderBuilder.make(
            ChainId.BNB_MAINNET,
            privy_key,
            OrderBuilderOptions(predict_account=self.api.predict_account_address),
        )

    def is_blocked(self, market_id: str, side: str) -> bool:
        key = (market_id, side)
        until = self._blocked.get(key)
        if until is None:
            return False
        if time.time() >= until:
            del self._blocked[key]
            return False
        return True

    def _block(self, market_id: str, side: str):
        key = (market_id, side)
        self._blocked[key] = time.time() + PRECISION_BLOCK_HOURS * 3600
        self.log_func(f"[{market_id}] {side.upper()}: precision error × {PRECISION_RETRY_LIMIT} — блок на {PRECISION_BLOCK_HOURS} ч")

    async def _build_and_sign(
        self, market_id: str, side: str, price: float, shares: float,
        order_side: str = "buy",
    ) -> tuple[dict, str] | None:
        """Строит подписанный ордер. order_side='buy'|'sell'. Возвращает (body, order_hash) или None."""
        market_info = self.market_info_cache.get(market_id)
        if not market_info:
            return None

        token_id = _get_token_id(market_info, side)
        if not token_id:
            self.log_func(f"[{market_id}] ✗ tokenId не найден для {side}")
            return None

        fee_rate = market_info.get("feeRateBps", 200)
        is_neg_risk = market_info.get("isNegRisk", False)
        is_yield = market_info.get("isYieldBearing", True)
        _order_side = order_side

        def _sync():
            from predict_sdk import Side, BuildOrderInput, LimitHelperInput
            builder = self._make_builder()
            sdk_side = Side.BUY if _order_side == "buy" else Side.SELL
            WEI = 10 ** 18
            price_wei = _round_wei(int(price * WEI))
            qty_wei = _round_wei(int(shares * WEI))
            amounts = builder.get_limit_order_amounts(
                LimitHelperInput(side=sdk_side, price_per_share_wei=price_wei, quantity_wei=qty_wei)
            )
            order = builder.build_order(
                "LIMIT",
                BuildOrderInput(
                    side=sdk_side,
                    token_id=str(token_id),
                    maker_amount=str(amounts.maker_amount),
                    taker_amount=str(amounts.taker_amount),
                    fee_rate_bps=fee_rate,
                ),
            )
            typed = builder.build_typed_data(order, is_neg_risk=is_neg_risk, is_yield_bearing=is_yield)
            signed = builder.sign_typed_data_order(typed)
            order_hash = builder.build_typed_data_hash(typed)

            try:
                d = signed.to_dict()
            except AttributeError:
                try:
                    d = signed.dict()
                except AttributeError:
                    side_int = 0 if _order_side == "buy" else 1
                    d = {
                        "salt": str(order.salt), "maker": order.maker, "signer": order.signer,
                        "taker": order.taker, "token_id": order.token_id,
                        "maker_amount": str(order.maker_amount), "taker_amount": str(order.taker_amount),
                        "expiration": str(order.expiration), "nonce": str(order.nonce),
                        "fee_rate_bps": order.fee_rate_bps, "side": side_int, "signature_type": 0,
                    }
                    d["signature"] = getattr(signed, "signature", getattr(signed, "sig", ""))

            key_map = {
                "maker_amount": "makerAmount", "taker_amount": "takerAmount",
                "token_id": "tokenId", "fee_rate_bps": "feeRateBps",
                "signature_type": "signatureType",
            }
            final = {}
            for k, v in d.items():
                ck = key_map.get(k, k)
                if ck == "signature":
                    v = ("0x" + str(v)) if v and not str(v).startswith("0x") else str(v)
                elif ck in ("makerAmount", "takerAmount"):
                    v = str(v)
                final[ck] = v
            final["hash"] = order_hash

            body = {
                "data": {
                    "pricePerShare": str(amounts.price_per_share),
                    "strategy": "LIMIT",
                    "slippageBps": "0",
                    "order": final,
                }
            }
            return body, order_hash

        try:
            return await asyncio.to_thread(_sync)
        except Exception as e:
            self.log_func(f"[{market_id}] ✗ Ошибка подписи ордера: {e}")
            return None

    async def place_order(
        self, market_id: str, side: str, price: float, shares: float
    ) -> OrderRecord | None:
        """Подписывает и выставляет ордер. Обрабатывает precision errors."""
        if self.is_blocked(market_id, side):
            return None

        dp = self.market_info_cache.get(market_id, {}).get("decimalPrecision", 3)
        price = round(max(MIN_ORDER_PRICE, min(price, MAX_ORDER_PRICE)), dp)
        if shares <= 0 or price <= 0:
            return None

        key = (market_id, side)
        current_shares = shares
        precision_retries = 0

        while True:
            result = await self._build_and_sign(market_id, side, price, current_shares)
            if result is None:
                return None

            body, order_hash = result
            resp = await self.api.place_order(body)

            if resp is None:
                self.log_func(f"[{market_id}] ✗ {side.upper()}: нет ответа от API")
                return None

            # Precision error
            if isinstance(resp, dict) and not resp.get("success", True):
                text = resp.get("text", "") or str(resp)
                if _is_precision_error(text):
                    precision_retries += 1
                    self._precision_errors[key] = self._precision_errors.get(key, 0) + 1
                    if precision_retries >= PRECISION_RETRY_LIMIT:
                        self._block(market_id, side)
                        return None
                    allowed = _parse_allowed_decimals(text)
                    if allowed is not None:
                        price = round(price, allowed)
                        price = max(MIN_ORDER_PRICE, min(price, MAX_ORDER_PRICE))
                        self.log_func(f"[{market_id}] Price precision, повтор с {price*100:.2f}¢")
                    else:
                        new_usd = (current_shares * price) - 0.05
                        if new_usd < MIN_ORDER_VALUE_USD:
                            return None
                        current_shares = new_usd / price
                        self.log_func(f"[{market_id}] InvalidPrecisionError, повтор с ${new_usd:.2f}")
                    continue
                status = resp.get("status", "?")
                self.log_func(f"[{market_id}] ✗ {side.upper()}: HTTP {status}")
                return None

            if isinstance(resp, dict) and resp.get("success"):
                od = resp.get("data", {})
                oid = str(od.get("id") or od.get("orderId") or "")
                self._precision_errors[key] = 0
                self.log_func(f"[{market_id}] {side.upper()} ✓ {price*100:.1f}¢ id={oid}")
                return OrderRecord(
                    order_id=oid,
                    market_id=market_id,
                    side=side,
                    price=price,
                    shares=current_shares,
                )

            return None

    async def cancel_orders(self, order_ids: list[str], market_id: str = "") -> bool:
        """Отменяет ордера по ID."""
        if not order_ids:
            return True
        ok = await self.api.cancel_orders(order_ids)
        if ok:
            self.log_func(f"[{market_id or '?'}] ✓ Отменено {len(order_ids)} ордер(ов)")
        return ok

    async def atomic_replace(
        self,
        market_id: str,
        side: str,
        old_order_id: str | None,
        new_price: float,
        new_shares: float,
    ) -> OrderRecord | None:
        """
        Атомарный replace: cancel и place летят параллельно.
        Зазор минимален — оба запроса уходят почти одновременно.
        """
        if old_order_id:
            cancel_task = asyncio.create_task(
                self.cancel_orders([old_order_id], market_id=market_id)
            )
            place_task = asyncio.create_task(
                self.place_order(market_id, side, new_price, new_shares)
            )
            results = await asyncio.gather(cancel_task, place_task, return_exceptions=True)
            cancel_ok = results[0] is True
            new_record = results[1] if not isinstance(results[1], Exception) else None
            # Если cancel не прошёл, но новый ордер выставился — отменяем его,
            # чтобы не держать два ордера одновременно в стакане.
            if not cancel_ok and new_record is not None:
                self.log_func(
                    f"[{market_id}] ⚠ cancel не прошёл, отменяю новый ордер {new_record.order_id}"
                )
                await self.cancel_orders([new_record.order_id], market_id=market_id)
                return None
            return new_record
        else:
            return await self.place_order(market_id, side, new_price, new_shares)

    async def sell_market(
        self, market_id: str, side: str, shares: float, mid_price: float | None = None
    ) -> bool:
        """
        Продаёт позицию (авто-продажа при исполнении лимитки).
        mid_price — последняя известная рыночная цена из стакана.
        Продаём на 3¢ ниже mid чтобы гарантированно попасть в зону бидов.
        Если mid_price не передан — делаем API запрос за текущей ценой.
        """
        if shares <= 0:
            return False
        market_info = self.market_info_cache.get(market_id)
        if not market_info:
            self.log_func(f"[{market_id}] ✗ sell_market: нет info о маркете")
            return False

        # Получаем актуальную цену
        if mid_price is None:
            info = await self.api.get_market(market_id)
            if info:
                # lastTradePrice — это цена YES (вероятность), для NO инвертируем
                raw = info.get("lastTradePrice") or info.get("midPrice") or 0.5
                mid_price = (1.0 - raw) if side == "no" else raw
            else:
                mid_price = 0.5  # fallback

        # Продаём на 1 тик ниже mid — практически по рыночной цене
        dp = market_info.get("decimalPrecision", 3)
        tick = 1 / (10 ** dp)
        sell_price = round(mid_price - tick, dp)
        sell_price = max(MIN_ORDER_PRICE, min(sell_price, MAX_ORDER_PRICE))

        self.log_func(
            f"[{market_id}] ⚠ Авто-продажа {side.upper()} {shares:.2f} шт "
            f"по {sell_price*100:.1f}¢ (mid={mid_price*100:.1f}¢)"
        )

        result = await self._build_and_sign(market_id, side, sell_price, shares, order_side="sell")
        if result is None:
            self.log_func(f"[{market_id}] ✗ Авто-продажа: не удалось подписать ордер")
            return False

        body, _ = result
        resp = await self.api.place_order(body)
        if resp and isinstance(resp, dict) and resp.get("success"):
            self.log_func(f"[{market_id}] ✓ Авто-продажа {side.upper()} выполнена")
            return True

        self.log_func(f"[{market_id}] ✗ Авто-продажа не удалась: {resp}")
        return False
