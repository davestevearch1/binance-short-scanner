# Binance Short, Long & Breakout Scanner

Automatically scans all USDT perpetual futures pairs on Binance and alerts you on Telegram when a coin matches one of three setups:

- 🔴 **Short** — fade an exhausted pump near the 24h high
- 🟢 **Bounce-long** — fade an exhausted dump (hammer) near the 24h low
- 🚀 **Breakout-long** — catch a confirmed momentum shift (above EMA25, rising RSI, volume conviction)

Each alert includes a stop-loss suggestion based on the trigger level (24h high for shorts, 24h low for bounces, EMA25 for breakouts).

## Telegram modes (live-switchable by admin)

| Command | Shorts | Bounce-longs | Breakouts |
|---|---|---|---|
| `/v1` | ✓ loose | — | — |
| `/v2` (default) | ✓ strict | — | — |
| `/long` | — | ✓ strict | ✓ strict |
| `/v1Both` | ✓ loose | ✓ loose | ✓ loose |
| `/v2Both` | ✓ strict | ✓ strict | ✓ strict |

`/status` shows the current mode and the active filters for each side. `/help` lists all commands. `/pause` and `/resume` are admin-only.

## Bounce-long vs breakout-long — what's the difference?

Both go long, but the entry pattern is opposite:

- **Bounce-long** fires after a sharp dump and a hammer candle near the 24h low. Bet: capitulation is over, price snaps back. Stop: just below the 24h low.
- **Breakout-long** fires when a coin transitions from downtrend to uptrend with confirmation — above EMA25, RSI(6) crossing up, volume spike, 24h change +5–25%. Bet: momentum shift will continue. Stop: just below EMA25.

The breakout catches the kind of move where a coin grinds +10–20% in a day without ever dumping or spiking — the bounce-long would miss it, and the short scanner won't fire either (RSI never hits 70+).

## How it works

Every 60 seconds the scanner:

1. Fetches all USDT perp futures tickers in one API call
2. Builds up to three candidate lists (depending on the active mode):
   - **Shorts**: liquid coins within 5% of 24h high
   - **Bounce-longs**: liquid coins within 5% of 24h low, not down >20% on day (knife filter)
   - **Breakouts**: liquid coins up 5–25% on day
3. For each candidate, fetches the **15m and 1h chart** and runs EMA(7/25/99), RSI(6/12/24), MACD
4. Sends a Telegram alert (with stop suggestion) when all conditions for that direction's strategy match
5. Each direction has its own 1h cooldown per coin per timeframe — a single coin can alert short, then later breakout or bounce, independently

---

## Setup

### Step 1 — Install Python dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the steps
3. Copy the **bot token** it gives you (looks like `123456:ABC-DEF...`)

### Step 3 — Get your Telegram Chat ID

1. Search for **@userinfobot** on Telegram
2. Start it — it will reply with your Chat ID (a number like `987654321`)

### Step 4 — Configure your credentials

```bash
cp .env.example .env
```

Edit `.env` and paste your token and chat ID:

```
TELEGRAM_TOKEN=123456:ABC-DEFyourtokenhere
TELEGRAM_CHAT_ID=987654321
```

### Step 5 — Run the scanner

```bash
python scanner.py
```

Live logs print to the terminal. Alerts go to your Telegram.

---

## Adjusting the strategy thresholds

Open `scanner.py` and edit the constants at the top:

| Setting | Default | What it does |
|---|---|---|
| `MIN_24H_QUOTE_VOL` | `5_000_000` | Skip illiquid coins — only scan ones with >$5M USDT 24h volume |
| `MAX_DIST_FROM_HIGH` | `0.05` | Price must be within 5% of 24h high |
| `MIN_RECENT_PUMP` | `0.05` | Price must have gained ≥5% in last 10 candles |
| `RSI6_MIN` | `65` | Minimum RSI(6) to trigger alert |
| `TIMEFRAMES` | `["15m", "1h"]` | Which chart timeframes to check |
| `SCAN_INTERVAL_SEC` | `60` | How often to scan (seconds) |
| `ALERT_COOLDOWN_SEC` | `3600` | Min time between re-alerts for same coin |

---

## Deployment — Running 24/7

The scanner runs as an infinite loop, so it needs a machine that stays on. Pick one:

### Option A: Docker (recommended for any cloud or local)

```bash
cp .env.example .env   # fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
docker compose up -d   # builds image, starts container, auto-restarts on crash
docker compose logs -f # tail live logs
```

### Option B: Cheap VPS — Hetzner CX21 (~$4/mo) or DigitalOcean Droplet (~$6/mo)

```bash
# On the VPS:
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

git clone https://github.com/davestevearch1/binance-short-scanner.git
cd binance-short-scanner

# Use a virtualenv (required on Ubuntu 23.04+ due to PEP 668):
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env   # paste your TELEGRAM_TOKEN and TELEGRAM_CHAT_ID

# Install as a systemd service so it survives reboots.
# Edit User= and paths inside scanner.service if your VPS user is not "ubuntu":
sudo cp scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable scanner
sudo systemctl start scanner
sudo systemctl status scanner   # should show "active (running)"
```

### Option C: Google Cloud free tier (GCE e2-micro)

Google Cloud offers a permanently free `e2-micro` VM in `us-east1`, `us-west1`, or `us-central1`. Set it up the same way as Option B. See [GCP Always Free docs](https://cloud.google.com/free/docs/free-cloud-features#compute).

> **Why not Firebase?** Firebase Hosting is for static sites and Firebase Functions are serverless (max 9 min per call). Neither can run a persistent Python process. Use Compute Engine or a VPS instead.

---

## No Binance API key required

The scanner only reads **public** market data — no API key needed. It does **not** place trades.

---

## Example alerts

Short — fade an exhausted pump:
```
🔴 SHORT SETUP — UBUSDT  [15m]  mode:v2
...
🛑 Stop suggestion: above 0.15856  (+2.3% from entry)
```

Bounce-long — buy capitulation:
```
🟢 LONG SETUP — HUSDT  [15m]  mode:v2Both
...
🛑 Stop suggestion: below 0.19473  (-1.6% from entry)
```

Breakout-long — momentum shift confirmed:
```
🚀 BREAKOUT LONG — RIFUSDT  [1h]  mode:v2Both
...
EMA25: 0.06508  ← stop trigger
🛑 Stop suggestion: below 0.06475  (-3.9% from entry)
```

The emoji prefix (🔴 / 🟢 / 🚀) makes the setup type recognisable at a glance.
