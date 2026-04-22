# PredictFun Market-Making Bot

> 🇷🇺 [Русская версия](README_RU.md) | 🐦 [X @Red_Devil_74](https://x.com/Red_Devil_74) | 💼 [LinkedIn](https://www.linkedin.com/in/pavelbelovinvest/)

A market-making bot for [Predict.fun](https://predict.fun) prediction markets. It automatically places limit orders on both sides (YES / NO) of prediction markets, earning the spread between bids and asks.

**How it works:** the bot continuously monitors the orderbook, places limit orders near the mid price, and repositions them when the market moves. You earn the spread when your orders get filled.

The bot runs on a VPS server and has a web dashboard — you manage everything through a browser, no command line needed after setup.

---

## ⚠️ Disclaimer

This bot is provided free of charge, as is. The author **takes no responsibility** for loss of funds, technical failures, API changes on Predict.fun, or any other consequences of using the bot. Prediction market trading involves risk — you act at your own discretion.

**Recommendation:** before using, paste the code into any AI (ChatGPT, Claude, Gemini) and ask it to review for safety and correctness. Takes 5 minutes and gives extra confidence.

---

## Getting help

If something doesn't work — open [Claude Code](https://claude.ai/code) or [ChatGPT](https://chatgpt.com) directly in the bot folder. The AI will help with any installation step, explain errors, and suggest fixes.

---

## Table of Contents

1. [What you need](#1-what-you-need)
2. [Getting a VPS](#2-getting-a-vps)
3. [Getting credentials from Predict.fun](#3-getting-credentials-from-predictfun)
4. [Installing the bot on VPS](#4-installing-the-bot-on-vps)
5. [Configuring the bot](#5-configuring-the-bot)
6. [Running](#6-running)
7. [Auto-start on reboot](#7-auto-start-on-reboot)
8. [How to use](#8-how-to-use)
9. [Settings reference](#9-settings-reference)

---

## 1. What you need

- VPS server (Linux, Ubuntu 22.04+)
- A [Predict.fun](https://predict.fun) account with funds deposited
- Starting capital: ~$50–100 (you can start smaller, but spread market-making works better with more depth)

---

## 2. Getting a VPS

A VPS is a remote server that runs your bot 24/7. Without it, the bot only works while your computer is on.

### Minimum requirements

| Parameter | Minimum |
|-----------|---------|
| CPU | 1 vCPU |
| RAM | 1 GB |
| Disk | 10 GB SSD |
| OS | Ubuntu 22.04 LTS |
| Python | 3.10+ |

### Where to buy

- **[Aeza.net](https://aeza.net)** ⭐ — great price, crypto accepted, fast setup
- **[HiHoster](https://hihoster.com)** ⭐ — solid option, crypto accepted
- **[Hetzner](https://www.hetzner.com/)** — reliable, affordable European servers
- **[DigitalOcean](https://www.digitalocean.com/)** — beginner-friendly

### Connecting to the server

After purchasing you'll receive an IP address, login, and password. Connect via SSH.

**Mac / Linux** — open Terminal:
```bash
ssh root@YOUR_IP
```
Enter the password when prompted (characters won't show while typing — that's normal).

**Windows** — open PowerShell (Win+R → `powershell`) and run the same command. Or use [PuTTY](https://putty.org).

> If stuck — ask an AI: "how to connect to VPS via SSH from Windows/Mac".

---

## 3. Getting credentials from Predict.fun

You need three things from Predict.fun: an API key, your account address, and a private key.

### API Key

API keys are issued manually through the Predict.fun Discord:

1. Join the [Predict.fun Discord](https://discord.gg/predictdotfun)
2. Find the **#support-ticket** channel and open a ticket
3. Request an API key, including your wallet address in the message
4. Save the key once received

### Account Address

This is your public address on Predict.fun — the one shown in your profile (starts with `0x...`).

### Private Key (Privy Wallet)

Predict.fun uses [Privy](https://privy.io) embedded wallets. To export your private key:

1. Go to [predict.fun](https://predict.fun) and open **Settings**
2. Find the **Export Private Key** section
3. Follow the steps to reveal and copy your private key
4. Store it somewhere safe — you'll need it to configure the bot

> **Never share your private key with anyone.** Whoever has it controls the wallet.

---

## 4. Installing the bot on VPS

Run these commands one by one after connecting to your server.

### Update the system
```bash
sudo apt update && sudo apt upgrade -y
```

### Install Python and Git
```bash
sudo apt install python3 python3-venv python3-pip git -y
```

### Download the bot
```bash
cd ~
git clone https://github.com/kohtabeloff/PredictFun_bot_pabloversion.git predictfun_bot
cd predictfun_bot
```

### Create virtual environment and install dependencies
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 5. Configuring the bot

Create the config file from the example:
```bash
cp bot_config.json.example bot_config.json
nano bot_config.json
```

Or create it manually:
```bash
nano bot_config.json
```

Fill in the fields:

```json
{
  "api_key": "your_api_key_from_predictfun",
  "predict_account_address": "0x...",
  "privy_wallet_private_key": "your_private_key",
  "proxy": "",
  "telegram_token": "",
  "telegram_chat_id": "",
  "ui_password": ""
}
```

### Field descriptions

| Field | Required | Description |
|-------|----------|-------------|
| `api_key` | Yes | API key from Predict.fun |
| `predict_account_address` | Yes | Your account address (`0x...`) |
| `privy_wallet_private_key` | Yes | Privy wallet private key for signing orders |
| `proxy` | No | HTTP proxy if needed (format: `http://user:pass@host:port`) |
| `telegram_token` | No | Telegram bot token for fill notifications (create via [@BotFather](https://t.me/BotFather)) |
| `telegram_chat_id` | No | Your Telegram chat ID (get from [@userinfobot](https://t.me/userinfobot)) |
| `ui_password` | No | Password to protect the web dashboard (leave empty to disable) |

Save the file: `Ctrl+O`, then `Ctrl+X`.

> **New account?** If you just registered on Predict.fun and haven't made any trades yet, the bot won't be able to connect. Manually buy shares in any market for any amount (even $1) first — this activates your account and the bot will work normally after that.

---

## 6. Running

Make sure you're in the bot folder:
```bash
cd ~/predictfun_bot
source venv/bin/activate
```

Start the bot in the background:
```bash
nohup venv/bin/python main.py >> logs/session.log 2>&1 &
```

Then open the web dashboard in your browser:
```
http://YOUR_SERVER_IP:8080
```

Replace `YOUR_SERVER_IP` with your actual server IP address.

> The Manager dashboard (for managing multiple accounts) runs on port **8000**: `http://YOUR_SERVER_IP:8000`

To stop the bot:
```bash
pkill -f main.py
```

To view logs:
```bash
tail -f ~/predictfun_bot/logs/session.log
```

---

## 7. Auto-start on reboot

This makes the bot restart automatically if the server reboots.

Create a service file:
```bash
sudo nano /etc/systemd/system/predictfun-bot.service
```

Paste the content (replace `YOUR_USER` with your login — usually `root`):

```ini
[Unit]
Description=PredictFun Market-Making Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/predictfun_bot
ExecStart=/home/YOUR_USER/predictfun_bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

If you're using the root user, replace `/home/YOUR_USER/predictfun_bot` with `/root/predictfun_bot`.

Save (`Ctrl+O`, `Ctrl+X`) and enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable predictfun-bot
sudo systemctl start predictfun-bot
```

Check the status:
```bash
sudo systemctl status predictfun-bot
```

View logs:
```bash
sudo journalctl -u predictfun-bot -f
```

### Also auto-start the Manager (port 8000)

If you use the Manager dashboard, create a second service for it:

```bash
sudo nano /etc/systemd/system/predictfun-manager.service
```

```ini
[Unit]
Description=PredictFun Manager Dashboard
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/predictfun_bot
ExecStart=/home/YOUR_USER/predictfun_bot/venv/bin/python run_manager.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable it:
```bash
sudo systemctl daemon-reload
sudo systemctl enable predictfun-manager
sudo systemctl start predictfun-manager
```

---

## 8. How to use

### Dashboard overview

Open `http://YOUR_SERVER_IP:8080` in your browser. The main controls are:

| Element | What it does |
|---------|-------------|
| **START** | Connects the bot to Predict.fun API and WebSocket |
| **Add Markets** | Add markets by ID |
| **Global Settings** | Set parameters for all markets at once |
| **Run All** | Start placing orders on all markets |
| **▶ / ⏸** | Start or pause an individual market |
| **Cancel All Orders** | Withdraw all active orders from the book — bot pauses briefly then repositions |
| **Remove All Markets** | Clear the market list entirely |
| **Export List** | Download all market IDs as a `.txt` file |
| **Manager** | Open the multi-account dashboard (port 8000) |
| **⚙** | Account settings (API key, address, private key, Telegram) |

### Workflow

1. Press **START** — the bot connects and restores your saved markets (paused by default)
2. Click **Add Markets**, paste market IDs — they'll be added in a paused state so you can configure them first
3. In **Global Settings**, set your order size, spread, and other parameters — click **Apply to All**
4. Press **Run All** — the bot starts placing orders
5. Watch the **Logs** tab to see what the bot is doing in real time

### Manager (port 8000)

If you run the bot for multiple accounts, open `http://YOUR_SERVER_IP:8000` to see all accounts in one place — balances, active markets, and status at a glance. Each account's full UI opens in a new tab.

The list of bots is stored in `manager.json` and can be edited from the Manager interface or manually.

### Telegram notifications (optional)

When configured, the bot sends a Telegram message whenever an order gets filled — including the result of the automatic position sell.

To set it up:
1. Create a bot via [@BotFather](https://t.me/BotFather) — it'll give you a token
2. Get your chat ID from [@userinfobot](https://t.me/userinfobot)
3. Enter both into `bot_config.json` (`telegram_token` and `telegram_chat_id`)

### Proxy support

If you run multiple accounts on one server, Predict.fun sees them all from the same IP. Assign each account a different proxy in the settings (⚙) — field **Proxy**. Format: `http://host:port` or `http://user:pass@host:port`.

---

## 9. Settings reference

| Setting | Default | Description |
|---------|---------|-------------|
| **Position USDT** | — | Size of each order placed by the bot, in USDT |
| **Target liquidity ($)** | — | Minimum USD depth in the orderbook required before the bot places orders. Helps avoid thin markets. |
| **Min orders before** | 0 (off) | Minimum number of orders ahead of yours in the queue. Set to 0 to disable. |
| **Max auto-spread (%)** | 6% | Maximum distance from the mid price the bot will place orders. Wider = more fill chance, more risk. |
| **Min spread (¢)** | 0.2¢ | Minimum distance from mid price in cents. Prevents placing orders too close to the current price. |
| **Liquidity mode** | BID | `BID` — standard mode. `ASK` — alternative liquidity mode. |
| **Side** | Both | Which side to make markets on: `Both`, `YES only`, or `NO only`. |
| **Reposition limit** | 0 (off) | Max number of repositions within the volatility window before cooldown kicks in. Set to 0 to disable. |
| **Volatility window (sec)** | — | Time window in seconds for counting repositions. |
| **Volatility cooldown (sec)** | — | How long the bot pauses after hitting the reposition limit. |

---

## Tips

**Start small.** Run your first markets with a minimum order size. Make sure the bot correctly places and repositions orders before scaling up.

**Don't add too many markets at once.** Start with 10–20, check that everything is stable, then expand.

**Pick active markets.** The bot earns when orders get filled. Choose markets with actual trading volume — thin markets with no activity won't generate fills.

**Volatility protection matters.** In fast-moving markets, the bot can reposition many times in a row, paying fees each time. Set the reposition limit and volatility window to protect yourself.

**Use a UI password.** If your VPS is publicly accessible, set a `ui_password` in `bot_config.json` so only you can access the dashboard.

---

## Updates and contacts

Follow for bot updates, strategies, and insights:

- 🐦 X (Twitter): [@Red_Devil_74](https://x.com/Red_Devil_74)
- 💼 LinkedIn: [pavelbelovinvest](https://www.linkedin.com/in/pavelbelovinvest/)

Support the author:

- EVM: `0xA3aCe3905fb080930f7Eeac9Fe401F5B41b16629`
- SOL: `5UztCBoUq2HvtH5nibLmWgxuR5fU5AeagkX9mqdXa5Pq`
