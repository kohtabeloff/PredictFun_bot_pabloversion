# PredictFun Liquidity Farming Bot

Бот для фарминга ликвидности на [predict.fun](https://predict.fun). Выставляет лимитные ордера в стакан, зарабатывая очки ликвидности. Управляется через браузер.

---

## Требования

- Python 3.11+
- `predict_sdk` (установи отдельно, инструкция от predict.fun)
- Аккаунт на predict.fun с API ключом

---

## Установка

```bash
git clone https://github.com/kohtabeloff/PredictFun_bot_pabloversion.git
cd PredictFun_bot_pabloversion

# Создать виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# Установить зависимости
pip install -r requirements.txt
pip install predict_sdk  # или по инструкции от predict.fun
```

---

## Запуск

```bash
source .venv/bin/activate
python main.py
```

Открой в браузере: **http://localhost:8080**

На VPS замени `localhost` на IP сервера: `http://YOUR_IP:8080`

---

## Настройка аккаунта

Все данные вводятся через UI — нажми шестерёнку ⚙ в верхнем правом углу:

- **API Key** — ключ от predict.fun
- **Account Address** — адрес predict-аккаунта (0x...)
- **Private Key** — приватный ключ Privy-кошелька
- **Прокси** — опционально, формат `http://user:pass@host:port`
- **Telegram Bot Token / Chat ID** — опционально, для уведомлений

---

## Использование

1. **⚙ Настройки** — введи данные аккаунта (API ключ, адрес, приватный ключ)
2. **СТАРТ** — запускает движок бота (подключается к API и WebSocket)
3. **Добавить маркеты** — вставь ID маркетов через запятую или с новой строки
4. **Общие настройки** — установи параметры сразу для всех маркетов (размер ордера, спред и т.д.)
5. **Запустить все** — включает выставление ордеров на всех маркетах
6. **Отменить все ордера** — убирает все ордера из стакана
7. **Удалить все маркеты** — полностью очищает список маркетов

---

## Запуск на VPS через systemd (автозапуск)

Создай файл сервиса:

```bash
sudo nano /etc/systemd/system/predictfun-bot.service
```

Содержимое (замени пути под своего пользователя):

```ini
[Unit]
Description=PredictFun Liquidity Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/PredictFun_bot_pabloversion
ExecStart=/home/ubuntu/PredictFun_bot_pabloversion/.venv/bin/python main.py --autostart
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> Флаг `--autostart` обязателен — без него бот поднимется после перезагрузки, но не запустится автоматически.

Запусти:

```bash
sudo systemctl enable predictfun-bot
sudo systemctl start predictfun-bot
sudo systemctl status predictfun-bot
```

Просмотр логов:

```bash
journalctl -u predictfun-bot -f
```

---

## Основные параметры

| Параметр | Описание |
|---|---|
| Целевая ликвидность | Суммарная глубина стакана, которую бот держит |
| Макс. авто-спред | Максимальное расстояние от мид-цены (%) |
| Сторона | YES, NO или оба |
| Позиция USDT | Размер каждого ордера |
| Волатильность | Количество переставлений до отступа |
