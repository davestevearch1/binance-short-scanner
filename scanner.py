#!/usr/bin/env python3
"""
Binance Futures Short, Long & Breakout Scanner
-----------------------------------------------
Three setup types in one scanner:
  - Shorts (🔴):   fade an exhausted pump near the 24h high
  - Bounce-longs (🟢): fade an exhausted dump (hammer) near the 24h low
  - Breakouts (🚀): catch a confirmed momentum shift to the upside (above EMA25,
                    rising RSI, volume conviction, 24h change +5–25%)

Each alert includes a stop suggestion based on the trigger level
(24h high for shorts, 24h low for bounces, EMA25 for breakouts).

Modes (admin-switchable from Telegram):
  /v1      — shorts only, loose conditions
  /v2      — shorts only, strict  (default)
  /long    — bounce-longs + breakouts (longs only), strict
  /v1Both  — shorts + bounce-longs + breakouts, loose
  /v2Both  — shorts + bounce-longs + breakouts, strict

Setup: copy .env.example to .env and fill in your Telegram credentials.
Run:   python scanner.py
"""

import os
import json
import time
import logging
import requests
import pandas as pd
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Telegram credentials (set in .env) ───────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
# Admin chat — only this ID can change mode / pause / resume
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Everyone who receives alerts (defaults to just the admin)
TELEGRAM_BROADCAST_IDS = [
    cid.strip() for cid in os.getenv("TELEGRAM_BROADCAST_IDS", TELEGRAM_CHAT_ID).split(",")
    if cid.strip()
]

# ── Binance Futures base URL ──────────────────────────────────────────────────
FUTURES_BASE = "https://fapi.binance.com"

# ── Timeframes to scan ────────────────────────────────────────────────────────
TIMEFRAMES = ["15m", "1h"]

# ── Filters fixed across all modes ───────────────────────────────────────────
MIN_24H_QUOTE_VOL  = 5_000_000  # Only scan coins with >$5M USDT 24h volume
MAX_DIST_FROM_HIGH = 0.05       # Short pre-filter: within 5% of 24h high
MAX_DIST_FROM_LOW  = 0.05       # Long  pre-filter: within 5% of 24h low
MIN_RECENT_PUMP    = 0.05       # Short: price gained >=5% in last 10 candles
MIN_RECENT_DUMP    = 0.07       # Long:  price dropped >=7% in last 10 candles
REQUIRE_EMA_STACK  = True       # Short: EMA7 > EMA25 > EMA99 (bullish stack)
REQUIRE_EMA_BEAR   = True       # Long:  EMA7 < EMA25 < EMA99 (bearish stack)
REQUIRE_MACD_POS   = True       # Short: MACD histogram must be positive
REQUIRE_MACD_NEG   = True       # Long:  MACD histogram must be negative
KNIFE_MAX_DROP     = 0.20       # Long:  skip coins down >20% on the day
MIN_VOL_MULTIPLIER = 1.5        # Long/Breakout: candle volume >= 1.5x 20-candle average
STOP_BUFFER_PCT    = 0.005      # 0.5% buffer past the trigger level for stop suggestion

# Breakout-long (momentum trade) fixed thresholds
BREAKOUT_MIN_DAILY       = 5.0   # Breakout pre-filter: 24h change >= +5%
BREAKOUT_MAX_DAILY       = 50.0  # Breakout pre-filter: 24h change <= +50% (skip exhausted runners)
BREAKOUT_MIN_RECENT_PUMP = 0.03  # Breakout: price up >=3% in last 10 candles on this TF
BREAKOUT_RSI_MAX         = 85    # Breakout: RSI(6) must be < 85 (rising RSI separates from shorts)
MAX_BREAKOUT_STOP_PCT    = 5.0   # Breakout: skip if stop would be >5% wide (bad R:R)

# ── Mode preset (live-switchable via Telegram) ────────────────────────────────
# Restart reverts to DEFAULT_MODE.
DEFAULT_MODE = "v2"
VALID_MODES  = ("v1", "v2", "long", "v1both", "v2both")  # matched lowercased

# These globals are set by apply_mode() at startup and on each Telegram command.
MODE:                   str
SHORTS_ENABLED:         bool
LONGS_ENABLED:          bool
BREAKOUTS_ENABLED:      bool
# Short-side thresholds
RSI6_MIN:               int
REQUIRE_MACD_DECLINING: bool
REQUIRE_RSI_DECLINING:  bool
MIN_UPPER_WICK_RATIO:   float
# Long-side thresholds
RSI6_MAX:               int
REQUIRE_MACD_RISING:    bool
REQUIRE_RSI_RISING:     bool
MIN_LOWER_WICK_RATIO:   float
# Breakout-side thresholds (mode-controlled v1/v2 toggles)
BREAKOUT_RSI_MIN:               int
REQUIRE_BREAKOUT_FULL_STACK:    bool
REQUIRE_BREAKOUT_MACD_RISING:   bool

# Canonical display names (preserves v1Both / v2Both capitalisation)
_MODE_DISPLAY = {
    "v1":     "v1",
    "v2":     "v2",
    "long":   "long",
    "v1both": "v1Both",
    "v2both": "v2Both",
}


def _apply_short_strategy(name: str) -> None:
    global RSI6_MIN, REQUIRE_MACD_DECLINING, REQUIRE_RSI_DECLINING, MIN_UPPER_WICK_RATIO
    if name == "v1":
        RSI6_MIN, REQUIRE_MACD_DECLINING, REQUIRE_RSI_DECLINING, MIN_UPPER_WICK_RATIO = \
            65, False, False, 0.0
    else:  # v2
        RSI6_MIN, REQUIRE_MACD_DECLINING, REQUIRE_RSI_DECLINING, MIN_UPPER_WICK_RATIO = \
            70, True, True, 0.35


def _apply_long_strategy(name: str) -> None:
    global RSI6_MAX, REQUIRE_MACD_RISING, REQUIRE_RSI_RISING, MIN_LOWER_WICK_RATIO
    if name == "v1":
        RSI6_MAX, REQUIRE_MACD_RISING, REQUIRE_RSI_RISING, MIN_LOWER_WICK_RATIO = \
            30, False, False, 0.0
    else:  # v2
        RSI6_MAX, REQUIRE_MACD_RISING, REQUIRE_RSI_RISING, MIN_LOWER_WICK_RATIO = \
            25, True, True, 0.35


