"""
PredictFun Manager — дашборд для управления несколькими ботами.
Запуск: python run_manager.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from manager import parser_runner as pr

STATIC_DIR = Path(__file__).parent / "static"
MANAGER_CONFIG = Path(__file__).parent.parent / "manager.json"
MANAGER_KEY_FILE = MANAGER_CONFIG.parent / ".manager_key"
MANAGER_SECRET_FILE = MANAGER_CONFIG.parent / ".manager_proxy_secret"
MAIN_PY = MANAGER_CONFIG.parent / "main.py"
ACCOUNTS_DIR = MANAGER_CONFIG.parent / "accounts"

# Словарь запущенных менеджером процессов: bot_id → Popen
_processes: dict[str, subprocess.Popen] = {}

# Одноразовый токен для первичной настройки пароля
_setup_token: str | None = None

_BOT_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')

_parser_running = False


# ── Конфиг ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if MANAGER_CONFIG.exists():
        return json.loads(MANAGER_CONFIG.read_text(encoding="utf-8"))
    return {"bots": []}


def save_config(data: dict):
    import tempfile
    tmp = MANAGER_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MANAGER_CONFIG)


# ── Шифрование паролей ботов (Fernet, симметричный ключ) ─────────────────────

def _get_fernet() -> Fernet:
    if not MANAGER_KEY_FILE.exists():
        key = Fernet.generate_key()
        MANAGER_KEY_FILE.write_bytes(key)
        MANAGER_KEY_FILE.chmod(0o600)
    else:
        key = MANAGER_KEY_FILE.read_bytes()
    return Fernet(key)


def encrypt_password(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_password(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()


_FERNET_PREFIX = "fernet:"


# ── Прокси-секрет (общий с ботами) ───────────────────────────────────────────

def _get_or_create_proxy_secret() -> str:
    if not MANAGER_SECRET_FILE.exists():
        secret = secrets.token_urlsafe(32)
        MANAGER_SECRET_FILE.write_text(secret, encoding="utf-8")
        MANAGER_SECRET_FILE.chmod(0o600)
    return MANAGER_SECRET_FILE.read_text(encoding="utf-8").strip()


# ── Password hashing (PBKDF2-HMAC-SHA256, встроен в Python) ──────────────────

_PBKDF2_PREFIX = "pbkdf2sha256:"


def hash_password(plain: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260_000)
    return f"{_PBKDF2_PREFIX}{salt}:{dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    if stored.startswith(_PBKDF2_PREFIX):
        rest = stored[len(_PBKDF2_PREFIX):]
        salt, dk_hex = rest.split(":", 1)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt.encode(), 260_000)
        return secrets.compare_digest(dk.hex(), dk_hex)
    # Обратная совместимость: старые plaintext-пароли до миграции
    return secrets.compare_digest(plain, stored)


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
    stored = bot_cfg.get("password", "")
    if not stored:
        return {}
    plain = decrypt_password(stored[len(_FERNET_PREFIX):]) if stored.startswith(_FERNET_PREFIX) else stored
    encoded = base64.b64encode(f"admin:{plain}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


# ── Управление процессами ─────────────────────────────────────────────────────

def _bot_data_dir(bot: dict) -> Path:
    stored = bot.get("data_dir")
    if stored:
        return Path(stored)
    return ACCOUNTS_DIR / bot["id"]


def _launch_process(bot: dict) -> subprocess.Popen:
    data_dir = _bot_data_dir(bot)
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(MAIN_PY),
        "--port", str(bot["port"]),
        "--data-dir", str(data_dir),
    ]
    env = os.environ.copy()
    env["MANAGER_PROXY_SECRET"] = _get_or_create_proxy_secret()
    return subprocess.Popen(cmd, cwd=str(MAIN_PY.parent), env=env)


async def _is_bot_online(port: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"http://localhost:{port}/api/state")
            return r.status_code == 200
    except Exception:
        return False


# ── Lifespan: авто-старт управляемых ботов ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _setup_token
    cfg = load_config()
    if not cfg.get("manager_password", "").strip():
        _setup_token = secrets.token_urlsafe(16)
        print("\n" + "=" * 60)
        print(f"  SETUP TOKEN: {_setup_token}")
        print("  Введите этот токен на странице настройки менеджера.")
        print("=" * 60 + "\n")
    # Миграция: шифруем plaintext пароли ботов если есть
    _migrate_changed = False
    for bot in cfg["bots"]:
        pw = bot.get("password", "")
        if pw and not pw.startswith(_FERNET_PREFIX):
            try:
                bot["password"] = _FERNET_PREFIX + encrypt_password(pw)
                _migrate_changed = True
            except Exception as e:
                print(f"[Manager] Не удалось зашифровать пароль бота {bot['id']}: {e}")
    if _migrate_changed:
        save_config(cfg)
        print("[Manager] Пароли ботов зашифрованы.")

    for bot in cfg["bots"]:
        if not bot.get("managed"):
            continue
        online = await _is_bot_online(bot["port"])
        if online:
            # Процесс уже работает (выжил после перезапуска менеджера)
            continue
        try:
            proc = _launch_process(bot)
            _processes[bot["id"]] = proc
            print(f"[Manager] Запущен бот {bot['id']} на порту {bot['port']}")
        except Exception as e:
            print(f"[Manager] Не удалось запустить бот {bot['id']}: {e}")
    yield
    # При остановке менеджера процессы ботов продолжают работать


app = FastAPI(title="PredictFun Manager", lifespan=lifespan)


# ── Auth middleware ───────────────────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    cfg = load_config()
    password = cfg.get("manager_password", "").strip()
    client_host = (request.client.host if request.client else "") or ""
    is_local = client_host in ("127.0.0.1", "::1", "localhost")

    def _check_basic(headers) -> bool:
        auth = headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
            _, pwd = decoded.split(":", 1)
            return verify_password(pwd, password)
        except Exception:
            return False

    if not password:
        # Пароль не задан — показываем страницу первичной настройки всем.
        # Разрешаем только GET / (сама страница) и PUT /api/manager/password
        # (чтобы форма могла сохранить пароль). Всё остальное блокируем.
        path = request.url.path
        if path == "/" and request.method == "GET":
            return await call_next(request)
        if path == "/api/manager/password" and request.method == "PUT":
            return await call_next(request)
        # Локальные процессы тоже не должны менять состояние без пароля
        return Response(
            "Сначала установите пароль менеджера.",
            status_code=403,
            media_type="text/plain; charset=utf-8",
        )

    if _check_basic(request.headers):
        if request.method in ("POST", "PUT", "DELETE"):
            host = request.headers.get("host", "")
            expected = f"{request.url.scheme}://{host}"
            origin = request.headers.get("origin", "")
            referer = request.headers.get("referer", "")
            source = origin or (referer.split("/")[0] + "//" + referer.split("/")[2] if "//" in referer else "")
            if not source or source.rstrip("/") != expected.rstrip("/"):
                return Response("Forbidden: Origin mismatch", status_code=403)
        return await call_next(request)

    return Response(
        "Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="PredictFun Manager"'},
    )


# ── Фронтенд ─────────────────────────────────────────────────────────────────

_SETUP_PAGE = """\
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Настройка менеджера</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0f1117; color: #e2e8f0; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: #1a1d27; border: 1px solid #2d3148; border-radius: 12px;
            padding: 40px; width: 100%; max-width: 380px; }
    h1 { font-size: 20px; margin-bottom: 8px; }
    p  { font-size: 13px; color: #8892a4; margin-bottom: 24px; line-height: 1.5; }
    label { display: block; font-size: 12px; color: #8892a4; margin-bottom: 6px; }
    input { width: 100%; background: #0f1117; border: 1px solid #2d3148; border-radius: 8px;
            color: #e2e8f0; padding: 10px 12px; font-size: 14px; margin-bottom: 16px; outline: none; }
    input:focus { border-color: #4f6ef7; }
    button { width: 100%; background: #4f6ef7; border: none; border-radius: 8px;
             color: #fff; padding: 11px; font-size: 14px; font-weight: 600; cursor: pointer; }
    button:hover { background: #3d5ce0; }
    .err { color: #f87171; font-size: 13px; margin-top: 12px; display: none; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Добро пожаловать</h1>
    <p>Введите токен из консоли сервера, затем придумайте пароль для входа в менеджер.</p>
    <label>Токен из консоли сервера</label>
    <input id="tok" type="text" placeholder="Токен из терминала" autocomplete="off" />
    <label>Новый пароль</label>
    <input id="p1" type="password" placeholder="Минимум 6 символов" />
    <label>Повторите пароль</label>
    <input id="p2" type="password" placeholder="Повторите пароль" />
    <button onclick="setup()">Установить пароль и войти</button>
    <div class="err" id="err"></div>
  </div>
  <script>
    document.getElementById('p2').addEventListener('keydown', e => { if (e.key === 'Enter') setup(); });
    async function setup() {
      const tok = document.getElementById('tok').value.trim();
      const p1 = document.getElementById('p1').value;
      const p2 = document.getElementById('p2').value;
      const err = document.getElementById('err');
      err.style.display = 'none';
      if (!tok) { err.textContent = 'Введите токен из консоли сервера'; err.style.display = 'block'; return; }
      if (p1.length < 6) { err.textContent = 'Пароль слишком короткий (минимум 6 символов)'; err.style.display = 'block'; return; }
      if (p1 !== p2) { err.textContent = 'Пароли не совпадают'; err.style.display = 'block'; return; }
      const r = await fetch('/api/manager/password', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ current_password: '', new_password: p1, setup_token: tok })
      });
      if (r.ok) {
        window.location.reload();
      } else {
        const d = await r.json().catch(() => ({}));
        err.textContent = 'Ошибка: ' + (d.detail || r.status);
        err.style.display = 'block';
      }
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    cfg = load_config()
    if not cfg.get("manager_password", "").strip():
        return HTMLResponse(_SETUP_PAGE)
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


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
                "managed": bool(bot.get("managed")),
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


@app.post("/api/bots/create")
async def create_bot(request: Request):
    """Создать нового бота: выбирает порт, создаёт папку, запускает процесс."""
    body = await request.json()
    cfg = load_config()

    # Авто-выбор порта
    used_ports = {b["port"] for b in cfg["bots"]}
    port = 8081
    while port in used_ports:
        port += 1

    # Авто-выбор ID
    existing_ids = {b["id"] for b in cfg["bots"]}
    idx = 1
    while f"bot{idx}" in existing_ids:
        idx += 1
    bot_id = f"bot{idx}"

    name = (body.get("name") or "").strip() or f"Аккаунт {idx}"
    data_dir = ACCOUNTS_DIR / bot_id

    entry: dict = {
        "id": bot_id,
        "name": name,
        "port": port,
        "managed": True,
        "data_dir": str(data_dir),
    }
    cfg["bots"].append(entry)
    save_config(cfg)

    try:
        proc = _launch_process(entry)
        _processes[bot_id] = proc
    except Exception as e:
        cfg["bots"] = [b for b in cfg["bots"] if b["id"] != bot_id]
        save_config(cfg)
        raise HTTPException(500, f"Не удалось запустить бот: {e}")

    return {"ok": True, "id": bot_id, "port": port, "name": name}


@app.post("/api/bots/{bot_id}/launch")
async def launch_bot(bot_id: str):
    """Запустить процесс управляемого бота (если он упал или не запущен)."""
    bot = _get_bot_cfg(bot_id)
    if not bot.get("managed"):
        raise HTTPException(400, "Бот не управляется менеджером")
    proc = _processes.get(bot_id)
    if proc and proc.poll() is None:
        raise HTTPException(409, "Процесс уже запущен")
    proc = _launch_process(bot)
    _processes[bot_id] = proc
    return {"ok": True}


@app.post("/api/bots")
async def add_bot(request: Request):
    """Добавить существующего бота в manager.json (без запуска процесса)."""
    body = await request.json()
    bot_id = body.get("id", "").strip()
    name = body.get("name", "").strip()
    port = int(body.get("port", 0))
    password = body.get("password", "").strip()
    if not bot_id or not port:
        raise HTTPException(400, "Нужны id и port")
    if not _BOT_ID_RE.match(bot_id):
        raise HTTPException(400, "ID может содержать только буквы, цифры, '-' и '_' (макс. 64 символа)")
    if port < 1024 or port > 65535:
        raise HTTPException(400, "Порт должен быть в диапазоне 1024–65535")
    cfg = load_config()
    if any(b["id"] == bot_id for b in cfg["bots"]):
        raise HTTPException(409, f"Бот '{bot_id}' уже существует")
    try:
        proxy_secret = _get_or_create_proxy_secret()
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"http://localhost:{port}/api/ping",
                headers={"X-Manager-Secret": proxy_secret},
            )
            if r.status_code != 200 or r.text.strip() != "pong":
                raise HTTPException(400, f"Порт {port} не отвечает как бот PredictFun")
    except HTTPException:
        raise
    except httpx.ConnectError:
        raise HTTPException(400, f"Не удалось подключиться к порту {port} — бот не запущен?")
    except Exception:
        raise HTTPException(400, f"Порт {port} не отвечает — бот не запущен?")
    entry: dict = {"id": bot_id, "name": name or bot_id, "port": port}
    if password:
        entry["password"] = _FERNET_PREFIX + encrypt_password(password)
    cfg["bots"].append(entry)
    save_config(cfg)
    return {"ok": True}


@app.put("/api/manager/password")
async def set_manager_password(request: Request):
    """Установить или сменить пароль менеджера."""
    global _setup_token
    body = await request.json()
    new_password = body.get("new_password", "").strip()
    current_password = body.get("current_password", "").strip()
    cfg = load_config()
    existing = cfg.get("manager_password", "").strip()
    if not existing:
        provided_token = body.get("setup_token", "").strip()
        if not _setup_token or not secrets.compare_digest(provided_token, _setup_token):
            raise HTTPException(403, "Неверный токен настройки")
    elif not verify_password(current_password, existing):
        raise HTTPException(403, "Неверный текущий пароль")
    if new_password:
        cfg["manager_password"] = hash_password(new_password)
        _setup_token = None
    else:
        cfg.pop("manager_password", None)
        _setup_token = secrets.token_urlsafe(16)
        print("\n" + "=" * 60)
        print(f"  SETUP TOKEN: {_setup_token}")
        print("  Пароль менеджера сброшен. Используйте токен для установки нового.")
        print("=" * 60 + "\n")
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
                bot["password"] = _FERNET_PREFIX + encrypt_password(password)
            else:
                bot.pop("password", None)
            save_config(cfg)
            return {"ok": True}
    raise HTTPException(404, f"Бот '{bot_id}' не найден")


@app.delete("/api/bots/{bot_id}")
async def remove_bot(bot_id: str):
    """Удалить бота из менеджера. Для управляемых ботов — останавливает процесс."""
    cfg = load_config()
    bot = next((b for b in cfg["bots"] if b["id"] == bot_id), None)
    if not bot:
        raise HTTPException(404, f"Бот '{bot_id}' не найден")

    if bot.get("managed"):
        proc = _processes.pop(bot_id, None)
        if proc and proc.poll() is None:
            proc.terminate()

    cfg["bots"] = [b for b in cfg["bots"] if b["id"] != bot_id]
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
    timeout = 300.0 if path == "api/markets" and request.method == "POST" else 30.0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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

# ── Парсер маркетов ───────────────────────────────────────────────────────────

@app.get("/api/parser/tags")
async def parser_tags():
    cfg = load_config()
    api_key = cfg.get("parser_api_key", "").strip()
    if api_key:
        tags = await asyncio.get_running_loop().run_in_executor(None, pr.fetch_tags, api_key)
    else:
        tags = pr.TAGS_FALLBACK
    return tags


@app.get("/api/parser/config")
async def parser_config():
    cfg = load_config()
    key = cfg.get("parser_api_key", "")
    return {"has_key": bool(key)}


@app.put("/api/parser/config")
async def save_parser_config(request: Request):
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    cfg = load_config()
    cfg["parser_api_key"] = api_key
    save_config(cfg)
    return {"ok": True}


@app.post("/api/parser/run")
async def run_parser(request: Request):
    global _parser_running
    if _parser_running:
        raise HTTPException(429, "Парсер уже запущен, дождитесь завершения")
    _parser_running = True  # устанавливаем до первого await — нет гонки в asyncio
    try:
        body = await request.json()
        cfg = load_config()
        api_key = (body.get("api_key") or cfg.get("parser_api_key", "")).strip()
        if not api_key:
            raise HTTPException(400, "API ключ не задан")

        use_all = bool(body.get("use_all_markets", True))
        raw_ids = body.get("market_ids") or []
        try:
            market_ids_input = [int(x) for x in raw_ids if str(x).strip()]
        except ValueError:
            raise HTTPException(400, "Некорректные market IDs")
    except HTTPException:
        _parser_running = False
        raise

    exclude_tag_ids: list[str] = body.get("exclude_tag_ids") or []
    exclude_tag_names: list[str] = body.get("exclude_tag_names") or []
    require_status: str | None = body.get("require_status") or None
    raw_days = body.get("min_days")
    min_days: int | None = int(raw_days) if raw_days else None
    use_kalshi: bool = bool(body.get("use_kalshi", False))

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def step_cb(step: int, status: str, detail: str):
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"type": "step", "step": step, "status": status, "detail": detail},
        )

    future = loop.run_in_executor(
        None,
        lambda: pr.run_pipeline(
            api_key=api_key,
            use_all_markets=use_all,
            market_ids_input=market_ids_input if not use_all else None,
            exclude_tag_ids=exclude_tag_ids,
            exclude_tag_names=exclude_tag_names,
            require_status=require_status,
            min_days=min_days,
            use_kalshi=use_kalshi,
            step_callback=step_cb,
        ),
    )

    async def generate():
        global _parser_running
        try:
            while True:
                done = future.done()
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    if done:
                        break
                    yield ": keepalive\n\n"

            result, error = future.result()
            yield f"data: {json.dumps({'type': 'done', 'ids': result, 'error': error})}\n\n"
        finally:
            _parser_running = False

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.websocket("/ws/proxy/{bot_id}")
async def ws_proxy(bot_id: str, websocket: WebSocket):
    """Туннелирует WebSocket от браузера к нужному боту."""
    # @app.middleware("http") не перехватывает WebSocket — проверяем вручную.
    mgr_cfg = load_config()
    mgr_password = mgr_cfg.get("manager_password", "").strip()
    ws_client_host = (websocket.client.host if websocket.client else "") or ""
    ws_is_local = ws_client_host in ("127.0.0.1", "::1", "localhost")

    origin = websocket.headers.get("origin", "")
    if origin:
        host = websocket.headers.get("host", "")
        scheme = "wss" if websocket.url.scheme == "wss" else "ws"
        http_scheme = "https" if scheme == "wss" else "http"
        expected = f"{http_scheme}://{host}"
        if origin.rstrip("/") != expected.rstrip("/"):
            await websocket.close(code=4003, reason="Origin mismatch")
            return

    if mgr_password:
        auth = websocket.headers.get("authorization", "")
        authorized = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
                _, pwd = decoded.split(":", 1)
                authorized = verify_password(pwd, mgr_password)
            except Exception:
                pass
        if not authorized:
            await websocket.close(code=4003, reason="Unauthorized")
            return
    elif not ws_is_local:
        # Пароль не задан — WS разрешён только с localhost
        await websocket.close(code=4003, reason="Manager has no password — localhost only")
        return

    bot_cfg = _get_bot_cfg(bot_id)
    base_url = f"http://localhost:{bot_cfg['port']}"
    ws_url = f"ws://localhost:{bot_cfg['port']}/ws"

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
        import websockets  # noqa: PLC0415
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
