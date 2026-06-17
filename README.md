# Binance Short, Long & Breakout Scanner

Automatically scans all USDT perpetual futures pairs on Binance and alerts you on Telegram when a coin matches one of four setups:

- 🔴 **Fade-short** — fade an exhausted pump near the 24h high
- 🔻 **Breakdown-short** — short a coin already trending **down** that's breaking lower with volume (trend continuation)
- 🟢 **Bounce-long** — fade an exhausted dump (hammer) near the 24h low
- 🚀 **Breakout-long** — catch a confirmed momentum shift (above EMA25, rising RSI, volume conviction)

The two shorts are mirror images: the **fade-short** needs a *bullish* EMA stack near the 24h high (short the top of a pump), while the **breakdown-short** needs a *bearish* EMA stack with the coin down 5–40% on the day (short the continuation of a downtrend). The breakdown is the structural mirror of the breakout-long.

Each alert includes a stop-loss suggestion based on the trigger level (24h high for fade-shorts, EMA25 for breakdowns, 24h low for bounces, EMA25 for breakouts).

## Telegram modes (live-switchable by admin)

A mode controls **which alerts are sent** — it does *not* limit what's measured.
All four strategies use the strict rules and run on every scan.

| Command | Sends alerts for |
|---|---|
| `/short` | Fade-shorts + Breakdown-shorts |
| `/long` (default) | Bounce-longs + Breakouts |
| `/both` | All four |

(`/v2` and `/v2Both` still work as hidden aliases for `/short` and `/both`.)

`/status` shows the current mode and the active filters for each side. `/help` lists all commands. `/pause` and `/resume` are admin-only.

### Shadow-logging: data on every strategy, even when not alerting

Every scan **evaluates and records** all four strategies on **both timeframes**,
regardless of the active mode — but only *sends* alerts for the mode's directions
on the alert timeframe (1h). So even while running `/long`, the scanner silently
measures how both shorts (and every 15m signal) *would* have done, using the real
production code path and live forward prices (no backtest/lookahead bias).

`/stats` breaks the +2h outcomes down **by strategy and timeframe**, and notes how
many were actually sent vs shadow-measured. Each tracked trade also records its
MFE/MAE (max favorable/adverse move), an indicator snapshot, the daily-% context
(2h move, change since 00:00 UTC, blow-off flag), and the market regime at entry —
all for later evidence-based tuning.

- **Measure** all timeframes: `TIMEFRAMES = ["15m", "1h"]`
- **Alert** only: `ALERT_TIMEFRAMES = ["1h"]`

### Daily-% context & market regime

Every signal records three signed price-change windows, computed from klines
already fetched (no extra API calls):