def _apply_breakout_strategy(name: str) -> None:
    global BREAKOUT_RSI_MIN, REQUIRE_BREAKOUT_FULL_STACK, REQUIRE_BREAKOUT_MACD_RISING
    if name == "v1":
        BREAKOUT_RSI_MIN, REQUIRE_BREAKOUT_FULL_STACK, REQUIRE_BREAKOUT_MACD_RISING = \
            50, False, False
    else:  # v2
        BREAKOUT_RSI_MIN, REQUIRE_BREAKOUT_FULL_STACK, REQUIRE_BREAKOUT_MACD_RISING = \
            60, True, True


def apply_mode(mode: str) -> bool:
    """
    Switch scanner mode. Case-insensitive. Returns True if applied, False if unknown.
    Mapping:
      v1     → shorts v1, longs off,   breakouts off
      v2     → shorts v2, longs off,   breakouts off
      long   → shorts off, longs v2,   breakouts v2
      v1both → shorts v1, longs v1,    breakouts v1
      v2both → shorts v2, longs v2,    breakouts v2
    """
    global MODE, SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED
    key = mode.strip().lower()
    if key not in VALID_MODES:
        return False
    MODE = _MODE_DISPLAY[key]
    if key == "v1":
        SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED = True,  False, False
        _apply_short_strategy("v1");  _apply_long_strategy("v2");  _apply_breakout_strategy("v2")
    elif key == "v2":
        SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED = True,  False, False
        _apply_short_strategy("v2");  _apply_long_strategy("v2");  _apply_breakout_strategy("v2")
    elif key == "long":
        SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED = False, True,  True
        _apply_short_strategy("v2");  _apply_long_strategy("v2");  _apply_breakout_strategy("v2")
    elif key == "v1both":
        SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED = True,  True,  True
        _apply_short_strategy("v1");  _apply_long_strategy("v1");  _apply_breakout_strategy("v1")
    elif key == "v2both":
        SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED = True,  True,  True
        _apply_short_strategy("v2");  _apply_long_strategy("v2");  _apply_breakout_strategy("v2")
    return True


if not apply_mode(DEFAULT_MODE):
    raise SystemExit(f"Invalid DEFAULT_MODE {DEFAULT_MODE!r} — must be one of {VALID_MODES}")

# ── Scanner behaviour ─────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC  = 60    # Full scan every 60 seconds
ALERT_COOLDOWN_SEC = 3600  # Don't re-alert same coin+timeframe+direction for 1 hour
KLINE_LIMIT        = 150   # Candles fetched per symbol per timeframe

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Key: "SYMBOL:timeframe:direction" → unix timestamp of last alert (direction = short|long|breakout)
_alerted: dict[str, float] = {}

# Session-level counters for heartbeat
_total_scans  = 0
_total_alerts = 0
_last_heartbeat: float = 0.0
HEARTBEAT_INTERVAL_SEC = 3600

# ── Telegram command polling state ────────────────────────────────────────────
_paused = False
_telegram_update_offset = 0
COMMAND_POLL_INTERVAL_SEC = 5

# ── Performance tracker ───────────────────────────────────────────────────────
STATE_FILE            = "state.json"
TRACKER_CHECKUP_1_SEC = 1800   # +30 minutes
TRACKER_CHECKUP_2_SEC = 7200   # +2 hours (final)
RESULTS_MAX_DAYS      = 30     # keep results for 30 days then purge

_tracked: list[dict] = []  # alerts awaiting follow-up
_results: list[dict] = []  # completed outcomes


def load_state() -> None:
    global _tracked, _results
    try:
        data = json.loads(Path(STATE_FILE).read_text())
        _tracked = data.get("tracked", [])
        _results = data.get("results", [])
        log.info("Tracker: loaded %d pending, %d results from %s",
                 len(_tracked), len(_results), STATE_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("state.json unreadable (%s) — starting fresh", e)


def save_state() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RESULTS_MAX_DAYS)).isoformat()
    fresh  = [r for r in _results if r.get("ts", "") >= cutoff]
    tmp = Path(STATE_FILE + ".tmp")
    tmp.write_text(json.dumps({"tracked": _tracked, "results": fresh}, indent=2))
    tmp.replace(Path(STATE_FILE))


def _current_variant() -> str:
    return "v1" if MODE.lower() in ("v1", "v1both") else "v2"


def track_alert(sig: dict) -> None:
    entry = sig["price"]
    _tracked.append({
        "symbol":       sig["symbol"],
        "timeframe":    sig["timeframe"],
        "direction":    sig["direction"],
        "variant":      _current_variant(),
        "mode":         MODE,
        "entry":        entry,
        "stop":         sig["stop_price"],
        "ts":           time.time(),
        "extreme_seen": entry,
        "checks_done":  0,
    })
    save_state()


def format_checkup_message(item: dict, price: float, stop_hit: bool, final: bool) -> str:
    direction = item["direction"]
    entry     = item["entry"]
    stop      = item["stop"]
    label     = "SHORT" if direction == "short" else ("LONG" if direction == "long" else "BREAKOUT")
    emoji     = "🔴" if direction == "short" else ("🟢" if direction == "long" else "🚀")
    tag       = "(FINAL)" if final else "(+30 min)"
    pct       = (price - entry) / entry * 100
    if direction == "short":
        moving_right = price < entry
        pct_display  = f"{-pct:+.2f}%"  # show as positive when price fell (good for shorts)
    else:
        moving_right = price > entry
        pct_display  = f"{pct:+.2f}%"
    status = "🛑 STOP HIT" if stop_hit else ("✅ in profit" if moving_right else "⚠️ against")
    return (
        f"📊 {emoji} <b>{label} follow-up — {item['symbol']}</b> [{item['timeframe']}] {tag}\n"
        f"Entry: {entry}   Stop: {stop}\n"
        f"Now:   {price}   → {pct_display}   {status}\n"
        f"Extreme seen: {item['extreme_seen']}"
    )


