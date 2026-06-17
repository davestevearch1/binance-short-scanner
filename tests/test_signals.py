"""Network-free unit tests for the breakdown short, daily-% windows, and regime.

Run with:  python -m tests.test_signals     (from the repo root)
or:        python tests/test_signals.py

Synthetic OHLCV DataFrames are crafted so the strategy functions exercise their
real code paths without touching Binance. The breakdown/downtrend parameters
(d=0.012, u=0.005) were chosen by grid-search so every gate passes with margin.
"""
import math
import os
import sys
import tempfile
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scanner  # noqa: E402

FIXED_END_MS = 1_750_000_000_000


def _frame(closes, tf="1h", *, highs=None, lows=None, vol_boost_at=-2,
           vol_base=1000.0, vol_mult=3.0, end_ms=FIXED_END_MS):
    """Build a klines DataFrame matching fetch_klines' columns/dtypes."""
    n    = len(closes)
    step = scanner._TF_MINUTES[tf] * 60 * 1000
    ot   = [end_ms - (n - 1 - i) * step for i in range(n)]
    highs = highs if highs is not None else [c * 1.002 for c in closes]
    lows  = lows  if lows  is not None else [c * 0.998 for c in closes]
    df = pd.DataFrame({
        "open_time": ot, "open": list(closes), "high": highs, "low": lows,
        "close": list(closes), "volume": [vol_base] * n,
        "close_time": [t + step for t in ot],
        "quote_vol": [0] * n, "trades": [0] * n,
        "taker_buy_base": [0] * n, "taker_buy_quote": [0] * n, "_": [0] * n,
    })
    df.loc[df.index[vol_boost_at], "volume"] = vol_base * vol_mult
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df


def _downtrend(d=0.012, u=0.005, end_ms=FIXED_END_MS):
    """A choppy downtrend that fires the breakdown short (all gates pass)."""
    closes, p = [], 100.0
    for i in range(138):
        p *= (1 - 0.0012)
        closes.append(p * (1 + 0.006 * math.sin(i / 3.0)))
    p = closes[-1]
    for r in [-d, +u, -d, +u, -d, +u, -d, +u, -d, +u, -d, -d]:
        p *= (1 + r)
        closes.append(p)
    return _frame(closes[:150], "1h", end_ms=end_ms)


def _uptrend(end_ms=FIXED_END_MS):
    closes, p = [], 50.0
    for _ in range(150):
        p *= 1.003
        closes.append(p)
    return _frame(closes, "1h", end_ms=end_ms)


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_breakdown_fires_on_downtrend_and_fade_does_not():
    df = _downtrend()
    price = df["close"].iloc[-2]
    ticker = {"priceChangePercent": "-12.0", "quoteVolume": "50000000",
              "highPrice": "120", "lowPrice": "80", "lastPrice": str(price)}
    bd = scanner.check_breakdown_signal(df, ticker, "TESTUSDT", "1h")
    assert bd is not None, "breakdown should fire on a clean downtrend"
    assert bd["direction"] == "breakdown"
    assert bd["stop_price"] > bd["price"], "short stop must sit ABOVE entry"
    # The fade-short must NOT fire on a downtrend (needs bullish stack near 24h high).
    fade = scanner.check_short_signal(df, ticker, "TESTUSDT", "1h")
    assert fade is None, "fade-short must not fire on a downtrend"
    # The new daily-% keys must be present on the signal.
    for k in ("change_2h", "change_utc", "blowoff"):
        assert k in bd, f"missing daily-% key {k}"


def test_breakdown_none_on_uptrend():
    df = _uptrend()
    ticker = {"priceChangePercent": "+12.0", "quoteVolume": "50000000",
              "highPrice": "120", "lowPrice": "40", "lastPrice": "1"}
    assert scanner.check_breakdown_signal(df, ticker, "T", "1h") is None, \
        "breakdown must not fire when the coin is up on the day"


def test_change_windows_2h_matches_independent_calc():
    for tf, n2h in [("1h", 2), ("15m", 8)]:
        df = _downtrend()  # candle spacing comes from tf in _change_windows
        price = float(df["close"].iloc[-2])
        base = float(df["close"].iloc[-2 - n2h])
        expected = (price - base) / base * 100
        out = scanner._change_windows(df, tf, price, change24=-12.0)
        assert abs(out["change_2h"] - round(expected, 2)) < 0.01, \
            f"{tf}: {out['change_2h']} != {expected}"


def test_change_windows_utc_matches_independent_calc():
    # Anchor candles to the real current day so a candle sits at/after 00:00 UTC.
    import time
    df = _downtrend(end_ms=int(time.time() * 1000))
    price = float(df["close"].iloc[-2])
    now = datetime.now(timezone.utc)
    midnight_ms = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000)
    mask = df["open_time"].astype("int64") >= midnight_ms
    assert bool(mask.any()), "fixture should contain a candle since 00:00 UTC"
    day_open = float(df["open"].iloc[int(mask.values.argmax())])
    expected = (price - day_open) / day_open * 100
    out = scanner._change_windows(df, "1h", price, change24=-12.0)
    assert abs(out["change_utc"] - round(expected, 2)) < 0.01


