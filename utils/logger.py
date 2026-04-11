"""
Логирование: файл + broadcast в Web UI через EventBus.
"""
from __future__ import annotations

import asyncio
import datetime
import os
from typing import Callable

class EventBus:
    """Простой pub/sub для real-time обновлений UI."""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    def emit(self, event: dict):
        dead = []
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)


class BotLogger:
    def __init__(self, event_bus: EventBus):
        import config as cfg
        self.event_bus = event_bus
        self._log_file: str | None = None
        self._recent: list[dict] = []  # последние 500 строк для новых подключений UI
        self._logs_dir = cfg.LOGS_DIR
        os.makedirs(self._logs_dir, exist_ok=True)

    def _open_log_file(self):
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._log_file = os.path.join(self._logs_dir, f"session_{ts}.log")

    def log(self, message: str, level: str = "INFO"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        print(line)

        # Файл
        if self._log_file is None:
            self._open_log_file()
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

        # UI broadcast
        entry = {"type": "log", "ts": ts, "level": level, "msg": message}
        self._recent.append(entry)
        if len(self._recent) > 500:
            self._recent.pop(0)
        self.event_bus.emit(entry)

    def get_recent(self, n: int = 100) -> list[dict]:
        return self._recent[-n:]

    def __call__(self, message: str):
        """Позволяет передавать logger как log_func=logger."""
        self.log(message)


def make_log_func(logger: BotLogger, prefix: str = "") -> Callable[[str], None]:
    def _log(msg: str):
        logger.log(f"{prefix}{msg}")
    return _log