def _process_pending_checkups(tickers: list[dict], now: float) -> None:
    if not _tracked:
        return
    price_map = {t["symbol"]: float(t["lastPrice"]) for t in tickers}
    changed  = False
    finished = []
    for item in _tracked:
        sym   = item["symbol"]
        price = price_map.get(sym)
        if price is None:
            continue
        direction = item["direction"]
        if direction == "short":
            item["extreme_seen"] = max(item["extreme_seen"], price)
        else:
            item["extreme_seen"] = min(item["extreme_seen"], price)
        changed  = True
        stop     = item["stop"]
        stop_hit = (direction == "short" and item["extreme_seen"] > stop) or \
                   (direction != "short" and item["extreme_seen"] < stop)
        elapsed  = now - item["ts"]
        if item["checks_done"] == 0 and elapsed >= TRACKER_CHECKUP_1_SEC:
            send_telegram(format_checkup_message(item, price, stop_hit, final=False))
            item["checks_done"] = 1
        elif item["checks_done"] == 1 and elapsed >= TRACKER_CHECKUP_2_SEC:
            send_telegram(format_checkup_message(item, price, stop_hit, final=True))
            entry   = item["entry"]
            pct     = (price - entry) / entry * 100
            if direction == "short":
                pct = -pct  # positive means price fell (good for shorts)
            outcome = "loss" if stop_hit else ("win" if pct > 0 else "loss")
            _results.append({
                "symbol":    sym,
                "direction": direction,
                "variant":   item.get("variant", "v2"),
                "timeframe": item["timeframe"],
                "mode":      item.get("mode", MODE),
                "entry":     entry,
                "stop":      stop,
                "outcome":   outcome,
                "final_pct": round(pct, 2),
                "ts":        datetime.fromtimestamp(item["ts"], tz=timezone.utc).isoformat(),
            })
            finished.append(item)
    for item in finished:
        _tracked.remove(item)
    if changed or finished:
        save_state()


def format_stats() -> str:
    if not _results:
        return "📈 No completed trade outcomes yet.\nCheck back after alerts have had 2 hours to play out."
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for r in _results:
        grouped[(r["direction"], r.get("variant", "v2"))].append(r)
    lines = [f"📈 <b>Scanner stats</b> ({len(_results)} completed, last {RESULTS_MAX_DAYS}d)\n"]
    for direction, emoji, label in [
        ("short",    "🔴", "Shorts"),
        ("long",     "🟢", "Bounces"),
        ("breakout", "🚀", "Breakouts"),
    ]:
        dir_items = [r for r in _results if r["direction"] == direction]
        if not dir_items:
            continue
        lines.append(f"\n{emoji} <b>{label}</b>")
        for variant in ("v1", "v2"):
            items = grouped.get((direction, variant), [])
            if not items:
                continue
            wins    = sum(1 for r in items if r["outcome"] == "win")
            losses  = len(items) - wins
            pct_win = wins / len(items) * 100
            avg_pct = sum(r["final_pct"] for r in items) / len(items)
            lines.append(
                f"  {variant}: {wins}W / {losses}L = {pct_win:.0f}%   avg {avg_pct:+.1f}% at 2h"
            )
    return "\n".join(lines)


