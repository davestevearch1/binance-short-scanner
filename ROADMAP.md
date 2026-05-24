# Roadmap — Future Features

## Phase 2 — Signal Quality

- **LLM signal scoring**: After a signal fires, call Claude with the indicator snapshot and rate confidence 1–10 with key risk factors. Only alert if score ≥ 7. Implement as `score_signal(sig: dict) -> int` in a new `ai_filter.py` module. Adds ~$0.001/alert.
- **Volume spike filter**: Check last-candle volume is ≥ 1.5× the 20-candle average — filters low-conviction pumps.
- **Funding rate annotation**: Cross-reference `/fapi/v1/fundingRate` — very high funding (>0.1%) on a pump increases retrace probability. Add as optional alert detail.

## Phase 3 — Persistence & Analytics

- **SQLite logging**: Record every signal to `signals.db` (symbol, timeframe, timestamp, all indicator values, alert sent). Enables backtesting and false-positive analysis.
- **Win/loss tracking**: `/win` and `/loss` Telegram bot commands to tag outcomes. Store in the same DB.
- **Weekly summary**: Every Sunday 08:00 UTC, send a Telegram message with signal count, win/loss ratio, and top performers.

## Phase 4 — Real-time & Performance

- **Binance WebSocket streams**: Replace 60s poll with `wss://fstream.binance.com/ws/!miniTicker@arr` live feed. Reduces API calls by ~95%. Trigger chart fetch only when a ticker crosses the pre-filter threshold.
- **Async I/O**: Rewrite `fetch_klines` calls using `asyncio` + `aiohttp` to scan all candidates in parallel. Current bottleneck is serial calls with 100ms sleep.

## Phase 5 — UI & Access

- **Web dashboard**: FastAPI app showing live signals, historical log, and adjustable thresholds. Deploy alongside the scanner on the same VPS.
- **Telegram slash commands**: `/status`, `/pause`, `/resume`, `/thresholds` — control the scanner from Telegram without SSH.
- **Multi-user support**: Serve multiple Telegram chat IDs from one bot instance (store list in `.env` or DB).
