# PredictFun Liquidity Farming Bot

## Назначение
Бот для фарминга ликвидности на PredictFun (prediction market на BNB Chain). Выставляет лимит-ордера в стакан, которые **не должны исполняться** — цель в том, чтобы ордера висели и зарабатывали reward-поинты за предоставление ликвидности.

## Архитектура
- **Backend**: Python 3.11+, asyncio, FastAPI + uvicorn
- **Frontend**: HTML/CSS/JS (single-file), общается с бэкендом через REST API + WebSocket
- **Торговля**: predict_sdk (подпись ордеров на BNB blockchain)
- **Хранение**: JSON файлы (bot_config.json, settings.json)

---

## Структура проекта

```
predictfun-bot/
├── main.py                     # Точка входа
├── config.py                   # Константы и дефолты
├── models.py                   # Pydantic v2 модели данных
├── requirements.txt            # Зависимости
│
├── core/                       # Торговая логика
│   ├── engine.py              # BotEngine — главный координатор
│   ├── market_worker.py       # MarketWorker — один воркер на маркет
│   ├── calculator.py          # Calculator — расчёт цен по кумулятивной ликвидности
│   └── order_manager.py       # OrderManager — подпись и размещение ордеров
│
├── api/                        # Интеграция с PredictFun API
│   ├── client.py              # APIClient — async REST клиент (aiohttp)
│   ├── auth.py                # JWT аутентификация через predict_sdk
│   └── websocket.py           # PredictWebSocket — получение orderbook в реальном времени
│
├── storage/                    # Персистентное хранилище
│   ├── config_store.py        # Глобальные настройки → bot_config.json
│   ├── settings_store.py      # Настройки маркетов → settings.json
│   └── accounts.py            # Загрузка аккаунтов из accounts.txt (legacy)
│
├── utils/
│   └── logger.py              # BotLogger + EventBus (pub/sub для UI)
│
└── web/
    ├── app.py                 # FastAPI приложение (REST + WebSocket)
    └── static/
        └── index.html         # Браузерный UI (~1500 строк)
```

---

## Модели данных (models.py)

| Модель | Назначение |
|--------|-----------|
| **MarketSettings** | Настройки маркета: side (yes/no/both), position_size, target_liquidity, max_auto_spread, min_spread, liquidity_mode, volatile_* |
| **OrderRecord** | Выставленный ордер: order_id, market_id, side, price, shares, placed_at |
| **OrderCalculation** | Результат расчёта: mid_price, best_bid/ask, buy_price/shares, liquidity, can_place, spread |
| **MarketState** | Полное состояние маркета для UI |
| **AccountInfo** | Данные аккаунта: api_key, address, privy_key, proxy |
| **BotState** | Состояние бота: running, ws_connected, balance, markets, total_orders |

---

## Ключевые компоненты

### BotEngine (core/engine.py)
Главный координатор. Управляет жизненным циклом всех компонентов.

**При старте:**
1. Получает JWT через predict_sdk
2. Создаёт APIClient (aiohttp session)
3. Создаёт OrderManager
4. Запускает PredictWebSocket
5. Запускает фоновые задачи: Inspector, ExecutionGuard, BalanceLoop

**Фоновые задачи:**
- **Inspector** (каждые 10 сек): ищет orphan-ордера (остались от краша) и отменяет
- **ExecutionGuard** (каждые 3 сек): проверяет не исполнились ли лимитки → авто-продажа
- **BalanceLoop** (каждые 5 мин): обновляет баланс USDT

### MarketWorker (core/market_worker.py)
Один asyncio.Task на каждый маркет. Получает orderbook из WebSocket очереди.

**Цикл обработки:**
1. Ждёт обновление стакана из очереди
2. Calculator рассчитывает оптимальную цену
3. Проверяет волатильность (cooldown)
4. Решает: поставить / переставить / отменить ордер
5. Отправляет обновление в UI

**Волатильность:** если слишком много переставлений за окно → RETREAT (ордер уходит на max distance от mid, а не паузится)

### Calculator (core/calculator.py)
Чистая логика расчётов без async зависимостей.

**Алгоритм расчёта цены:**
1. Берёт лучший bid/ask → вычисляет mid price
2. **Кумулятивная ликвидность**: суммирует depth от лучшей цены вглубь стакана
3. Находит уровень где cumulative ≥ target_liquidity
4. Ставит ордер на 1 тик ниже этого уровня
5. Ограничивает: `price ≥ mid - max_auto_spread%`
6. Проверяет: `|mid - price| ≥ min_spread`

### OrderManager (core/order_manager.py)
Подпись и размещение ордеров через predict_sdk.

**Ключевые операции:**
- `place_order()` — подпись через predict_sdk (в отдельном потоке), отправка через API
- `cancel_orders()` — пакетная отмена (по 50 штук)
- `atomic_replace()` — `asyncio.gather(cancel, place)` параллельно → минимальный gap
- `sell_market()` — авто-продажа на 1 тик ниже mid price