# ── Smoke test ────────────────────────────────────────────────────────────────
def smoke_test() -> None:
    """Verify Binance and Telegram are reachable before entering the main loop."""
    log.info("Running startup checks...")

    try:
        r = requests.get(f"{FUTURES_BASE}/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=10)
        r.raise_for_status()
        log.info("Binance API  ✓  (BTC price: %s)", r.json().get("price", "?"))
    except Exception as e:
        log.error("Binance API unreachable: %s — cannot run scanner. Check network.", e)
        raise SystemExit(1)

    if not TELEGRAM_TOKEN or not TELEGRAM_BROADCAST_IDS:
        log.warning("TELEGRAM_TOKEN or chat IDs not set — alerts will print to console only")
        return

    log.info("Telegram     ✓  (broadcasting to %d chat(s), admin=%s)",
             len(TELEGRAM_BROADCAST_IDS), TELEGRAM_CHAT_ID or "(none)")
    sides = []
    if SHORTS_ENABLED:
        sides.append("shorts")
    if LONGS_ENABLED:
        sides.append("bounce-longs")
    if BREAKOUTS_ENABLED:
        sides.append("breakouts")
    send_telegram(
        f"✅ <b>Scanner started</b> — watching Binance perp futures\n"
        f"Mode: <b>{MODE}</b>  ({'  +  '.join(sides)})\n"
        f"Send /help for commands"
    )


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    """Broadcast a message to every chat ID in TELEGRAM_BROADCAST_IDS."""
    if not TELEGRAM_TOKEN or not TELEGRAM_BROADCAST_IDS:
        print(f"\n{'─'*60}\n{text}\n{'─'*60}\n")
        return
    for cid in TELEGRAM_BROADCAST_IDS:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            log.error("Telegram broadcast to %s failed: %s", cid, e)


def send_telegram_reply(chat_id: str | int, text: str) -> None:
    """Reply to a single chat (for command responses, not broadcasts)."""
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram reply to %s failed: %s", chat_id, e)


# ── Telegram command polling ──────────────────────────────────────────────────
def format_status() -> str:
    short_info = (
        f"  RSI(6) ≥ {RSI6_MIN}{', declining' if REQUIRE_RSI_DECLINING else ''}\n"
        f"  MACD positive{', declining' if REQUIRE_MACD_DECLINING else ''}\n"
        f"  Upper wick ≥ {int(MIN_UPPER_WICK_RATIO * 100)}%"
    ) if SHORTS_ENABLED else "  (off)"

    long_info = (
        f"  RSI(6) ≤ {RSI6_MAX}{', rising' if REQUIRE_RSI_RISING else ''}\n"
        f"  MACD negative{', rising' if REQUIRE_MACD_RISING else ''}\n"
        f"  Lower wick ≥ {int(MIN_LOWER_WICK_RATIO * 100)}%\n"
        f"  Vol ≥ {MIN_VOL_MULTIPLIER}×  |  knife filter: −{int(KNIFE_MAX_DROP*100)}%"
    ) if LONGS_ENABLED else "  (off)"

    breakout_info = (
        f"  Price > EMA25  |  EMA7 > EMA25{' > EMA99' if REQUIRE_BREAKOUT_FULL_STACK else ''}\n"
        f"  RSI(6) {BREAKOUT_RSI_MIN}–{BREAKOUT_RSI_MAX}, rising\n"
        f"  MACD > 0{', rising' if REQUIRE_BREAKOUT_MACD_RISING else ''}\n"
        f"  Vol ≥ {MIN_VOL_MULTIPLIER}×  |  24h change {BREAKOUT_MIN_DAILY:.0f}–{BREAKOUT_MAX_DAILY:.0f}%"
    ) if BREAKOUTS_ENABLED else "  (off)"

    return (
        f"🟢 <b>Scanner status</b>\n"
        f"Mode:   <b>{MODE}</b>\n"
        f"State:  {'⏸ Paused' if _paused else '▶ Running'}\n"
        f"Scans:  {_total_scans}  |  Alerts: {_total_alerts}\n"
        f"\n"
        f"📉 <b>Shorts</b>\n{short_info}\n"
        f"\n"
        f"📈 <b>Bounce-Longs</b>\n{long_info}\n"
        f"\n"
        f"🚀 <b>Breakouts</b>\n{breakout_info}"
    )


def format_help(is_admin: bool) -> str:
    public = (
        "<b>Commands</b>\n"
        "/status — show current scanner state\n"
        "/stats  — win/loss breakdown per strategy (v1 vs v2)\n"
        "/help   — show this message"
    )
    if not is_admin:
        return public
    return (
        public + "\n\n"
        "<b>Admin — mode commands</b>\n"
        "/v1     — shorts only, loose conditions\n"
        "/v2     — shorts only, strict  (default)\n"
        "/long   — bounce-longs + breakouts (longs only), strict\n"
        "/v1Both — shorts + bounce-longs + breakouts, loose\n"
        "/v2Both — shorts + bounce-longs + breakouts, strict\n"
        "\n"
        "<b>Admin — controls</b>\n"
        "/pause  — stop scanning (alerts off)\n"
        "/resume — resume scanning"
    )


def handle_command(text: str, from_id: int, chat_id: int) -> None:
    """Dispatch a Telegram command. Admin commands rejected for non-admins."""
    global _paused
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    if not cmd.startswith("/"):
        return  # not a command — ignore regular chat messages
    # Strip leading slash and any "@BotName" suffix used in group chats
    cmd_root = cmd[1:].split("@", 1)[0]
    is_admin = str(from_id) == str(TELEGRAM_CHAT_ID)

    # Mode-switching commands (case-insensitive)
    if cmd_root in VALID_MODES:
        if not is_admin:
            send_telegram_reply(chat_id, "⛔ Only the admin can change mode.")
            return
        if cmd_root == MODE.lower():
            send_telegram_reply(chat_id, f"ℹ️ Already on <b>{MODE}</b>.")
            return
        apply_mode(cmd_root)
        log.info("Mode switched to %s via Telegram by admin", MODE)
        send_telegram(f"⚙️ <b>Mode switched to {MODE}</b> by admin")

    elif cmd_root == "status":
        send_telegram_reply(chat_id, format_status())

    elif cmd_root == "pause":
        if not is_admin:
            send_telegram_reply(chat_id, "⛔ Only the admin can pause the scanner.")
            return
        if _paused:
            send_telegram_reply(chat_id, "ℹ️ Already paused.")
            return
        _paused = True
        log.info("Scanner paused via Telegram by admin")
        send_telegram("⏸ <b>Scanner paused</b> by admin")

    elif cmd_root == "resume":
        if not is_admin:
            send_telegram_reply(chat_id, "⛔ Only the admin can resume the scanner.")
            return
        if not _paused:
            send_telegram_reply(chat_id, "ℹ️ Already running.")
            return
        _paused = False
        log.info("Scanner resumed via Telegram by admin")
        send_telegram("▶ <b>Scanner resumed</b> by admin")

    elif cmd_root == "stats":
        send_telegram_reply(chat_id, format_stats())

    elif cmd_root == "help":
        send_telegram_reply(chat_id, format_help(is_admin))


def drain_telegram_updates() -> None:
    """Advance offset past any pending updates so messages sent before startup are ignored."""
    global _telegram_update_offset
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"timeout": 0},
            timeout=10,
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
        if updates:
            _telegram_update_offset = updates[-1]["update_id"] + 1
            log.info("Drained %d stale Telegram update(s) on startup", len(updates))
    except Exception as e:
        log.debug("Telegram drain error: %s", e)


def poll_telegram_commands() -> None:
    """Fetch new Telegram messages and dispatch any commands from allowed users."""
    global _telegram_update_offset
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _telegram_update_offset, "timeout": 0},
            timeout=10,
        )
        r.raise_for_status()
        for update in r.json().get("result", []):
            _telegram_update_offset = max(_telegram_update_offset, update["update_id"] + 1)
            msg = update.get("message") or update.get("edited_message")
            if not msg or "text" not in msg:
                continue
            from_id = msg["from"]["id"]
            chat_id = msg["chat"]["id"]
            if str(from_id) not in TELEGRAM_BROADCAST_IDS and str(from_id) != str(TELEGRAM_CHAT_ID):
                continue
            handle_command(msg["text"], from_id, chat_id)
    except Exception as e:
        log.debug("Telegram poll error: %s", e)


# ── Binance API ───────────────────────────────────────────────────────────────
def fetch_all_tickers() -> list[dict]:
    """Single API call — returns 24h stats for every USDT perp futures pair."""
    r = requests.get(f"{FUTURES_BASE}/fapi/v1/ticker/24hr", timeout=15)
    r.raise_for_status()
    return [t for t in r.json() if t["symbol"].endswith("USDT")]


def fetch_klines(symbol: str, interval: str) -> pd.DataFrame:
    r = requests.get(
        f"{FUTURES_BASE}/fapi/v1/klines",
        params={"symbol": symbol, "interval": interval, "limit": KLINE_LIMIT},
        timeout=15,
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_buy_base", "taker_buy_quote", "_",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ── Technical indicators ──────────────────────────────────────────────────────
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    gain = d.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-10))


def macd_hist(s: pd.Series, fast=12, slow=26, sig=9) -> pd.Series:
    line = ema(s, fast) - ema(s, slow)
    return line - ema(line, sig)


