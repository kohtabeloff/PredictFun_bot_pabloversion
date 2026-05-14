"""
FastAPI приложение: REST API + WebSocket для UI.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import secrets

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# BotEngine подключается снаружи через app.state.engine
app = FastAPI(title="PredictFun Bot")

STATIC_DIR = Path(__file__).parent / "static"
_MANAGER_SECRET_FILE = Path(__file__).parent.parent / ".manager_proxy_secret"


def _get_manager_secret() -> str:
    secret = os.environ.get("MANAGER_PROXY_SECRET", "")
    if not secret and _MANAGER_SECRET_FILE.exists():
        try:
            secret = _MANAGER_SECRET_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return secret


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """HTTP Basic Auth — если ui_password задан в настройках."""
    if request.url.path == "/api/ping":
        return await call_next(request)

    cfg = getattr(request.app.state, "config_store", None)
    password = cfg.get().get("ui_password", "") if cfg else ""

    if password:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                _, pwd = decoded.split(":", 1)
                if not secrets.compare_digest(pwd.encode(), password.encode()):
                    return Response("Неверный пароль", status_code=401,
                                    headers={"WWW-Authenticate": "Basic realm=\"PredictFun Bot\""})
            except Exception:
                return Response("Ошибка авторизации", status_code=401,
                                headers={"WWW-Authenticate": "Basic realm=\"PredictFun Bot\""})
        else:
            return Response("Требуется авторизация", status_code=401,
                            headers={"WWW-Authenticate": "Basic realm=\"PredictFun Bot\""})

        if request.method in ("POST", "PUT", "DELETE"):
            host = request.headers.get("host", "")
            expected = f"{request.url.scheme}://{host}"
            origin = request.headers.get("origin", "")
            referer = request.headers.get("referer", "")
            # Берём origin, если есть. Иначе — hostname из referer
            source = origin or (referer.split("/")[0] + "//" + referer.split("/")[2] if "//" in referer else "")
            if not source or source.rstrip("/") != expected.rstrip("/"):
                return Response("Forbidden: Origin mismatch", status_code=403)

    return await call_next(request)


# ── Раздача фронтенда ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── REST API ──────────────────────────────────────────────────────────────

@app.get("/api/ping")
async def manager_ping(request: Request):
    """Проверка что это наш бот: менеджер отправляет секрет, бот его подтверждает."""
    expected = _get_manager_secret()
    provided = request.headers.get("x-manager-secret", "")
    if not expected or not secrets.compare_digest(provided, expected):
        raise HTTPException(403, "Invalid manager secret")
    return Response("pong", media_type="text/plain")


@app.get("/api/state")
async def get_state():
    engine = app.state.engine
    return engine.get_state().model_dump()


@app.get("/api/logs")
async def get_logs(n: int = 100):
    logger = app.state.logger
    return {"logs": logger.get_recent(n)}


class AddMarketsRequest(BaseModel):
    market_ids: list[str]


@app.post("/api/markets")
async def add_markets(req: AddMarketsRequest):
    engine = app.state.engine
    if not engine.running:
        raise HTTPException(400, "Бот не запущен")
    results = await engine.add_markets(req.market_ids, force_disabled=True)
    return {"results": results}


@app.delete("/api/markets/{market_id}")
async def remove_market(market_id: str):
    engine = app.state.engine
    ok = await engine.remove_market(market_id)
    if not ok:
        raise HTTPException(409, "Не удалось отменить ордера на бирже — маркет остаётся под управлением")
    return {"ok": True}


@app.post("/api/markets/{market_id}/cancel")
async def cancel_market_orders(market_id: str):
    """Отменяет все ордера маркета, не удаляя его."""
    engine = app.state.engine
    worker = engine._workers.get(market_id)
    if worker:
        ids = worker.get_active_order_ids()
        if ids and engine.order_manager:
            ok = await engine.order_manager.cancel_orders(ids, market_id=market_id)
            if ok:
                worker.order_yes = None
                worker.order_no = None
            else:
                engine._broadcast_state()
                return {"ok": False, "error": "Не удалось отменить ордера на бирже"}
        else:
            worker.order_yes = None
            worker.order_no = None
        engine._broadcast_state()
    return {"ok": True}


class UpdateSettingsRequest(BaseModel):
    enabled: bool | None = None
    side: str | None = None
    position_size_usdt: float | None = None
    position_size_shares: float | None = None
    min_spread: float | None = None
    target_liquidity: float | None = None
    max_auto_spread: float | None = None
    liquidity_mode: str | None = None
    volatile_reposition_limit: int | None = None
    volatile_window_seconds: float | None = None
    volatile_cooldown_seconds: float | None = None
    min_orders_before: int | None = None


@app.put("/api/markets/{market_id}/settings")
async def update_settings(market_id: str, req: UpdateSettingsRequest):
    engine = app.state.engine
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Нет параметров для обновления")
    settings = engine.update_market_settings(market_id, **updates)
    return settings.model_dump()


@app.put("/api/markets/settings/bulk")
async def bulk_update_settings(req: UpdateSettingsRequest):
    """Применить настройки ко ВСЕМ загруженным маркетам."""
    engine = app.state.engine
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "Нет параметров для обновления")
    results = {}
    # Берём все известные маркеты: и активные воркеры (бот запущен), и из settings_store (бот остановлен)
    all_market_ids = set(engine._workers.keys()) | set(engine.settings_store.all().keys())
    for market_id in all_market_ids:
        s = engine.update_market_settings(market_id, **updates)
        results[market_id] = s.model_dump()
    # Сохраняем как дефолтные — применятся к маркетам, добавленным позже
    engine.set_global_defaults(**updates)
    return {"updated": len(results), "results": results}


class BotConfigRequest(BaseModel):
    api_key: str | None = None
    predict_account_address: str | None = None
    privy_wallet_private_key: str | None = None
    proxy: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    ui_password: str | None = None
    predict_points_only: bool | None = None
    predict_points_poll_sec: int | None = None  # 60–7200
    auto_sell_enabled: bool | None = None
    auto_sell_max_loss_pct: float | None = None  # 0.1–95.0
    auto_sell_delay_sec: float | None = None     # 0–3600


@app.get("/api/config")
async def get_config():
    store = app.state.config_store
    data = store.get()
    result = dict(data)
    # Для чувствительных полей возвращаем только флаг установлен/нет, само значение не отдаём
    for secret_field in ("privy_wallet_private_key", "api_key", "telegram_token"):
        val = result.get(secret_field, "")
        result[f"{secret_field}_set"] = bool(val)
        result[secret_field] = ""  # никогда не отдаём содержимое, даже hint
    result["ui_password_set"] = bool(result.pop("ui_password", ""))
    return result


@app.post("/api/config")
async def save_config(req: BotConfigRequest):
    store = app.state.config_store
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if "privy_wallet_private_key" in updates and updates["privy_wallet_private_key"] == "":
        del updates["privy_wallet_private_key"]
    # Зажимаем значения ДО сохранения — иначе после рестарта clamp не применяется
    if "predict_points_poll_sec" in updates:
        updates["predict_points_poll_sec"] = max(60, min(7200, int(updates["predict_points_poll_sec"])))
    if "auto_sell_max_loss_pct" in updates:
        updates["auto_sell_max_loss_pct"] = max(0.1, min(95.0, float(updates["auto_sell_max_loss_pct"])))
    if "auto_sell_delay_sec" in updates:
        updates["auto_sell_delay_sec"] = max(0.0, min(3600.0, float(updates["auto_sell_delay_sec"])))
    data = store.update(**updates)
    import config as cfg
    cfg.TELEGRAM_TOKEN = data.get("telegram_token", "")
    cfg.TELEGRAM_CHAT_ID = data.get("telegram_chat_id", "")
    cfg.POINTS_FILTER_ENABLED = bool(data.get("predict_points_only", False))
    cfg.POINTS_POLL_INTERVAL_SEC = int(data.get("predict_points_poll_sec", 600))
    cfg.AUTO_SELL_ENABLED = bool(data.get("auto_sell_enabled", False))
    cfg.AUTO_SELL_MAX_LOSS_PCT = float(data.get("auto_sell_max_loss_pct", 15.0))
    cfg.AUTO_SELL_DELAY_SEC = float(data.get("auto_sell_delay_sec", 0.0))
    return {"ok": True, "restart_required": any(
        k in updates for k in ("api_key", "predict_account_address", "privy_wallet_private_key", "proxy")
    )}


@app.post("/api/cancel-all")
async def cancel_all():
    engine = app.state.engine
    failed = await engine.cancel_all()
    if failed:
        return {"ok": False, "failed_markets": failed}
    return {"ok": True}


@app.delete("/api/markets")
async def remove_all_markets():
    """Удаляет все маркеты: отменяет ордера, останавливает воркеры, чистит настройки."""
    engine = app.state.engine
    failed = []
    for mid in list(engine._workers.keys()):
        ok = await engine.remove_market(mid)
        if not ok:
            failed.append(mid)
    if failed:
        raise HTTPException(409, f"Не удалось отменить ордера для маркетов: {', '.join(failed)}")
    # Чистим settings.json — в том числе если бот остановлен и воркеров не было
    for mid in list(engine.settings_store.all().keys()):
        engine.settings_store.remove(mid)
    return {"ok": True}


@app.get("/api/markets/export")
async def export_markets():
    """
    Возвращает JSON со всеми рынками и их настройками.
    Формат: {"version": 1, "markets": [{market_id, ...settings}]}
    """
    engine = app.state.engine
    all_settings = engine.settings_store.all()
    markets = []
    for mid, s in all_settings.items():
        entry = s.model_dump()
        markets.append(entry)
    return {"version": 1, "markets": markets}


class ImportMarketsRequest(BaseModel):
    markets: list[dict]
    apply_settings: bool = True


@app.post("/api/markets/import")
async def import_markets(req: ImportMarketsRequest):
    """
    Загружает рынки из экспортированного JSON.
    apply_settings=True — применяет настройки из файла.
    apply_settings=False — загружает только market_id, настройки игнорирует.
    """
    engine = app.state.engine
    if not engine.running:
        raise HTTPException(400, "Бот не запущен — сначала запусти бот")

    results = {}
    market_ids = []
    settings_by_id: dict[str, dict] = {}

    for entry in req.markets:
        mid = str(entry.get("market_id") or entry.get("id") or "").strip()
        if not mid:
            continue
        market_ids.append(mid)
        if req.apply_settings:
            settings_by_id[mid] = entry

    if not market_ids:
        raise HTTPException(400, "Нет валидных market_id в запросе")

    # Применяем настройки до добавления (чтобы новые маркеты стартовали с нужными настройками)
    if req.apply_settings:
        for mid, entry in settings_by_id.items():
            allowed_fields = {
                "enabled", "side", "position_size_usdt", "position_size_shares",
                "min_spread", "target_liquidity", "max_auto_spread", "liquidity_mode",
                "volatile_reposition_limit", "volatile_window_seconds",
                "volatile_cooldown_seconds", "min_orders_before",
            }
            updates = {k: v for k, v in entry.items() if k in allowed_fields and v is not None}
            if updates:
                engine.settings_store.update(mid, **updates)

    add_results = await engine.add_markets(market_ids, force_disabled=not req.apply_settings)
    for mid, res in add_results.items():
        results[mid] = res

    # Откатываем settings для маркетов, которые не загрузились — иначе они
    # будут пытаться автостартовать при следующем запуске бота с чужими настройками
    if req.apply_settings:
        for mid, res in add_results.items():
            if "error" in str(res) and mid in settings_by_id:
                engine.settings_store.remove(mid)

    ok = sum(1 for v in results.values() if v in ("ok", "already_exists"))
    errors = {mid: v for mid, v in results.items() if "error" in str(v)}
    return {"ok": True, "loaded": ok, "total": len(market_ids), "errors": errors}


async def _restore_saved_markets(engine, force_disabled: bool = False):
    """Фоновая задача: восстанавливает маркеты из settings.json после старта бота.
    force_disabled=True — маркеты загружаются в выключенном состоянии (ручной запуск).
    force_disabled=False — восстанавливается сохранённый enabled (автоперезапуск).
    """
    saved = engine.settings_store.all()
    if not saved:
        return
    ids = list(saved.keys())
    engine.logger.log(f"Восстановление {len(ids)} маркетов из settings.json...")
    results = await engine.add_markets(ids, force_disabled=force_disabled)
    ok = sum(1 for v in results.values() if v in ("ok", "already_exists"))
    err = [mid for mid, v in results.items() if "error" in str(v)]
    engine.logger.log(f"Восстановлено {ok} маркетов" + (f", ошибки: {', '.join(err)}" if err else ""))


@app.post("/api/bot/start")
async def bot_start():
    engine = app.state.engine
    if engine._state != "stopped":
        raise HTTPException(400, f"Бот в состоянии '{engine._state}', нельзя запустить")
    # Если аккаунт — заглушка, подгружаем актуальный из config_store
    cfg = app.state.config_store.get()
    addr = cfg.get("predict_account_address", "")
    key  = cfg.get("privy_wallet_private_key", "")
    if addr and addr != "0x0000000000000000000000000000000000000000" and key:
        from models import AccountInfo
        engine.account = AccountInfo(
            api_key=cfg.get("api_key", ""),
            predict_account_address=addr,
            privy_wallet_private_key=key,
            proxy=cfg.get("proxy") or None,
        )
    elif not engine.account.api_key:
        raise HTTPException(400, "Настрой аккаунт в ⚙ Настройки перед запуском")
    try:
        await engine.start()
    except Exception as e:
        raise HTTPException(500, f"Ошибка запуска: {e}")
    # Ручной старт: грузим маркеты выключенными, чтобы ордера не начали
    # выставляться до того как пользователь проверит список и включит нужные.
    asyncio.create_task(_restore_saved_markets(engine, force_disabled=True))
    return {"ok": True}


@app.post("/api/bot/stop")
async def bot_stop():
    engine = app.state.engine
    if engine._state != "running":
        raise HTTPException(400, f"Бот в состоянии '{engine._state}', нельзя остановить")
    await engine.stop()
    return {"ok": True}


# Хранилище одноразовых WS-токенов (token -> True)
_ws_tokens: dict[str, float] = {}
_WS_TOKEN_TTL = 30  # секунд


@app.get("/api/ws-token")
async def ws_token():
    """Выдаёт одноразовый короткоживущий токен для WebSocket (запрос уже прошёл Basic Auth)."""
    import time
    # Чистим просроченные
    now = time.time()
    expired = [t for t, ts in _ws_tokens.items() if now - ts > _WS_TOKEN_TTL]
    for t in expired:
        _ws_tokens.pop(t, None)
    # Генерируем новый
    token = secrets.token_urlsafe(32)
    _ws_tokens[token] = now
    return {"token": token}


# ── WebSocket ────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Проверяем одноразовый WS-токен
    cfg = getattr(app.state, "config_store", None)
    password = cfg.get().get("ui_password", "") if cfg else ""
    if password:
        import time
        token = websocket.query_params.get("token", "")
        issued_at = _ws_tokens.pop(token, None)  # одноразовый — сразу удаляем
        if issued_at is None or (time.time() - issued_at) > _WS_TOKEN_TTL:
            await websocket.close(code=4001, reason="Unauthorized")
            return

    await websocket.accept()
    event_bus = app.state.event_bus
    logger = app.state.logger
    queue = event_bus.subscribe()

    # Отдаём начальное состояние
    try:
        engine = app.state.engine
        await websocket.send_json({"type": "bot_state", "data": engine.get_state().model_dump()})
        await websocket.send_json({"type": "logs", "logs": logger.get_recent(50)})
    except Exception:
        pass

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Нет событий 30 сек — шлём ping чтобы проверить что клиент жив
                await websocket.send_json({"type": "ping"})
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        event_bus.unsubscribe(queue)
