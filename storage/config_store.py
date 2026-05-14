"""
Хранилище глобальных настроек бота (Telegram, аккаунт).
Сохраняет в bot_config.json внутри DATA_DIR.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


_DEFAULTS = {
    "api_key": "",
    "predict_account_address": "",
    "privy_wallet_private_key": "",
    "proxy": "",
    "telegram_token": "",
    "telegram_chat_id": "",
    "ui_password": "",  # пустой = защита отключена
    "predict_points_only": False,
    "predict_points_poll_sec": 600,
    "auto_sell_enabled": False,
    "auto_sell_max_loss_pct": 15.0,
    "auto_sell_delay_sec": 0.0,
    "auto_sell_order_expiry_sec": 0,
}


class ConfigStore:
    def __init__(self, path: str | None = None):
        if path is None:
            import config as cfg
            path = os.path.join(cfg.DATA_DIR, "bot_config.json")
        self._path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        if Path(self._path).exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        dir_ = os.path.dirname(self._path) or "."
        with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False,
                                         suffix=".tmp", encoding="utf-8") as tf:
            json.dump(self._data, tf, ensure_ascii=False, indent=2)
            tmp_path = tf.name
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self._path)

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
