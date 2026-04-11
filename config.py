"""
Конфигурация PredictFun Liquidity Bot
"""
from __future__ import annotations

import os

# API
API_BASE_URL = "https://api.predict.fun"
WS_URL = "wss://ws.predict.fun/ws"

# Пути
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = BASE_DIR  # переопределяется через set_data_dir() до создания storage-объектов
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.txt")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOGS_DIR = os.path.join(DATA_DIR, "logs")


def set_data_dir(path: str):
    """Переключает все пути к данным на указанную папку.
    Вызывать ДО создания ConfigStore / SettingsStore / BotLogger."""
    global DATA_DIR, ACCOUNTS_FILE, SETTINGS_FILE, LOGS_DIR
    DATA_DIR = os.path.abspath(path)
    os.makedirs(DATA_DIR, exist_ok=True)
    ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.txt")
    SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
    LOGS_DIR = os.path.join(DATA_DIR, "logs")

# Дефолтные настройки маркета
DEFAULT_POSITION_SIZE_USDT = 100.0
DEFAULT_MIN_SPREAD = 0.2           # центы
DEFAULT_TARGET_LIQUIDITY = 1000.0  # USD
DEFAULT_MAX_AUTO_SPREAD = 6.0      # процент
DEFAULT_LIQUIDITY_MODE = "bid"     # bid | ask

# Волатильность
DEFAULT_VOLATILE_REPOSITION_LIMIT = 0   # 0 = выключена
DEFAULT_VOLATILE_WINDOW_SECONDS = 60
DEFAULT_VOLATILE_COOLDOWN_SECONDS = 300  # 5 мин (вместо 1 часа в старом боте)

# Ордера
MIN_ORDER_VALUE_USD = 1.0
MIN_ORDER_PRICE = 0.001
MAX_ORDER_PRICE = 0.999
AMOUNT_PRECISION = 10**13  # округление wei

# Inspector
INSPECTOR_INTERVAL_SEC = 10
EXECUTION_GUARD_INTERVAL_SEC = 3

# Web
WEB_HOST = "0.0.0.0"
WEB_PORT = 8080

# Telegram
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def format_proxy(proxy_string: str | None) -> dict | None:
    """Формат прокси для requests."""
    if not proxy_string:
        return None
    if isinstance(proxy_string, dict):
        return proxy_string
    if not proxy_string.startswith("http://"):
        proxy_string = f"http://{proxy_string}"
    return {"http": proxy_string, "https": proxy_string}


def format_proxy_for_aiohttp(proxy_string: str | None) -> str | None:
    """Формат прокси для aiohttp."""
    if not proxy_string:
        return None
    if not proxy_string.startswith(("http://", "https://")):
        return f"http://{proxy_string}"
    return proxy_string
