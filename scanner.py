#!/usr/bin/env python3
"""
Binance Futures Short Scanner
-------------------------------
Automates the manual workflow of finding short setups:
  1. Fetches all USDT perp futures tickers
  2. Pre-filters to liquid coins (>$5M 24h vol) that are still near their 24h high
  3. Runs EMA(7/25/99), RSI(6/12/24), MACD on 15m and 1h charts
  4. Sends a Telegram alert when the chart matches a short/retrace setup

Setup: copy .env.example to .env and fill in your Telegram credentials.
Run:   python scanner.py
"""

import os
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Telegram credentials (set in .env) ───────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Binance Futures base URL ──────────────────────────────────────────────────
FUTURES_BASE = "https://fapi.binance.com"

# ── Timeframes to scan ────────────────────────────────────────────────────────
TIMEFRAMES = ["15m", "1h"]

# ── Strategy preset ───────────────────────────────────────────────────────────
# "v1" = original behaviour (looser, more alerts)
# "v2" = stricter reversal confirmation (fewer, higher-quality alerts)
STRATEGY = "v2"

# ── Strategy thresholds (adjust to taste) ────────────────────────────────────
MIN_24H_QUOTE_VOL  = 5_000_000  # Only scan coins with >$5M USDT 24h volume (liquidity filter)
MAX_DIST_FROM_HIGH = 0.05       # Price must be within 5% of 24h high
MIN_RECENT_PUMP    = 0.05       # Price must have gained >=5% in the last 10 candles (recent pump)
REQUIRE_EMA_STACK  = True       # EMA7 > EMA25 > EMA99 (stacked bullish = overextended pump)
REQUIRE_MACD_POS   = True       # MACD histogram must be positive

if STRATEGY == "v1":
    RSI6_MIN               = 65    # Elevated RSI threshold (original)
    REQUIRE_MACD_DECLINING = False  # Don't require histogram to be rolling over
    REQUIRE_RSI_DECLINING  = False  # Don't require RSI to be turning down
    MIN_UPPER_WICK_RATIO   = 0.0   # No wick filter
else:  # v2
    RSI6_MIN               = 70    # Clearly overbought before alerting
    REQUIRE_MACD_DECLINING = True   # Histogram must be falling (momentum rolling over)
    REQUIRE_RSI_DECLINING  = True   # RSI(6) must be turning down (peak passed)
    MIN_UPPER_WICK_RATIO   = 0.35  # Close must be in bottom 65% of candle range

# ── Scanner behaviour ─────────────────────────────────────────────────────────
SCAN_INTERVAL_SEC  = 60    # Full scan every 60 seconds
ALERT_COOLDOWN_SEC = 3600  # Don't re-alert same coin+timeframe for 1 hour
KLINE_LIMIT        = 150   # Candles fetched per symbol per timeframe

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Key: "SYMBOL:timeframe" → unix timestamp of last alert
_alerted: dict[str, float] = {}

# Session-level counters for heartbeat
_total_scans  = 0
_total_alerts = 0
_last_heartbeat: float = 0.0
HEARTBEAT_INTERVAL_SEC = 3600


# ── Smoke test ────────────────────────────────────────────────────────────────
def smoke_test() -> None:
    """Verify Binance and Telegram are reachable before entering the main loop."""
    log.info("Running startup checks...")

    # Binance reachability
    try:
        r = requests.get(f"{FUTURES_BASE}/fapi/v1/ticker/price?symbol=BTCUSDT", timeout=10)
        r.raise_for_status()
        log.info("Binance API  ✓  (BTC price: %s)", r.json().get("price", "?"))
    except Exception as e:
        log.error("Binance API unreachable: %s — cannot run scanner. Check network.", e)
        raise SystemExit(1)

    # Telegram credentials
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — alerts will print to console only")
        return

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "✅ Scanner started — watching Binance perp futures for short setups",
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        r.raise_for_status()
        log.info("Telegram     ✓  (startup message sent)")
    except Exception as e:
        log.error("Telegram failed: %s — check TELEGRAM_TOKEN and TELEGRAM_CHAT_ID", e)


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"\n{'─'*60}\n{text}\n{'─'*60}\n")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram failed: %s", e)


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