# ── Indicator helper ──────────────────────────────────────────────────────────
def compute_indicators(df: pd.DataFrame) -> dict:
    """
    Compute all indicators on df and return values at the last CLOSED candle (i=-2).
    Previous candle values (i=-3) are included for declining/rising checks.
    """
    c = df["close"]
    i = -2

    _e7  = ema(c, 7)
    _e25 = ema(c, 25)
    _e99 = ema(c, 99)
    _r6  = rsi(c, 6)
    _r12 = rsi(c, 12)
    _r24 = rsi(c, 24)
    _mh  = macd_hist(c)

    # 20-candle average volume, excluding the live (forming) candle
    avg_vol_20 = df["volume"].iloc[-22:-2].mean()

    return {
        "price":       c.iloc[i],
        "e7":          _e7.iloc[i],
        "e25":         _e25.iloc[i],
        "e99":         _e99.iloc[i],
        "r6":          _r6.iloc[i],
        "r6_prev":     _r6.iloc[i - 1],
        "r12":         _r12.iloc[i],
        "r24":         _r24.iloc[i],
        "mh":          _mh.iloc[i],
        "mh_prev":     _mh.iloc[i - 1],
        "candle_high": df["high"].iloc[i],
        "candle_low":  df["low"].iloc[i],
        "candle_vol":  df["volume"].iloc[i],
        "avg_vol_20":  avg_vol_20,
    }


# ── Signal logic ──────────────────────────────────────────────────────────────
def check_short_signal(df: pd.DataFrame, ticker: dict, symbol: str, tf: str) -> dict | None:
    """
    Returns a short signal dict when the coin matches a short/retrace setup, else None.

    Conditions (all must pass):
      1. RSI(6) >= RSI6_MIN              — overbought
      2. RSI(6) declining (v2)           — peak has passed
      3. Price within MAX_DIST_FROM_HIGH — at obvious resistance
      4. Recent pump >= MIN_RECENT_PUMP  — move confirmed on this TF
      5. EMA stack bullish               — overextended pump
      6. MACD histogram > 0             — momentum still present
      7. MACD histogram declining (v2)  — momentum rolling over
      8. Upper wick >= MIN_UPPER_WICK_RATIO (v2) — rejection candle
    """
    ind    = compute_indicators(df)
    c      = df["close"]
    price  = ind["price"]
    high24 = float(ticker["highPrice"])

    if ind["r6"] < RSI6_MIN:
        return None
    if REQUIRE_RSI_DECLINING and ind["r6"] >= ind["r6_prev"]:
        return None

    dist_pct = (high24 - price) / high24
    if dist_pct > MAX_DIST_FROM_HIGH:
        return None

    lookback   = min(10, len(c) - 2)
    past_price = c.iloc[-2 - lookback]
    recent_pump = (price - past_price) / past_price if past_price > 0 else 0
    if recent_pump < MIN_RECENT_PUMP:
        return None

    if REQUIRE_EMA_STACK and not (ind["e7"] > ind["e25"] > ind["e99"]):
        return None
    if REQUIRE_MACD_POS and ind["mh"] <= 0:
        return None
    if REQUIRE_MACD_DECLINING and ind["mh"] >= ind["mh_prev"]:
        return None

    candle_range = ind["candle_high"] - ind["candle_low"]
    wick_ratio   = (ind["candle_high"] - price) / candle_range if candle_range > 0 else 0.0
    if wick_ratio < MIN_UPPER_WICK_RATIO:
        return None

    stop_price = high24 * (1 + STOP_BUFFER_PCT)
    stop_pct   = (stop_price - price) / price * 100

    return {
        "direction":   "short",
        "symbol":      symbol,
        "timeframe":   tf,
        "price":       price,
        "high24":      high24,
        "change24":    float(ticker["priceChangePercent"]),
        "quote_vol":   float(ticker["quoteVolume"]),
        "dist_pct":    round(dist_pct * 100, 2),
        "recent_move": round(recent_pump * 100, 2),
        "rsi6":        round(ind["r6"], 2),
        "rsi12":       round(ind["r12"], 2),
        "rsi24":       round(ind["r24"], 2),
        "ema7":        round(ind["e7"], 8),
        "ema25":       round(ind["e25"], 8),
        "ema99":       round(ind["e99"], 8),
        "macd_hist":   round(ind["mh"], 8),
        "wick_ratio":  round(wick_ratio * 100, 1),
        "stop_price":  round(stop_price, 8),
        "stop_pct":    round(stop_pct, 2),
    }


