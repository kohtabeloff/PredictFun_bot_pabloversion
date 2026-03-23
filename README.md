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

## Настройка аккаунта

Скопируй файл примера и заполни своими данными:

```bash
cp accounts.txt.example accounts.txt
```

Формат `accounts.txt`:
```
api_key,0xАдрес,приватный_ключ_privy
# или с прокси:
api_key,0xАдрес,приватный_ключ_privy,http://user:pass@host:port
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

## Использование

1. **Настройки** (шестерёнка в хедере) — введи пароль для UI, Telegram токен (опционально)
2. **СТАРТ** — запускает движок бота (подключается к API и WebSocket)
3. **Добавить маркеты** — вставь ID маркетов через запятую или с новой строки
4. **Общие настройки** — установи параметры сразу для всех маркетов
5. **▶ Запустить** на карточке маркета — бот начинает выставлять ордера
6. **Отменить все** — убирает все ордера из стакана

---

## Запуск на VPS через systemd (автозапуск)

Создай файл сервиса:

```bash
sudo nano /etc/systemd/system/predictfun-bot.service
```

Содержимое:

```ini
[Unit]
Description=PredictFun Liquidity Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/PredictFun_bot_pabloversion
ExecStart=/home/ubuntu/PredictFun_bot_pabloversion/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Запусти:

```bash
sudo systemctl enable predictfun-bot
sudo systemctl start predictfun-bot
sudo systemctl status predictfun-bot
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