- **`change_2h`** — % move over the last 2 hours
- **`change_utc`** — % move since 00:00 UTC (Binance's anchored daily change)
- **`blowoff`** — flagged when the coin is up ≥ `BLOWOFF_PCT` (100%) on the day

These are interpreted per direction: a fade-short reads them as a pumped/parabolic
top, while a breakdown-short lives in negative territory (down 5–40% on the day).
For now they're **recorded only** (`FADE_MIN_DAILY_CHANGE = None`) so thresholds can
be tuned on real outcomes.

Each scan also measures a **market regime** from the full ticker list — the average
absolute 24h move and breadth (% of liquid coins up). On a *quiet* day
(`avg |24h move| < REGIME_QUIET_AVG_ABS`, default 3%) sends are suppressed
(`REGIME_SUPPRESS_ON_QUIET`) while everything is still shadow-measured, so the alert
stream stays meaningful when the whole market is flat. The regime shows in `/status`
and the hourly heartbeat.

## Bounce-long vs breakout-long — what's the difference?

Both go long, but the entry pattern is opposite:

- **Bounce-long** fires after a sharp dump and a hammer candle near the 24h low. Bet: capitulation is over, price snaps back. Stop: just below the 24h low.
- **Breakout-long** fires when a coin transitions from downtrend to uptrend with confirmation — above EMA25, RSI(6) crossing up, volume spike, 24h change +5–25%. Bet: momentum shift will continue. Stop: just below EMA25.

The breakout catches the kind of move where a coin grinds +10–20% in a day without ever dumping or spiking — the bounce-long would miss it, and the short scanner won't fire either (RSI never hits 70+).

## How it works

Every 60 seconds the scanner:

1. Fetches all USDT perp futures tickers in one API call
2. Builds all four candidate lists (every scan, regardless of mode):
   - **Fade-shorts**: liquid coins within 5% of 24h high
   - **Breakdown-shorts**: liquid coins down 5–40% on day (trending lower, not capitulated)
   - **Bounce-longs**: liquid coins within 5% of 24h low, not down >20% on day (knife filter)
   - **Breakouts**: liquid coins up 5–50% on day
3. For each candidate, fetches the **15m and 1h chart** and runs EMA(7/25/99), RSI(6/12/24), MACD
4. **Records** every matching signal for +2h outcome tracking (shadow-logging), and **sends** a Telegram alert (with stop suggestion) only when the signal's direction is enabled in the current mode and its timeframe is an alert timeframe (1h)
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
| `MAX_DIST_FROM_HIGH` | `0.05` | Fade-short: price must be within 5% of 24h high |
| `MIN_RECENT_PUMP` | `0.05` | Fade-short: price must have gained ≥5% in last 10 candles |
| `RSI6_MIN` | `70` | Fade-short: minimum RSI(6) to trigger |
| `BREAKDOWN_DROP_MIN` / `_MAX` | `5.0` / `40.0` | Breakdown-short: 24h change must be between −5% and −40% |
| `BREAKDOWN_RSI_MIN` / `BREAKDOWN_RSI_MAX` | `15` / `40` | Breakdown-short: RSI(6) band (falling, not capitulated) |
| `MAX_BREAKDOWN_STOP_PCT` | `5.0` | Breakdown-short: skip if stop above EMA25 would be >5% wide |
| `FADE_MIN_DAILY_CHANGE` | `None` | Optional fade-short floor — require up ≥X% on the day (off by default) |
| `BLOWOFF_PCT` | `100.0` | Flag coins up ≥100% on the day as blow-off tops |
| `REGIME_QUIET_AVG_ABS` | `3.0` | Avg \|24h move\| below this = "quiet" market |
| `REGIME_SUPPRESS_ON_QUIET` | `True` | Shadow-only (no sends) on quiet days |
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

The trade-tracker history (`state.json`) is persisted on a named Docker volume
(`scanner-data`, mounted at `/app/data` via `STATE_FILE`), so it survives
restarts and redeploys. For a non-Docker deploy, set `STATE_FILE` to a path
outside the working tree to keep history across `git pull`s.

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

Fade-short — fade an exhausted pump:
```
🔴 SHORT SETUP — UBUSDT  [1h]  mode:short
...
🛑 Stop suggestion: above 0.15856  (+2.3% from entry)
```

Breakdown-short — short a downtrend breaking lower:
```
🔻 BREAKDOWN SHORT — DOGEUSDT  [1h]  mode:short
24h Change:  -12.00%   |  ...
Since 00:00 UTC: -8.40%   |  2h: -3.10%
EMA25: 84.15401175  ← stop trigger
🛑 Stop suggestion: above 84.57478  (+4.1% from entry)
```

Bounce-long — buy capitulation:
```
🟢 LONG SETUP — HUSDT  [1h]  mode:long
...
🛑 Stop suggestion: below 0.19473  (-1.6% from entry)
```

Breakout-long — momentum shift confirmed:
```
🚀 BREAKOUT LONG — RIFUSDT  [1h]  mode:long
...
EMA25: 0.06508  ← stop trigger
🛑 Stop suggestion: below 0.06475  (-3.9% from entry)
```

The emoji prefix (🔴 fade-short / 🔻 breakdown-short / 🟢 bounce-long / 🚀 breakout-long) makes the setup type recognisable at a glance.
