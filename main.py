"""
Точка входа: запуск FastAPI + BotEngine в одном event loop.
"""
from __future__ import annotations

import asyncio
import sys

import uvicorn

from storage.accounts import load_accounts
from storage.settings_store import SettingsStore
from storage.config_store import ConfigStore
from utils.logger import BotLogger, EventBus
from core.engine import BotEngine
from web.app import app


async def main():
    from models import AccountInfo

    config_store = ConfigStore()
    saved = config_store.get()

    # Пробуем загрузить аккаунт: сначала accounts.txt, потом bot_config.json
    accounts = load_accounts()
    if accounts:
        account = accounts[0]
        print(f"Аккаунт из accounts.txt: {account.predict_account_address[:12]}...")
    elif saved.get("predict_account_address") and saved.get("privy_wallet_private_key"):
        account = AccountInfo(
            api_key=saved["api_key"],
            predict_account_address=saved["predict_account_address"],
            privy_wallet_private_key=saved["privy_wallet_private_key"],
            proxy=saved.get("proxy") or None,
        )
        print(f"Аккаунт из bot_config.json: {account.predict_account_address[:12]}...")
    else:
        # Запускаем UI без аккаунта — пользователь введёт данные через настройки
        print("Аккаунт не найден — запускаю UI для ввода настроек")
        account = AccountInfo(
            api_key="",
            predict_account_address="0x0000000000000000000000000000000000000000",
            privy_wallet_private_key="0000000000000000000000000000000000000000000000000000000000000001",
        )

    event_bus = EventBus()
    settings_store = SettingsStore()
    logger = BotLogger(event_bus)
    engine = BotEngine(account, settings_store, event_bus, logger)

    app.state.engine = engine
    app.state.event_bus = event_bus
    app.state.logger = logger
    app.state.config_store = config_store

    # Применяем Telegram из сохранённых настроек
    import config as cfg
    cfg.TELEGRAM_TOKEN = saved.get("telegram_token", "") or cfg.TELEGRAM_TOKEN
    cfg.TELEGRAM_CHAT_ID = saved.get("telegram_chat_id", "") or cfg.TELEGRAM_CHAT_ID

    from config import WEB_HOST, WEB_PORT
    config = uvicorn.Config(
        app,
        host=WEB_HOST,
        port=WEB_PORT,
        log_level="warning",
        loop="none",
    )
    server = uvicorn.Server(config)

    logger.log(f"Web UI: http://localhost:{WEB_PORT}")
    logger.log("Откройте браузер и нажмите СТАРТ")

    await asyncio.gather(server.serve(), return_exceptions=True)


async def demo():
    """Демо-режим: запускает только UI без реального бота."""
    from models import AccountInfo

    dummy_account = AccountInfo(
        api_key="demo",
        predict_account_address="0x0000000000000000000000000000000000000000",
        privy_wallet_private_key="0000000000000000000000000000000000000000000000000000000000000001",
    )

    event_bus = EventBus()
    settings_store = SettingsStore()
    config_store = ConfigStore()
    logger = BotLogger(event_bus)

    # Создаём движок но НЕ стартуем (predict_sdk не нужен)
    engine = BotEngine(dummy_account, settings_store, event_bus, logger)

    app.state.engine = engine
    app.state.event_bus = event_bus
    app.state.logger = logger
    app.state.config_store = config_store

    from config import WEB_HOST, WEB_PORT
    config = uvicorn.Config(
        app,
        host=WEB_HOST,
        port=WEB_PORT,
        log_level="warning",
        loop="none",
    )
    server = uvicorn.Server(config)

    logger.log("ДЕМО-РЕЖИМ: UI работает, торговля отключена")
    logger.log(f"Открой в браузере: http://localhost:{WEB_PORT}")

    await asyncio.gather(server.serve(), return_exceptions=True)


if __name__ == "__main__":
    is_demo = "--demo" in sys.argv
    try:
        asyncio.run(demo() if is_demo else main())
    except KeyboardInterrupt:
        print("\nОстановлено")