def check_long_signal(df: pd.DataFrame, ticker: dict, symbol: str, tf: str) -> dict | None:
    """
    Returns a long signal dict when the coin matches a bounce setup, else None.

    Fixed conditions (always required):
      1. Price within MAX_DIST_FROM_LOW    — at obvious support
      2. Recent dump >= MIN_RECENT_DUMP    — move confirmed on this TF
      3. EMA stack bearish                 — sustained dump
      4. MACD histogram < 0               — momentum still down
      5. 24h change not worse than KNIFE_MAX_DROP — avoid news/exploit dumps
      6. Candle volume >= MIN_VOL_MULTIPLIER × 20c avg — buyers showed up

    Variable conditions (v1 loose / v2 strict):
      7. RSI(6) <= RSI6_MAX               — oversold
      8. RSI(6) rising (v2)               — bottom has passed
      9. MACD histogram rising (v2)       — momentum turning
     10. Lower wick >= MIN_LOWER_WICK_RATIO (v2) — hammer candle
    """
    ind      = compute_indicators(df)
    c        = df["close"]
    price    = ind["price"]
    low24    = float(ticker["lowPrice"])
    change24 = float(ticker["priceChangePercent"])

    # Knife filter — skip hard news/exploit dumps
    if change24 <= -(KNIFE_MAX_DROP * 100):
        return None

    dist_pct = (price - low24) / low24
    if dist_pct > MAX_DIST_FROM_LOW:
        return None

    lookback   = min(10, len(c) - 2)
    past_price = c.iloc[-2 - lookback]
    recent_dump = (past_price - price) / past_price if past_price > 0 else 0
    if recent_dump < MIN_RECENT_DUMP:
        return None

    if REQUIRE_EMA_BEAR and not (ind["e7"] < ind["e25"] < ind["e99"]):
        return None
    if REQUIRE_MACD_NEG and ind["mh"] >= 0:
        return None

    # Volume confirmation — real buyers must have stepped in
    if ind["avg_vol_20"] > 0 and ind["candle_vol"] < MIN_VOL_MULTIPLIER * ind["avg_vol_20"]:
        return None

    # Variable (v1/v2) conditions
    if ind["r6"] > RSI6_MAX:
        return None
    if REQUIRE_RSI_RISING and ind["r6"] <= ind["r6_prev"]:
        return None
    if REQUIRE_MACD_RISING and ind["mh"] <= ind["mh_prev"]:
        return None

    candle_range = ind["candle_high"] - ind["candle_low"]
    lower_wick   = (price - ind["candle_low"]) / candle_range if candle_range > 0 else 0.0
    if lower_wick < MIN_LOWER_WICK_RATIO:
        return None

    stop_price = low24 * (1 - STOP_BUFFER_PCT)
    stop_pct   = (price - stop_price) / price * 100
    vol_mult   = ind["candle_vol"] / ind["avg_vol_20"] if ind["avg_vol_20"] > 0 else 0.0

    return {
        "direction":   "long",
        "symbol":      symbol,
        "timeframe":   tf,
        "price":       price,
        "low24":       low24,
        "change24":    change24,
        "quote_vol":   float(ticker["quoteVolume"]),
        "dist_pct":    round(dist_pct * 100, 2),
        "recent_move": round(recent_dump * 100, 2),
        "rsi6":        round(ind["r6"], 2),
        "rsi12":       round(ind["r12"], 2),
        "rsi24":       round(ind["r24"], 2),
        "ema7":        round(ind["e7"], 8),
        "ema25":       round(ind["e25"], 8),
        "ema99":       round(ind["e99"], 8),
        "macd_hist":   round(ind["mh"], 8),
        "wick_ratio":  round(lower_wick * 100, 1),
        "vol_mult":    round(vol_mult, 2),
        "stop_price":  round(stop_price, 8),
        "stop_pct":    round(stop_pct, 2),
    }


def check_breakout_signal(df: pd.DataFrame, ticker: dict, symbol: str, tf: str) -> dict | None:
    """
    Returns a breakout-long signal dict when the coin matches a momentum setup, else None.

    Fixed conditions (always required):
      1. 24h change in [BREAKOUT_MIN_DAILY, BREAKOUT_MAX_DAILY] — meaningful but not exhausted
      2. Price > EMA25                                 — above the trend trigger
      3. EMA7 > EMA25                                  — fast EMA momentum positive
      4. MACD histogram > 0                            — momentum confirmed
      5. RSI(6) in [BREAKOUT_RSI_MIN, BREAKOUT_RSI_MAX) and rising
                                                       — buyers in control, not yet overbought
      6. Candle volume >= MIN_VOL_MULTIPLIER × 20c avg — real conviction
      7. Recent pump >= BREAKOUT_MIN_RECENT_PUMP       — breakout fresh on this TF
      8. Stop distance <= MAX_BREAKOUT_STOP_PCT         — skip late/wide-stop entries

    Variable conditions (v1 loose / v2 strict):
      9. EMA stack full bullish (EMA7>EMA25>EMA99)    — required in v2
     10. MACD histogram rising                        — required in v2
     11. RSI(6) >= 60 (instead of just >=50)          — enforced via BREAKOUT_RSI_MIN
    """
    ind      = compute_indicators(df)
    c        = df["close"]
    price    = ind["price"]
    change24 = float(ticker["priceChangePercent"])

    # Pre-filter conditions repeated here for safety (main pre-filter in run_scan)
    if not (BREAKOUT_MIN_DAILY <= change24 <= BREAKOUT_MAX_DAILY):
        return None

    # Fixed conditions
    if price <= ind["e25"]:
        return None
    if ind["e7"] <= ind["e25"]:
        return None
    if REQUIRE_BREAKOUT_FULL_STACK and not (ind["e7"] > ind["e25"] > ind["e99"]):
        return None
    if ind["mh"] <= 0:
        return None
    if REQUIRE_BREAKOUT_MACD_RISING and ind["mh"] <= ind["mh_prev"]:
        return None
    if not (BREAKOUT_RSI_MIN <= ind["r6"] < BREAKOUT_RSI_MAX):
        return None
    if ind["r6"] <= ind["r6_prev"]:
        return None
    if ind["avg_vol_20"] > 0 and ind["candle_vol"] < MIN_VOL_MULTIPLIER * ind["avg_vol_20"]:
        return None

    lookback    = min(10, len(c) - 2)
    past_price  = c.iloc[-2 - lookback]
    recent_pump = (price - past_price) / past_price if past_price > 0 else 0
    if recent_pump < BREAKOUT_MIN_RECENT_PUMP:
        return None

    stop_price = ind["e25"] * (1 - STOP_BUFFER_PCT)
    stop_pct   = (price - stop_price) / price * 100
    if stop_pct > MAX_BREAKOUT_STOP_PCT:
        return None

    vol_mult = ind["candle_vol"] / ind["avg_vol_20"] if ind["avg_vol_20"] > 0 else 0.0

    return {
        "direction":   "breakout",
        "symbol":      symbol,
        "timeframe":   tf,
        "price":       price,
        "change24":    change24,
        "quote_vol":   float(ticker["quoteVolume"]),
        "recent_move": round(recent_pump * 100, 2),
        "rsi6":        round(ind["r6"], 2),
        "rsi12":       round(ind["r12"], 2),
        "rsi24":       round(ind["r24"], 2),
        "ema7":        round(ind["e7"], 8),
        "ema25":       round(ind["e25"], 8),
        "ema99":       round(ind["e99"], 8),
        "macd_hist":   round(ind["mh"], 8),
        "vol_mult":    round(vol_mult, 2),
        "stop_price":  round(stop_price, 8),
        "stop_pct":    round(stop_pct, 2),
    }


