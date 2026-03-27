"""
Расчёт лимитных ордеров с кумулятивной ликвидностью.

Отличие от старого бота:
- cumulative_depth суммирует ВСЮ глубину от лучшей цены (как показывает сам PredictFun)
- find_price_at_depth ищет уровень, при котором накопленная ликвидность >= target
"""
from __future__ import annotations

from models import MarketSettings, OrderCalculation
from config import MIN_ORDER_PRICE, MAX_ORDER_PRICE, MIN_ORDER_VALUE_USD


class Calculator:

    @staticmethod
    def cumulative_depth(
        orderbook: dict,
        outcome: str,  # "yes" | "no"
        up_to_price: float,
    ) -> float:
        """
        Суммарная ликвидность (USD) от лучшей цены до up_to_price (включительно).
        Кумулятивно — как показывает сам PredictFun в ордербуке.
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        total = 0.0
        try:
            if outcome == "yes":
                # Бид YES: цены идут от лучшей (выше) вниз
                for price, shares in bids:
                    p = float(price)
                    if p < up_to_price - 1e-9:
                        break
                    total += p * float(shares)
            else:
                # Бид NO = Ask YES (инвертируем): цены идут снизу вверх по no_price
                for yes_price, shares in asks:
                    no_p = 1.0 - float(yes_price)
                    if no_p < up_to_price - 1e-9:
                        break
                    total += no_p * float(shares)
        except Exception:
            pass
        return total

    @staticmethod
    def find_price_at_depth(
        orderbook: dict,
        outcome: str,
        target_depth: float,
        mode: str = "bid",  # "bid" | "ask"
        decimal_precision: int = 3,
        min_orders: int = 0,
    ) -> float:
        """
        Находит цену, при которой кумулятивная ликвидность >= target_depth
        И количество уровней в стакане перед заявкой >= min_orders.
        Ставим ордер на один тик ниже этой цены.
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        tick = 1 / (10 ** decimal_precision)
        acc = 0.0
        levels_seen = 0

        try:
            if outcome == "yes":
                levels = [(float(p), float(s)) for p, s in bids]
                for price, shares in levels:
                    if mode == "bid":
                        acc += price * shares
                    else:
                        acc += (1.0 - price) * shares
                    levels_seen += 1
                    if acc >= target_depth and levels_seen >= min_orders:
                        return round(price - tick, decimal_precision)
                if levels:
                    return round(levels[-1][0] - tick, decimal_precision)

            else:  # no
                levels = [(round(1.0 - float(p), decimal_precision + 1), float(s), float(p)) for p, s in asks]
                for no_price, shares, yes_price in levels:
                    if mode == "bid":
                        acc += no_price * shares
                    else:
                        acc += yes_price * shares
                    levels_seen += 1
                    if acc >= target_depth and levels_seen >= min_orders:
                        return round(no_price - tick, decimal_precision)
                if levels:
                    return round(levels[-1][0] - tick, decimal_precision)
        except Exception:
            pass

        return 0.0

    @staticmethod
    def _round_price(price: float, decimal_precision: int) -> float:
        return round(price, decimal_precision)

    @staticmethod
    def _calc_shares(
        price: float,
        settings: MarketSettings,
    ) -> float:
        """Количество акций исходя из настроек позиции."""
        if settings.position_size_usdt is not None:
            usd = settings.position_size_usdt
            shares = usd / price if price > 0 else 0.0
        elif settings.position_size_shares is not None:
            shares = settings.position_size_shares
        else:
            shares = 0.0

        # Минимальный ордер $1
        if shares * price < MIN_ORDER_VALUE_USD:
            shares = MIN_ORDER_VALUE_USD / price if price > 0 else 0.0

        return round(round(shares, 1), 1)

    @classmethod
    def calculate(
        cls,
        orderbook: dict,
        settings: MarketSettings,
        decimal_precision: int = 3,
    ) -> OrderCalculation | None:
        """
        Основной метод: рассчитывает цены и размеры ордеров для YES и NO.
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return None

        try:
            best_bid_yes = float(bids[0][0])
            best_ask_yes = float(asks[0][0])
        except (IndexError, TypeError, ValueError):
            return None

        if best_bid_yes >= best_ask_yes:
            return None

        mid_yes = (best_bid_yes + best_ask_yes) / 2
        mid_no = 1.0 - mid_yes
        tick = 1 / (10 ** decimal_precision)
        mode = settings.liquidity_mode or "bid"
        target = settings.target_liquidity or 1000.0
        max_spread_frac = (settings.max_auto_spread or 6.0) / 100.0

        # Находим цену по кумулятивной ликвидности (и числу уровней, если задано)
        min_orders = settings.min_orders_before or 0
        price_yes = cls.find_price_at_depth(orderbook, "yes", target, mode, decimal_precision, min_orders)
        price_no = cls.find_price_at_depth(orderbook, "no", target, mode, decimal_precision, min_orders)

        # Не дальше чем max_auto_spread от mid
        price_yes = max(price_yes, mid_yes - max_spread_frac)
        price_no = max(price_no, mid_no - max_spread_frac)

        # Округление
        price_yes = cls._round_price(price_yes, decimal_precision)
        price_no = cls._round_price(price_no, decimal_precision)

        # Не выше best bid/ask (не пересекаем спред)
        best_no_ask = 1.0 - best_bid_yes
        price_yes = max(MIN_ORDER_PRICE, min(price_yes, best_ask_yes - tick, MAX_ORDER_PRICE))
        price_no = max(MIN_ORDER_PRICE, min(price_no, best_no_ask - tick, MAX_ORDER_PRICE))

        # Ликвидность перед нашим ордером (кумулятивная)
        liq_yes = cls.cumulative_depth(orderbook, "yes", price_yes + tick)
        liq_no = cls.cumulative_depth(orderbook, "no", price_no + tick)

        # Проверки: ликвидность достаточна И спред не меньше минимального
        min_spread_frac = (settings.min_spread or 0.2) / 100.0
        can_yes = (
            liq_yes >= target
            and abs(mid_yes - price_yes) >= min_spread_frac
        )
        can_no = (
            liq_no >= target
            and abs(mid_no - price_no) >= min_spread_frac
        )

        shares_yes = cls._calc_shares(price_yes, settings)
        shares_no = cls._calc_shares(price_no, settings)

        return OrderCalculation(
            mid_price_yes=mid_yes,
            mid_price_no=mid_no,
            best_bid_yes=best_bid_yes,
            best_ask_yes=best_ask_yes,
            buy_yes_price=price_yes,
            buy_yes_shares=shares_yes,
            buy_yes_value_usd=shares_yes * price_yes,
            buy_no_price=price_no,
            buy_no_shares=shares_no,
            buy_no_value_usd=shares_no * price_no,
            liquidity_yes=liq_yes,
            liquidity_no=liq_no,
            min_liquidity=target,
            can_place_yes=can_yes,
            can_place_no=can_no,
            spread_yes=abs(mid_yes - price_yes),
            spread_no=abs(mid_no - price_no),
        )