**Защита от precision ошибок:** после 3 ошибок блокирует сторону на 24 часа.

### PredictWebSocket (api/websocket.py)
Подключается к `wss://ws.predict.fun/ws`, подписывается на orderbook каждого маркета.

- Dispatch: входящий orderbook → очередь нужного воркера
- При переполнении очереди: дропит старое, кладёт новое
- Автореконнект с экспоненциальной задержкой
- Heartbeat

### APIClient (api/client.py)
Единый aiohttp.ClientSession для всего бота.

- Авто-refresh JWT при 401
- Retry на 3 попытки
- Пагинация для get_open_orders()

---

## Веб-интерфейс

### REST API (web/app.py)

| Метод | URL | Функция |
|-------|-----|---------|
| GET | `/api/state` | Полное состояние бота |
| GET | `/api/logs` | Последние логи |
| POST | `/api/markets` | Добавить маркеты |
| DELETE | `/api/markets/{id}` | Удалить маркет |
| POST | `/api/markets/{id}/cancel` | Отменить ордера маркета |
| PUT | `/api/markets/{id}/settings` | Настройки маркета |
| PUT | `/api/markets/settings/bulk` | Настройки для ВСЕХ маркетов |
| GET | `/api/config` | Глобальные настройки бота |
| POST | `/api/config` | Сохранить настройки бота |
| POST | `/api/cancel-all` | Отменить все ордера |
| POST | `/api/bot/start` | Запустить бот |
| POST | `/api/bot/stop` | Остановить бот |
| WS | `/ws` | Real-time обновления |

### UI (web/static/index.html)
- **Header**: статус бота, WS, баланс, кнопки управления
- **Добавление маркетов**: ввод ID через запятую
- **Глобальные настройки**: раскрывающаяся панель с чекбоксами → "Применить ко всем"
- **Карточки маркетов**: цены, объёмы, ликвидность, кнопки ▶/■/⚙/Удалить
- **Индивидуальные настройки**: раскрывающаяся панель на карточке
- **Консоль логов**: правая колонка
- **Модальное окно настроек**: аккаунт, Telegram, пароль UI

### WebSocket события (UI ← Backend)
- `bot_state` — полное состояние
- `market_update` — обновление маркета
- `log` — запись лога
- `balance` — баланс USDT
- `orders_count` — количество ордеров
- `execution_alert` — исполнение лимитки (popup)

---

## Процесс торговли

```
1. Пользователь нажимает СТАРТ → бот подключается к API и WebSocket
2. Добавляет маркеты → создаётся по воркеру на каждый
3. Нажимает ▶ на маркетах → воркеры начинают выставлять ордера
4. WebSocket шлёт обновления стакана → воркер пересчитывает цену
5. Если цена изменилась значимо → atomic_replace (cancel + place параллельно)
6. Если волатильность высокая → RETREAT на безопасное расстояние
7. Inspector каждые 10 сек чистит orphan-ордера
8. ExecutionGuard каждые 3 сек проверяет исполнения → авто-продажа + Telegram alert
```

---

## Конфигурация (config.py)

| Параметр | Значение | Назначение |
|----------|----------|-----------|
| API_BASE_URL | `https://api.predict.fun` | REST API |
| WS_URL | `wss://ws.predict.fun/ws` | WebSocket |
| DEFAULT_POSITION_SIZE_USDT | 100.0 | Размер позиции |
| DEFAULT_MIN_SPREAD | 0.2 | Мин спред (центы) |
| DEFAULT_TARGET_LIQUIDITY | 1000.0 | Целевая ликвидность ($) |
| DEFAULT_MAX_AUTO_SPREAD | 6.0 | Макс авто-спред (%) |
| INSPECTOR_INTERVAL_SEC | 10 | Интервал инспектора |
| EXECUTION_GUARD_INTERVAL_SEC | 3 | Интервал проверки исполнений |
| WEB_PORT | 8080 | Порт UI |

---

## Зависимости

```
fastapi>=0.110.0          # REST + WebSocket сервер
uvicorn[standard]>=0.29.0 # ASGI сервер
websockets>=12.0          # WebSocket клиент
aiohttp>=3.9.0            # Async HTTP клиент
pydantic>=2.6.0           # Валидация данных
requests>=2.31.0          # Sync HTTP (для auth)
predict_sdk               # SDK PredictFun (подпись ордеров, баланс)
```

---

## Хранение данных

- **bot_config.json**: аккаунт (api_key, address, privy_key, proxy), Telegram (token, chat_id), пароль UI
- **settings.json**: настройки каждого маркета (side, position_size, target_liquidity, etc.)
- **accounts.txt**: legacy формат аккаунтов (`api_key,address,privy_key,proxy`)
- **logs/session_*.log**: логи сессий