# ── Alert formatters ──────────────────────────────────────────────────────────
def format_short_alert(s: dict) -> str:
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vol_m = s["quote_vol"] / 1_000_000
    return (
        f"🔴 <b>SHORT SETUP — {s['symbol']}</b>  [{s['timeframe']}]  mode:{MODE}\n"
        f"{ts}\n"
        f"\n"
        f"Price:       <b>{s['price']}</b>\n"
        f"24h High:    {s['high24']}  ({s['dist_pct']}% below — near top)\n"
        f"24h Change:  {s['change24']:+.2f}%   |  24h Vol: ${vol_m:.1f}M\n"
        f"Recent pump: +{s['recent_move']}%  (last 10 candles on {s['timeframe']})\n"
        f"\n"
        f"RSI(6):  <b>{s['rsi6']}</b>  |  RSI(12): {s['rsi12']}  |  RSI(24): {s['rsi24']}\n"
        f"EMA7:    {s['ema7']}\n"
        f"EMA25:   {s['ema25']}\n"
        f"EMA99:   {s['ema99']}\n"
        f"MACD:    {s['macd_hist']}\n"
        f"Wick:    {s['wick_ratio']}%  of candle range rejected by sellers\n"
        f"\n"
        f"🛑 Stop suggestion: above {s['stop_price']}  (+{s['stop_pct']:.2f}% from entry)\n"
        f"\n"
        f"⚠️ Coin pumped, near 24h high, EMAs stacked — potential retrace\n"
        f"Consider SHORT entry"
    )


def format_long_alert(s: dict) -> str:
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vol_m = s["quote_vol"] / 1_000_000
    return (
        f"🟢 <b>LONG SETUP — {s['symbol']}</b>  [{s['timeframe']}]  mode:{MODE}\n"
        f"{ts}\n"
        f"\n"
        f"Price:       <b>{s['price']}</b>\n"
        f"24h Low:     {s['low24']}  ({s['dist_pct']}% above — near bottom)\n"
        f"24h Change:  {s['change24']:+.2f}%   |  24h Vol: ${vol_m:.1f}M\n"
        f"Recent dump: -{s['recent_move']}%  (last 10 candles on {s['timeframe']})\n"
        f"\n"
        f"RSI(6):  <b>{s['rsi6']}</b>  |  RSI(12): {s['rsi12']}  |  RSI(24): {s['rsi24']}\n"
        f"EMA7:    {s['ema7']}\n"
        f"EMA25:   {s['ema25']}\n"
        f"EMA99:   {s['ema99']}\n"
        f"MACD:    {s['macd_hist']}\n"
        f"Wick:    {s['wick_ratio']}%  of candle range rejected by buyers (hammer)\n"
        f"Volume:  {s['vol_mult']}×  20-candle average\n"
        f"\n"
        f"🛑 Stop suggestion: below {s['stop_price']}  (-{s['stop_pct']:.2f}% from entry)\n"
        f"\n"
        f"⚠️ Coin dumped, near 24h low, EMAs bearish — potential bounce\n"
        f"Consider LONG entry"
    )


def format_breakout_alert(s: dict) -> str:
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vol_m = s["quote_vol"] / 1_000_000
    return (
        f"🚀 <b>BREAKOUT LONG — {s['symbol']}</b>  [{s['timeframe']}]  mode:{MODE}\n"
        f"{ts}\n"
        f"\n"
        f"Price:       <b>{s['price']}</b>\n"
        f"24h Change:  +{s['change24']:.2f}%   |  24h Vol: ${vol_m:.1f}M\n"
        f"Recent pump: +{s['recent_move']}%  (last 10 candles on {s['timeframe']})\n"
        f"\n"
        f"RSI(6):  <b>{s['rsi6']}</b>  |  RSI(12): {s['rsi12']}  |  RSI(24): {s['rsi24']}\n"
        f"EMA7:    {s['ema7']}\n"
        f"EMA25:   {s['ema25']}  ← stop trigger\n"
        f"EMA99:   {s['ema99']}\n"
        f"MACD:    {s['macd_hist']}\n"
        f"Volume:  {s['vol_mult']}×  20-candle average\n"
        f"\n"
        f"🛑 Stop suggestion: below {s['stop_price']}  (-{s['stop_pct']:.2f}% from entry)\n"
        f"\n"
        f"⚠️ Momentum shift confirmed: above EMA25, rising RSI, volume conviction\n"
        f"Consider LONG entry (breakout — momentum trade, not bounce trade)"
    )


def format_alert(s: dict) -> str:
    if s["direction"] == "breakout":
        return format_breakout_alert(s)
    if s["direction"] == "long":
        return format_long_alert(s)
    return format_short_alert(s)


