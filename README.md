# Binance Short Scanner

Automatically scans all USDT perpetual futures pairs on Binance and alerts you on Telegram when a coin is at an obvious short/retrace point — so you don't have to click through every chart manually.

## How it works

Every 60 seconds the scanner:

1. Fetches all USDT perp futures tickers in one API call
2. Pre-filters to any **liquid coin** (>$5M 24h volume) that's **still within 5% of its 24h high** — daily gain irrelevant; a flat coin that pumps in the last hour still gets caught
3. For each candidate, fetches the **15m and 1h chart**
4. Checks all five conditions for a short setup:
   - RSI(6) ≥ 65 (overbought / elevated)
   - Price within 5% of 24h high (at resistance, not already dumped)
   - Recent pump ≥ 5% in last 10 candles on that timeframe
   - EMA(7) > EMA(25) > EMA(99) (pump confirmed, all EMAs stacked bullish)
   - MACD histogram > 0 (momentum still up, about to turn)
5. Sends a Telegram alert when all conditions are met

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

## No Binance API key required

The scanner only reads **public** market data — no API key needed. It does **not** place trades.

---

## Example alert

```
🔴 SHORT SETUP — UBUSDT  [15m]
2026-05-24 14:30 UTC

Price:      0.15492
24h High:   0.15777  (1.8% below — near top)
24h Change: +28.14%   |  24h Vol: $94.5M
Recent pump: +6.8% (last 10 candles on 15m)

RSI(6):  67.4  |  RSI(12): 63.2  |  RSI(24): 63.6
EMA7:    0.15469
EMA25:   0.14976
EMA99:   0.13836
MACD:    0.00023

⚠️ Coin is pumping, near 24h high, EMAs stacked — potential retrace incoming
Consider SHORT entry
```