# ── Signal logic ──────────────────────────────────────────────────────────────
def check_signal(df: pd.DataFrame, ticker: dict, symbol: str, tf: str) -> dict | None:
    """
    Returns a signal dict when the coin matches a short/retrace setup, else None.

    Conditions (all must pass):
      1. RSI(6) >= RSI6_MIN          — overbought or elevated
      2. Price within 5% of 24h high — at obvious resistance, hasn't retraced yet
      3. Recent pump >= MIN_RECENT_PUMP — price gained on the chart timeframe itself
      4. EMA(7) > EMA(25) > EMA(99)  — pump confirmed, all EMAs stacked bullish
      5. MACD histogram > 0          — momentum still up (about to turn)
    """
    c = df["close"]

    _e7   = ema(c, 7)
    _e25  = ema(c, 25)
    _e99  = ema(c, 99)
    _r6   = rsi(c, 6)
    _r12  = rsi(c, 12)
    _r24  = rsi(c, 24)
    _mh   = macd_hist(c)

    # Use last CLOSED candle (index -2) — avoids false signals on live forming candle
    i = -2
    price    = c.iloc[i]
    e7       = _e7.iloc[i]
    e25      = _e25.iloc[i]
    e99      = _e99.iloc[i]
    r6       = _r6.iloc[i]
    r6_prev  = _r6.iloc[i - 1]
    r12      = _r12.iloc[i]
    r24      = _r24.iloc[i]
    mh       = _mh.iloc[i]
    mh_prev  = _mh.iloc[i - 1]
    high24   = float(ticker["highPrice"])

    if r6 < RSI6_MIN:
        return None

    # RSI(6) must be turning down — peak has passed
    if REQUIRE_RSI_DECLINING and r6 >= r6_prev:
        return None

    dist_pct = (high24 - price) / high24
    if dist_pct > MAX_DIST_FROM_HIGH:
        return None

    # Recent pump on this chart timeframe (last 10 closed candles)
    lookback = min(10, len(c) - 2)
    past_price = c.iloc[i - lookback]
    recent_pump = (price - past_price) / past_price if past_price > 0 else 0
    if recent_pump < MIN_RECENT_PUMP:
        return None

    if REQUIRE_EMA_STACK and not (e7 > e25 > e99):
        return None

    if REQUIRE_MACD_POS and mh <= 0:
        return None

    # MACD histogram must be declining — momentum rolling over, not still accelerating
    if REQUIRE_MACD_DECLINING and mh >= mh_prev:
        return None

    # Upper wick filter — close must be in lower portion of candle range (rejection candle)
    candle_high  = df["high"].iloc[i]
    candle_low   = df["low"].iloc[i]
    candle_range = candle_high - candle_low
    wick_ratio   = (candle_high - price) / candle_range if candle_range > 0 else 0.0
    if wick_ratio < MIN_UPPER_WICK_RATIO:
        return None

    return {
        "symbol":      symbol,
        "timeframe":   tf,
        "price":       price,
        "high24":      high24,
        "change24":    float(ticker["priceChangePercent"]),
        "quote_vol":   float(ticker["quoteVolume"]),
        "dist_pct":    round(dist_pct * 100, 2),
        "recent_pump": round(recent_pump * 100, 2),
        "rsi6":        round(r6, 2),
        "rsi12":       round(r12, 2),
        "rsi24":       round(r24, 2),
        "ema7":        round(e7, 8),
        "ema25":       round(e25, 8),
        "ema99":       round(e99, 8),
        "macd_hist":   round(mh, 8),
        "wick_ratio":  round(wick_ratio * 100, 1),
    }