def test_blowoff_flag_boundary():
    df = _uptrend()
    price = float(df["close"].iloc[-2])
    assert scanner._change_windows(df, "1h", price, change24=150.0)["blowoff"] is True
    assert scanner._change_windows(df, "1h", price, change24=50.0)["blowoff"] is False


def test_apply_mode_wiring():
    scanner.apply_mode("short")
    assert scanner.SHORTS_ENABLED and scanner.BREAKDOWNS_ENABLED
    assert not scanner.LONGS_ENABLED and not scanner.BREAKOUTS_ENABLED
    scanner.apply_mode("long")
    assert not scanner.SHORTS_ENABLED and not scanner.BREAKDOWNS_ENABLED
    assert scanner.LONGS_ENABLED and scanner.BREAKOUTS_ENABLED
    scanner.apply_mode("both")
    assert all([scanner.SHORTS_ENABLED, scanner.BREAKDOWNS_ENABLED,
                scanner.LONGS_ENABLED, scanner.BREAKOUTS_ENABLED])
    scanner.apply_mode(scanner.DEFAULT_MODE)


def _make_tracked(direction, entry, stop, alerted=False):
    return {
        "symbol": "T", "timeframe": "1h", "direction": direction, "variant": "v2",
        "mode": "both", "alerted": alerted, "entry": entry, "stop": stop,
        "ts": scanner.time.time() - (scanner.TRACKER_CHECKUP_2_SEC + 10),
        "extreme_seen": entry, "favor_seen": entry, "checks_done": 1, "snapshot": {},
    }


def test_breakdown_treated_as_short_in_tracker():
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    scanner.STATE_FILE = tmp
    # Win: price falls below entry → good for a short-side trade.
    scanner._tracked[:] = [_make_tracked("breakdown", entry=100.0, stop=105.0)]
    scanner._results[:] = []
    scanner._process_pending_checkups([{"symbol": "T", "lastPrice": "95"}], scanner.time.time())
    assert len(scanner._results) == 1
    r = scanner._results[0]
    assert r["direction"] == "breakdown"
    assert r["outcome"] == "win"
    assert r["final_pct"] == 5.0          # price fell 5% → +5% for a short
    assert r["mfe"] == 5.0 and r["mae"] == 0.0
    # Loss via stop hit: price rises above the (above-entry) stop.
    scanner._tracked[:] = [_make_tracked("breakdown", entry=100.0, stop=105.0)]
    scanner._results[:] = []
    scanner._process_pending_checkups([{"symbol": "T", "lastPrice": "106"}], scanner.time.time())
    assert scanner._results[0]["outcome"] == "loss"
    scanner._tracked[:] = []
    scanner._results[:] = []
    os.remove(tmp)


def _liquid_tickers(changes):
    return [{
        "symbol": f"C{i}USDT", "lastPrice": "100", "highPrice": "130",
        "lowPrice": "70", "quoteVolume": "10000000", "priceChangePercent": str(c),
    } for i, c in enumerate(changes)]


def test_fade_daily_floor_gate():
    # The fade-short floor rejects early when the coin isn't up enough on the day.
    df = _downtrend()  # any frame; the floor check runs before the other gates
    price = float(df["close"].iloc[-2])
    ticker = {"priceChangePercent": "10.0", "quoteVolume": "50000000",
              "highPrice": str(price * 1.01), "lowPrice": "1", "lastPrice": str(price)}
    _orig = scanner.FADE_MIN_DAILY_CHANGE
    try:
        scanner.FADE_MIN_DAILY_CHANGE = 20.0  # require >=20% up; ticker is only +10%
        assert scanner.check_short_signal(df, ticker, "T", "1h") is None
    finally:
        scanner.FADE_MIN_DAILY_CHANGE = _orig


def test_regime_quiet_classification():
    # Stub klines so any candidate scan returns immediately (len<50 → skip), no net.
    _orig_klines = scanner.fetch_klines
    _orig_sleep  = scanner.time.sleep
    scanner.fetch_klines = lambda *a, **k: pd.DataFrame({"close": [1.0] * 5})
    scanner.time.sleep = lambda *a, **k: None
    try:
        scanner.apply_mode("both")
        # Quiet market: small moves → avg|24h| ~1.2% < 3% threshold.
        scanner.run_scan(_liquid_tickers([1, -1.5, 2, -0.5, 1.2, -1.8, 0.9, -1.1, 1.4, -0.7]))
        assert scanner._regime["quiet"] is True
        assert scanner._regime["n"] == 10
        # Lively market: scale up 6x → avg|24h| ~7% > 3%.
        scanner.run_scan(_liquid_tickers([6, -9, 12, -3, 7.2, -10.8, 5.4, -6.6, 8.4, -4.2]))
        assert scanner._regime["quiet"] is False
    finally:
        scanner.fetch_klines = _orig_klines
        scanner.time.sleep = _orig_sleep
        scanner.apply_mode(scanner.DEFAULT_MODE)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
