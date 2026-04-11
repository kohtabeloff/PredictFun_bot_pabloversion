"""
Точка входа менеджера PredictFun.
Запуск: python run_manager.py [--port 8000]
"""
from __future__ import annotations

import asyncio
import sys
import uvicorn
from manager.app import app


def _parse_port() -> int:
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
        if arg.startswith("--port="):
            return int(arg.split("=", 1)[1])
    return 8000


if __name__ == "__main__":
    port = _parse_port()
    print(f"PredictFun Manager: http://localhost:{port}")
    asyncio.run(
        uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        ).serve()
    )
