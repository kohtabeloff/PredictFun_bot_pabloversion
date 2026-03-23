"""
Хранилище глобальных настроек бота (Telegram, аккаунт).
Сохраняет в bot_config.json рядом с проектом.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from config import BASE_DIR

CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")

_DEFAULTS = {
    "api_key": "",
    "predict_account_address": "",
    "privy_wallet_private_key": "",
    "proxy": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "ui_password": "",  # пустой = защита отключена
}


class ConfigStore:
    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        if Path(CONFIG_FILE).exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self) -> dict:
        result = dict(_DEFAULTS)
        result.update(self._data)
        return result

    def update(self, **kwargs) -> dict:
        for k, v in kwargs.items():
            if k in _DEFAULTS:
                self._data[k] = v
        self._save()
        return self.get()

    def get_password(self) -> str:
        return self._data.get("ui_password", "")
