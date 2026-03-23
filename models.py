"""
Модели данных бота
"""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

from config import (
    DEFAULT_LIQUIDITY_MODE,
    DEFAULT_MAX_AUTO_SPREAD,
    DEFAULT_MIN_SPREAD,
    DEFAULT_POSITION_SIZE_USDT,
    DEFAULT_TARGET_LIQUIDITY,
    DEFAULT_VOLATILE_COOLDOWN_SECONDS,
    DEFAULT_VOLATILE_REPOSITION_LIMIT,
    DEFAULT_VOLATILE_WINDOW_SECONDS,
)


class MarketSettings(BaseModel):
    """Настройки одного маркета."""
    market_id: str
    enabled: bool = False
    side: Literal["both", "yes", "no"] = "both"
    position_size_usdt: float | None = DEFAULT_POSITION_SIZE_USDT
    position_size_shares: float | None = None
    min_spread: float = DEFAULT_MIN_SPREAD
    target_liquidity: float = DEFAULT_TARGET_LIQUIDITY
    max_auto_spread: float = DEFAULT_MAX_AUTO_SPREAD
    liquidity_mode: Literal["bid", "ask"] = DEFAULT_LIQUIDITY_MODE
    volatile_reposition_limit: int = DEFAULT_VOLATILE_REPOSITION_LIMIT
    volatile_window_seconds: float = DEFAULT_VOLATILE_WINDOW_SECONDS
    volatile_cooldown_seconds: float = DEFAULT_VOLATILE_COOLDOWN_SECONDS


class OrderRecord(BaseModel):
    """Информация о выставленном ордере."""
    order_id: str
    market_id: str
    side: str  # "yes" | "no"
    price: float
    shares: float
    placed_at: float = Field(default_factory=time.time)


class OrderCalculation(BaseModel):
    """Результат расчёта ордеров для маркета."""
    mid_price_yes: float = 0.0
    mid_price_no: float = 0.0
    best_bid_yes: float = 0.0
    best_ask_yes: float = 0.0

    buy_yes_price: float = 0.0
    buy_yes_shares: float = 0.0
    buy_yes_value_usd: float = 0.0

    buy_no_price: float = 0.0
    buy_no_shares: float = 0.0
    buy_no_value_usd: float = 0.0

    liquidity_yes: float = 0.0
    liquidity_no: float = 0.0
    min_liquidity: float = 0.0

    can_place_yes: bool = False
    can_place_no: bool = False

    spread_yes: float = 0.0
    spread_no: float = 0.0


class MarketState(BaseModel):
    """Состояние маркета для UI."""
    market_id: str
    title: str = ""
    status: str = ""
    image_url: str = ""
    settings: MarketSettings
    order_yes: OrderRecord | None = None
    order_no: OrderRecord | None = None
    last_calculation: OrderCalculation | None = None
    ws_connected: bool = False
    last_update: float = 0.0


class AccountInfo(BaseModel):
    """Данные аккаунта."""
    api_key: str
    predict_account_address: str
    privy_wallet_private_key: str
    proxy: str | None = None


class BotState(BaseModel):
    """Полное состояние бота для UI."""
    running: bool = False
    ws_connected: bool = False
    account_address: str = ""
    balance_usdt: float | None = None
    markets: dict[str, MarketState] = {}
    total_open_orders: int = 0
