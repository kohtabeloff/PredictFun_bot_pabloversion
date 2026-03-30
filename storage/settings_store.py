"""
Хранилище настроек маркетов (JSON файл).
"""
from __future__ import annotations

import json
import os

from models import MarketSettings
from config import SETTINGS_FILE


class SettingsStore:
    def __init__(self, path: str = SETTINGS_FILE):
        self._path = path
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
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2, ensure_ascii=False)
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
