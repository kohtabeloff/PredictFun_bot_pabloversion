"""
PredictFun Manager — дашборд для управления несколькими ботами.
Запуск: python run_manager.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
MANAGER_CONFIG = Path(__file__).parent.parent / "manager.json"

app = FastAPI(title="PredictFun Manager")


# ── Конфиг ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if MANAGER_CONFIG.exists():
        return json.loads(MANAGER_CONFIG.read_text(encoding="utf-8"))
    return {"bots": []}


def save_config(data: dict):
    MANAGER_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_bot_url(bot_id: str) -> str:
    cfg = load_config()
    for bot in cfg["bots"]:
        if bot["id"] == bot_id:
            return f"http://localhost:{bot['port']}"
    raise HTTPException(404, f"Бот '{bot_id}' не найден в manager.json")


def _get_bot_cfg(bot_id: str) -> dict:
    cfg = load_config()
    for bot in cfg["bots"]:
        if bot["id"] == bot_id:
            return bot
    raise HTTPException(404, f"Бот '{bot_id}' не найден в manager.json")


def _auth_headers(bot_cfg: dict) -> dict:
    password = bot_cfg.get("password", "")
    if not password:
        return {}
    encoded = base64.b64encode(f"admin:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


# ── Фронтенд ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── API менеджера ─────────────────────────────────────────────────────────────

@app.get("/api/bots")
async def list_bots():
    """Список ботов с live-статусом (баланс, маркеты, ордера)."""
    cfg = load_config()
    result = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        for bot in cfg["bots"]:
            entry = {
                "id": bot["id"],
                "name": bot.get("name", bot["id"]),
                "port": bot["port"],
                "online": False,
                "running": False,
                "balance": None,
                "markets_count": 0,
                "orders_count": 0,
            }
            try:
                r = await client.get(
                    f"http://localhost:{bot['port']}/api/state",
                    headers=_auth_headers(bot),
                )
                if r.status_code == 200:
                    state = r.json()
                    entry["online"] = True
                    entry["running"] = state.get("running", False)
                    entry["balance"] = state.get("balance_usdt")
                    entry["markets_count"] = len(state.get("markets", {}))
                    entry["orders_count"] = state.get("total_open_orders", 0)
            except Exception:
                pass
            result.append(entry)
    return result


@app.post("/api/bots")
async def add_bot(request: Request):
    """Добавить бота в manager.json."""
    body = await request.json()
    bot_id = body.get("id", "").strip()
    name = body.get("name", "").strip()
    port = int(body.get("port", 0))
    password = body.get("password", "").strip()
    if not bot_id or not port:
        raise HTTPException(400, "Нужны id и port")
    cfg = load_config()
    if any(b["id"] == bot_id for b in cfg["bots"]):
        raise HTTPException(409, f"Бот '{bot_id}' уже существует")
    entry: dict = {"id": bot_id, "name": name or bot_id, "port": port}
    if password:
        entry["password"] = password
    cfg["bots"].append(entry)
    save_config(cfg)
    return {"ok": True}


@app.put("/api/bots/{bot_id}/password")
async def set_bot_password(bot_id: str, request: Request):
    """Обновить пароль бота."""
    body = await request.json()
    password = body.get("password", "").strip()
    cfg = load_config()
    for bot in cfg["bots"]:
        if bot["id"] == bot_id:
            if password:
                bot["password"] = password
            else:
                bot.pop("password", None)
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, f"Бот '{bot_id}' не найден")


@app.delete("/api/bots/{bot_id}")
async def remove_bot(bot_id: str):
    """Удалить бота из manager.json (процесс не останавливает)."""
    cfg = load_config()
    before = len(cfg["bots"])
    cfg["bots"] = [b for b in cfg["bots"] if b["id"] != bot_id]
    if len(cfg["bots"]) == before:
        raise HTTPException(404, f"Бот '{bot_id}' не найден")
    save_config(cfg)
    return {"ok": True}


@app.put("/api/bots/{bot_id}/name")
async def rename_bot(bot_id: str, request: Request):
    """Переименовать бота."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Нужно name")
    cfg = load_config()
    for bot in cfg["bots"]:
        if bot["id"] == bot_id:
            bot["name"] = name
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, f"Бот '{bot_id}' не найден")


# ── HTTP прокси ───────────────────────────────────────────────────────────────

@app.api_route(
    "/api/proxy/{bot_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
)
async def proxy_request(bot_id: str, path: str, request: Request):
    """Проксирует любой API-запрос к нужному боту."""
    bot_cfg = _get_bot_cfg(bot_id)
    base_url = f"http://localhost:{bot_cfg['port']}"
    url = f"{base_url}/{path}"
    body = await request.body()
    headers = _auth_headers(bot_cfg)
    if request.headers.get("content-type"):
        headers["content-type"] = request.headers["content-type"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )
    except httpx.ConnectError:
        raise HTTPException(503, f"Бот '{bot_id}' недоступен")


# ── WebSocket прокси ──────────────────────────────────────────────────────────

@app.websocket("/ws/proxy/{bot_id}")
async def ws_proxy(bot_id: str, websocket: WebSocket):
    """Туннелирует WebSocket от браузера к нужному боту."""
    bot_cfg = _get_bot_cfg(bot_id)
    base_url = f"http://localhost:{bot_cfg['port']}"
    ws_url = f"ws://localhost:{bot_cfg['port']}/ws"

    # Если бот с паролем — получаем одноразовый WS-токен
    if bot_cfg.get("password"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{base_url}/api/ws-token",
                    headers=_auth_headers(bot_cfg),
                )
                token = r.json().get("token", "")
            ws_url += f"?token={token}"
        except Exception:
            await websocket.close(code=4001, reason="Не удалось получить WS-токен")
            return

    await websocket.accept()

    try:
        import websockets
        async with websockets.connect(ws_url) as upstream:
            async def client_to_upstream():
                try:
                    async for msg in websocket.iter_text():
                        await upstream.send(msg)
                except (WebSocketDisconnect, Exception):
                    pass

            async def upstream_to_client():
                try:
                    async for msg in upstream:
                        await websocket.send_text(msg)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