def format_alert(s: dict) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    vol_m = s["quote_vol"] / 1_000_000
    return (
        f"🔴 <b>SHORT SETUP — {s['symbol']}</b>  [{s['timeframe']}]\n"
        f"{ts}\n"
        f"\n"
        f"Price:      <b>{s['price']}</b>\n"
        f"24h High:   {s['high24']}  ({s['dist_pct']}% below — near top)\n"
        f"24h Change: {s['change24']:+.2f}%   |  24h Vol: ${vol_m:.1f}M\n"
        f"Recent pump: +{s['recent_pump']}% (last 10 candles on {s['timeframe']})\n"
        f"\n"
        f"RSI(6):  <b>{s['rsi6']}</b>  |  RSI(12): {s['rsi12']}  |  RSI(24): {s['rsi24']}\n"
        f"EMA7:    {s['ema7']}\n"
        f"EMA25:   {s['ema25']}\n"
        f"EMA99:   {s['ema99']}\n"
        f"MACD:    {s['macd_hist']}\n"
        f"Wick:    {s['wick_ratio']}%  of candle range rejected by sellers\n"
        f"\n"
        f"⚠️ Coin is pumping, near 24h high, EMAs stacked — potential retrace incoming\n"
        f"Consider SHORT entry"
    )


# ── Scan cycle ────────────────────────────────────────────────────────────────
def run_scan(tickers: list[dict]) -> int:
    """Returns number of alerts sent this cycle."""
    now = time.time()

    # Step 1: fast pre-filter using ticker data only (no extra API calls)
    # Keeps any liquid coin sitting near its 24h high, regardless of daily gain.
    candidates = []
    for t in tickers:
        try:
            price     = float(t["lastPrice"])
            high24    = float(t["highPrice"])
            quote_vol = float(t["quoteVolume"])
            if quote_vol < MIN_24H_QUOTE_VOL:
                continue
            if high24 <= 0:
                continue
            if (high24 - price) / high24 > MAX_DIST_FROM_HIGH:
                continue
            candidates.append(t)
        except Exception:
            pass

    log.info(
        "Pre-filter: %d/%d coins have >$%.1fM vol and are within %d%% of 24h high",
        len(candidates), len(tickers), MIN_24H_QUOTE_VOL / 1_000_000,
        int(MAX_DIST_FROM_HIGH * 100),
    )

    # Step 2: detailed chart analysis on candidates only
    signals = 0
    for ticker in candidates:
        symbol = ticker["symbol"]
        for tf in TIMEFRAMES:
            key = f"{symbol}:{tf}"
            if now - _alerted.get(key, 0) < ALERT_COOLDOWN_SEC:
                continue
            try:
                df = fetch_klines(symbol, tf)
                if len(df) < 50:
                    continue
                sig = check_signal(df, ticker, symbol, tf)
                if sig:
                    log.info(
                        "SIGNAL  %-15s [%3s]  RSI6=%-5.1f  pump=+%.1f%%  %.2f%% from high  wick=%.0f%%",
                        symbol, tf, sig["rsi6"], sig["recent_pump"], sig["dist_pct"], sig["wick_ratio"],
                    )
                    send_telegram(format_alert(sig))
                    _alerted[key] = now
                    signals += 1
            except Exception as e:
                log.debug("Error %s %s: %s", symbol, tf, e)
            time.sleep(0.1)  # gentle rate limiting

    log.info("Scan complete — %d alert(s) sent", signals)
    return signals


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    global _total_scans, _total_alerts, _last_heartbeat

    log.info("=" * 60)
    log.info("Binance Perp Futures Short Scanner")
    log.info("Timeframes : %s", TIMEFRAMES)
    log.info("Filter     : 24h vol > $%.1fM,  within %d%% of 24h high",
             MIN_24H_QUOTE_VOL / 1_000_000, int(MAX_DIST_FROM_HIGH * 100))
    log.info("Strategy   : %s", STRATEGY)
    log.info("Signal     : RSI(6) >= %d,  recent pump >= %d%%,  EMA stack,  MACD+",
             RSI6_MIN, int(MIN_RECENT_PUMP * 100))
    log.info("           : MACD declining=%s,  RSI declining=%s,  min wick=%.0f%%",
             REQUIRE_MACD_DECLINING, REQUIRE_RSI_DECLINING, MIN_UPPER_WICK_RATIO * 100)
    log.info("=" * 60)

    smoke_test()
    _last_heartbeat = time.time()

    while True:
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
                f"Scans this session: {_total_scans}\n"
                f"Alerts sent:        {_total_alerts}\n"
                f"Next scan in {SCAN_INTERVAL_SEC}s"
            )
            _last_heartbeat = now

        log.info("Waiting %ds before next scan...", SCAN_INTERVAL_SEC)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
