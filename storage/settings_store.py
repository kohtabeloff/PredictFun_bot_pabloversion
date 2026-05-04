"""
Хранилище настроек маркетов (JSON файл).
"""
from __future__ import annotations

import json
import os
import tempfile

from models import MarketSettings


class SettingsStore:
    def __init__(self, path: str | None = None):
        if path is None:
            import config as cfg
            path = cfg.SETTINGS_FILE
        self._path: str = path
        self._data: dict[str, MarketSettings] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for mid, d in raw.items():
                d["market_id"] = mid
                self._data[mid] = MarketSettings(**d)
        except Exception:
            pass

    def save(self):
        try:
            raw = {mid: s.model_dump() for mid, s in self._data.items()}
            dir_ = os.path.dirname(self._path) or "."
            with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False,
                                             suffix=".tmp", encoding="utf-8") as tf:
                json.dump(raw, tf, indent=2, ensure_ascii=False)
                tmp_path = tf.name
            os.replace(tmp_path, self._path)
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")

    def has(self, market_id: str) -> bool:
        return market_id in self._data

    def get(self, market_id: str) -> MarketSettings:
        if market_id not in self._data:
            self._data[market_id] = MarketSettings(market_id=market_id)
        return self._data[market_id]

    def update(self, market_id: str, **kwargs) -> MarketSettings:
        s = self.get(market_id)
        updated = s.model_copy(update=kwargs)
        self._data[market_id] = updated
        self.save()
        return updated

    def remove(self, market_id: str):
        self._data.pop(market_id, None)
        self.save()

    def all(self) -> dict[str, MarketSettings]:
        return dict(self._data)