# ── Scan cycle ────────────────────────────────────────────────────────────────
def run_scan(tickers: list[dict]) -> int:
    """Returns number of alerts sent this cycle."""
    now = time.time()
    _process_pending_checkups(tickers, now)

    short_candidates:    list[dict] = []
    long_candidates:     list[dict] = []
    breakout_candidates: list[dict] = []

    for t in tickers:
        try:
            price     = float(t["lastPrice"])
            high24    = float(t["highPrice"])
            low24     = float(t["lowPrice"])
            quote_vol = float(t["quoteVolume"])
            change24  = float(t["priceChangePercent"])
            if quote_vol < MIN_24H_QUOTE_VOL:
                continue
            if high24 <= 0 or low24 <= 0:
                continue
            if SHORTS_ENABLED and (high24 - price) / high24 <= MAX_DIST_FROM_HIGH:
                short_candidates.append(t)
            if LONGS_ENABLED and change24 > -(KNIFE_MAX_DROP * 100):
                if (price - low24) / low24 <= MAX_DIST_FROM_LOW:
                    long_candidates.append(t)
            if BREAKOUTS_ENABLED and BREAKOUT_MIN_DAILY <= change24 <= BREAKOUT_MAX_DAILY:
                breakout_candidates.append(t)
        except Exception:
            pass

    if SHORTS_ENABLED:
        log.info(
            "Short    pre-filter: %d/%d coins have >$%.1fM vol and are within %d%% of 24h high",
            len(short_candidates), len(tickers),
            MIN_24H_QUOTE_VOL / 1_000_000, int(MAX_DIST_FROM_HIGH * 100),
        )
    if LONGS_ENABLED:
        log.info(
            "Long     pre-filter: %d/%d coins have >$%.1fM vol and are within %d%% of 24h low (knife-filtered)",
            len(long_candidates), len(tickers),
            MIN_24H_QUOTE_VOL / 1_000_000, int(MAX_DIST_FROM_LOW * 100),
        )
    if BREAKOUTS_ENABLED:
        log.info(
            "Breakout pre-filter: %d/%d coins up %.0f–%.0f%% on day",
            len(breakout_candidates), len(tickers),
            BREAKOUT_MIN_DAILY, BREAKOUT_MAX_DAILY,
        )

    signals = 0

    def _scan_candidates(candidates: list[dict], check_fn, direction: str) -> None:
        nonlocal signals
        for ticker in candidates:
            symbol = ticker["symbol"]
            for tf in TIMEFRAMES:
                key = f"{symbol}:{tf}:{direction}"
                if now - _alerted.get(key, 0) < ALERT_COOLDOWN_SEC:
                    continue
                try:
                    df = fetch_klines(symbol, tf)
                    if len(df) < 50:
                        continue
                    sig = check_fn(df, ticker, symbol, tf)
                    if sig:
                        if direction == "short":
                            log.info(
                                "SHORT  %-15s [%3s]  RSI6=%-5.1f  pump=+%.1f%%  %.2f%% from high  wick=%.0f%%",
                                symbol, tf, sig["rsi6"], sig["recent_move"],
                                sig["dist_pct"], sig["wick_ratio"],
                            )
                        elif direction == "long":
                            log.info(
                                "LONG   %-15s [%3s]  RSI6=%-5.1f  dump=-%.1f%%  %.2f%% from low   wick=%.0f%%  vol=%.1fx",
                                symbol, tf, sig["rsi6"], sig["recent_move"],
                                sig["dist_pct"], sig["wick_ratio"], sig["vol_mult"],
                            )
                        else:  # breakout
                            log.info(
                                "BREAK  %-15s [%3s]  RSI6=%-5.1f  pump=+%.1f%%  vol=%.1fx  stop=-%.1f%%",
                                symbol, tf, sig["rsi6"], sig["recent_move"],
                                sig["vol_mult"], sig["stop_pct"],
                            )
                        send_telegram(format_alert(sig))
                        track_alert(sig)
                        _alerted[key] = now
                        signals += 1
                except Exception as e:
                    log.debug("Error %s %s: %s", symbol, tf, e)
                time.sleep(0.1)  # gentle rate limiting

    _scan_candidates(short_candidates,    check_short_signal,    "short")
    _scan_candidates(long_candidates,     check_long_signal,     "long")
    _scan_candidates(breakout_candidates, check_breakout_signal, "breakout")

    log.info("Scan complete — %d alert(s) sent", signals)
    return signals


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    global _total_scans, _total_alerts, _last_heartbeat

    log.info("=" * 60)
    log.info("Binance Perp Futures Short & Long Scanner")
    log.info("Timeframes : %s", TIMEFRAMES)
    log.info("Mode       : %s  (shorts=%s, longs=%s, breakouts=%s)",
             MODE, SHORTS_ENABLED, LONGS_ENABLED, BREAKOUTS_ENABLED)
    log.info("Filter     : 24h vol > $%.1fM", MIN_24H_QUOTE_VOL / 1_000_000)
    if SHORTS_ENABLED:
        log.info("Short      : within %d%% of 24h high  RSI(6)>=%d  MACD-dec=%s  RSI-dec=%s  wick>=%.0f%%",
                 int(MAX_DIST_FROM_HIGH * 100), RSI6_MIN,
                 REQUIRE_MACD_DECLINING, REQUIRE_RSI_DECLINING, MIN_UPPER_WICK_RATIO * 100)
    if LONGS_ENABLED:
        log.info("Long       : within %d%% of 24h low   RSI(6)<=%d  MACD-rise=%s  RSI-rise=%s  wick>=%.0f%%  vol>=%.1fx  knife<=%d%%",
                 int(MAX_DIST_FROM_LOW * 100), RSI6_MAX,
                 REQUIRE_MACD_RISING, REQUIRE_RSI_RISING, MIN_LOWER_WICK_RATIO * 100,
                 MIN_VOL_MULTIPLIER, int(KNIFE_MAX_DROP * 100))
    if BREAKOUTS_ENABLED:
        log.info("Breakout   : 24h change %.0f–%.0f%%  RSI(6) %d–%d rising  MACD-rise=%s  full-stack=%s  vol>=%.1fx  max-stop=%.1f%%",
                 BREAKOUT_MIN_DAILY, BREAKOUT_MAX_DAILY,
                 BREAKOUT_RSI_MIN, BREAKOUT_RSI_MAX,
                 REQUIRE_BREAKOUT_MACD_RISING, REQUIRE_BREAKOUT_FULL_STACK,
                 MIN_VOL_MULTIPLIER, MAX_BREAKOUT_STOP_PCT)
    log.info("=" * 60)

    load_state()
    smoke_test()
    _last_heartbeat = time.time()
    drain_telegram_updates()  # skip any backlog from before startup

    while True:
        if _paused:
            log.info("Scanner paused — skipping scan cycle")
        else:
            try:
                tickers = fetch_all_tickers()
                alerts = run_scan(tickers)
                _total_scans  += 1
                _total_alerts += alerts
            except Exception as e:
                log.error("Scan cycle error: %s", e)

        # Hourly heartbeat
        now = time.time()
        if now - _last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            send_telegram(
                f"🟢 <b>Scanner alive</b> — [{ts}]\n"
                f"Mode:               <b>{MODE}</b>\n"
                f"Scans this session: {_total_scans}\n"
                f"Alerts sent:        {_total_alerts}\n"
                f"Next scan in {SCAN_INTERVAL_SEC}s"
            )
            _last_heartbeat = now

        # Poll Telegram for commands every COMMAND_POLL_INTERVAL_SEC until next scan
        log.info("Waiting %ds before next scan (polling for commands)...", SCAN_INTERVAL_SEC)
        deadline = time.time() + SCAN_INTERVAL_SEC
        while time.time() < deadline:
            poll_telegram_commands()
            time.sleep(min(COMMAND_POLL_INTERVAL_SEC, max(0.1, deadline - time.time())))


if __name__ == "__main__":
    main()
