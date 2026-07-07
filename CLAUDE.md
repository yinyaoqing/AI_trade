# AI Trade — Shioaji Trading Project

## Project Overview

This project uses [Shioaji](https://sinotrade.github.io/) — Taiwan's most popular trading API by SinoTrade — to build an AI-driven trading system supporting stocks, futures, and options on TWSE/OTC markets.

## Environment Setup

### Install Python (if not installed)
```bash
# Option 1: winget (Windows)
winget install Python.Python.3.12

# Option 2: uv (fast Python package manager)
winget install astral-sh.uv
uv python install 3.12
```

### Install dependencies
```bash
# Recommended (installs all deps including pandas, pandas-ta, yfinance)
uv sync

# Or with pip
pip install shioaji python-dotenv pandas pandas-ta yfinance openai requests feedparser beautifulsoup4
```

### Credentials
Set in `.env` file (git-ignored). Never commit credentials:
```env
API_KEY=your_api_key
SECRET_KEY=your_secret_key
CA_CERT_PATH=C:\path\to\cert.pfx
CA_PASSWORD=your_cert_password
OPENAI_API_KEY=sk-...          # Only needed when SENTIMENT_ENABLED=True
TELEGRAM_BOT_TOKEN=...         # Optional
TELEGRAM_CHAT_ID=...           # Optional
```

## Project Structure

```
AI_trade/
├── bot.py                    # Main trading bot (mode via self._simulation; currently LIVE)
├── run_bot.ps1               # Task Scheduler launcher (logs to logs/runtime_*.log)
├── backtest.py               # Daily-K backtest engine (yfinance or Shioaji)
├── minute_backtest.py        # Minute-K backtest engine (Shioaji only)
├── main.py                   # API connection & account test
├── src/ai_trade/
│   ├── __init__.py
│   ├── client.py             # Shioaji API wrapper
│   ├── news.py               # News aggregator (Cnyes / Yahoo / Google News)
│   ├── scanner.py            # (Legacy) 3-layer funnel scanner — no longer called by bot.py
│   ├── strategy.py           # Multi-strategy framework (StrategyAllocator)
│   └── chips.py              # Institutional flow analysis (auto date fallback)
├── pyproject.toml
└── .env
```

## Running the Bot

⚠️ **Current mode: `self._simulation = False` (LIVE trading, real orders).**
Flip to `True` in `AITradingBot.__init__` for development.

Production runs via local Windows Task Scheduler task `AI_Trade_Bot`
(weekdays 08:25, wakes the PC from sleep, runs `run_bot.ps1`, logs to `logs/runtime_*.log`).
The bot auto-exits at 13:35 TW (`AUTO_EXIT=0` to disable). The GitHub Actions workflow
keeps only `workflow_dispatch` as a manual fallback (its cron was removed 2026-07 due to
3–4h GitHub scheduling delays).

```bash
# Run the bot manually
uv run python bot.py

# Backtest with yfinance (no login required, 5+ years data)
uv run python backtest.py --code 2330 --start 2021-01-01 --yf

# Multi-stock backtest comparison
uv run python backtest.py --code 2330,2454,2317 --start 2021-01-01 --yf

# Syntax check (no API login needed)
uv run python -c "import ast; ast.parse(open('bot.py').read()); print('OK')"
```

## Shioaji Quick Reference

```python
import os
import shioaji as sj
from dotenv import load_dotenv

load_dotenv()

# Initialize (use simulation=True for testing)
api = sj.Shioaji(simulation=True)
api.login(
    api_key=os.environ["API_KEY"],
    secret_key=os.environ["SECRET_KEY"],
    fetch_contract=False,
)
api.activate_ca(
    ca_path=os.environ["CA_CERT_PATH"],
    ca_passwd=os.environ["CA_PASSWORD"],
)

# Contracts
stock  = api.Contracts.Stocks["2330"]       # TSMC
future = api.Contracts.Futures.TXF['TXF202501']

# Subscribe quotes (BidAsk — used for odd-lot slippage check)
api.quote.subscribe(stock, quote_type=sj.constant.QuoteType.BidAsk,
                    version=sj.constant.QuoteVersion.v1)

# Place order
order = api.Order(
    price=100,
    quantity=1,
    action=sj.constant.Action.Buy,
    price_type=sj.constant.StockPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
    order_lot=sj.constant.StockOrderLot.IntradayOdd,   # odd-lot intraday
    account=api.stock_account,
)
trade = api.place_order(stock, order)

# List today's trades (account parameter required)
trades = api.list_trades(api.stock_account)

# Logout
api.logout()
```

## Service Limits

| Type          | Rate Limit        |
|---------------|-------------------|
| Market data   | 50 req / 5s       |
| Accounting    | 25 req / 5s       |
| Orders        | 250 req / 10s     |
| Subscriptions | max 200 active    |

## Key Bot Parameters (bot.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SENTIMENT_ENABLED` | `False` | Toggle AI news sentiment. `False` skips OpenAI entirely (client not created, `OPENAI_API_KEY` not required), uses fixed score of `1.0`. |
| `TOTAL_BUDGET` | 46,000 | Total capital in TWD (runtime-overridden by `account_balance()` + `settlements()`) |
| `MAX_POSITIONS` | 7 | Max concurrent positions (effective count limited by `TOTAL_BUDGET // MIN_ORDER_VALUE`) |
| `STOP_LOSS_PCT` | 0.03 | Fixed stop-loss threshold (3%, combined with ATR stop, takes stricter) |
| `SLIPPAGE_LIMIT` | 0.01 | Max bid-ask spread (1%) — wider for odd-lot market reality |
| `MIN_ORDER_VALUE` | 11,000 | Min order value in TWD — prevents fee erosion on tiny odd-lot trades |
| `TRAILING_START` | 0.015 | Trailing stop activation profit |
| `TRAILING_PULLBACK` | 0.015 | Trailing stop fallback pullback (when ATR unavailable) |
| `TRAILING_ATR_MULT` | 0.6 | Dynamic trailing: exit when price pulls back 0.6×ATR from peak |
| `BREAKEVEN_TRIGGER` | 0.02 | Move stop to breakeven when profit reaches 2% |
| `TIME_STOP_BDAYS` | 5 | Time stop: force exit after N business days if peak profit never reached `TRAILING_START` (swing rule; replaced old `TIME_STOP_MINUTES`) |
| `RVOL_MIN` | 1.5 | Relative volume filter: today's volume must be 1.5× 5-day average |
| `VWAP_MAX_GAP` | 0.03 | Max allowed VWAP deviation (3%) — avoids chasing overextended moves |
| `ATR_MAX_PCT` | 0.03 | Skip entry if ATR/price > 3% (gap risk protection) |
| `MA_TREND_PERIOD` | 50 | Trend filter: only enter when price > MA50 |
| `RSI_DYNAMIC` | `True` | Allow RSI threshold to relax to 75 in trending markets |
| `MARKET_INDEX` | `"0050"` | Market index ticker for regime detection |
| `SCAN_INTERVAL` | 60 | Entry-scan main loop interval (seconds) |
| `EXIT_CHECK_INTERVAL` | 30 | Exit-monitor **thread** interval (seconds) — runs independently of entry scan |
| `DAILY_CACHE_TTL` | 7200 | Daily-K cache lifetime (seconds) — refreshes intraday partial bar every 2h |
| `AUTO_EXIT_AFTER_CLOSE` | `True` | Auto-exit process at 13:35 TW (local Task Scheduler mode; disable with env `AUTO_EXIT=0`) |
| `CORE_STOCKS` | 12 tickers | Backtest-validated core watchlist — fully evaluated every scan |
| `EXTENDED_STOCKS` | 62 tickers | Unvalidated extended watchlist — must pass snapshot prefilter first |
| `PREFILTER_TOP_N` | 15 | Extended-list prefilter: top N by change_rate enter full evaluation |
| `PREFILTER_MIN_AMOUNT` | 30,000,000 | Extended-list prefilter: min daily turnover in TWD |

## Key Backtest Parameters (backtest.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STOP_LOSS_PCT` | 0.025 | Stop-loss (2.5% — matches bot.py) |
| `ATR_MAX_PCT` | 0.03 | Skip entry if ATR/price > 3% (gap risk protection) |
| `MA_TREND_PERIOD` | 50 | Trend filter: only enter when price > MA50 |
| `TRAILING_ATR_MULT` | 0.6 | Dynamic trailing stop multiplier |
| `BREAKEVEN_TRIGGER` | 0.02 | Breakeven stop trigger |

## Watchlist: CORE_STOCKS + EXTENDED_STOCKS

`PINNED_STOCKS = CORE_STOCKS + EXTENDED_STOCKS`（74 檔，全部訂閱 BidAsk）。

- **CORE_STOCKS（12 檔，回測驗證 2021–2026）**：每輪進場掃描完整評估。
- **EXTENDED_STOCKS（62 檔，自選待驗證）**：每輪先經一次 `snapshots()` 批次粗篩
  （漲幅 > 0 且成交額 ≥ `PREFILTER_MIN_AMOUNT`，取漲幅前 `PREFILTER_TOP_N` 檔）
  才進入完整評估 — 控制單輪掃描時間，避免 74 檔全打 ticks/kbars/籌碼 API。

```python
CORE_STOCKS = (
    "2059",   # 川湖  PF=3.21 Sharpe=4.52
    "8210",   # 上緯  PF=1.87 Sharpe=3.63
    "3324",   # 雙鴻  PF=1.69 Sharpe=3.60
    "2454",   # 聯發科 PF=1.53 Sharpe=2.73 (0050)
    "3017",   # 奇鋐  PF=1.50 Sharpe=2.32
    "2330",   # 台積電 PF=1.33 Sharpe=1.93 (0050)
    "8996",   # 高力  PF=1.20 Sharpe=1.14
    "1590",   # 亞德客 PF=3.15 Sharpe=6.83 (0050)
    "2603",   # 長榮  PF=2.41 Sharpe=5.13 (0050)
    "2609",   # 陽明  PF=1.58 Sharpe=2.55 (0050)
    "2357",   # 華碩  PF=1.28 Sharpe=1.35 (0050)
    "2379",   # 瑞昱  PF=1.13 Sharpe=0.63 (0050)
)
```

## Strategy Architecture

### Two loops (since 2026-07)

**Exit-monitor thread** — every `EXIT_CHECK_INTERVAL` (30s) during 09:05–13:30, fully
independent of the entry scan so long scans can never delay stop-losses:
```
monitor_exit()  — ATR stop / breakeven / trailing / time stop, ignores all filters
```

**Entry-scan main loop** — every `SCAN_INTERVAL` (60s) during 09:05–13:25
(entry scan starts at 09:20 after the early-market filter); process auto-exits at 13:35:
```
1. check_market_trend() — skip entry scan if 0050 < MA20 (hysteresis band)
2. sentiment score      — SENTIMENT_ENABLED=False → fixed 1.0
3. allocator.allocate() — TRENDING vs RANGING regime
4. scan_candidates()    — CORE fully evaluated + EXTENDED prefiltered, rank, buy top scorers
```

### StrategyAllocator (strategy.py)
Detects market regime from 0050 20-day annualised volatility:
- **TRENDING** (vol < 1.5%): 80% momentum / 20% mean-reversion
- **RANGING** (vol ≥ 1.5%): 30% momentum / 70% mean-reversion

### scan_candidates() — correct entry point for scanning
Always call `bot.scan_candidates(watch_list, score, analysis, alloc)`.
Do NOT call `bot.scan_mean_reversion()` or `bot.scan_and_buy()` — these do not exist.

### Entry conditions (momentum strategy)
All must pass:
1. Not already holding this stock
2. Slippage OK — bid-ask spread ≤ 1% (from live BidAsk subscription, fallback to snapshot)
3. `current_price > MA50` — long-term uptrend
4. `ATR/price ≤ 3%` — not too volatile (gap risk)
5. `RSI < 70` (or 75 in trending market with `RSI_DYNAMIC=True`) — RSI computed on
   **1-minute resampled bars** (`ticks_to_minute_df`), never on raw ticks
6. `0 < VWAP_gap ≤ 3%` — above VWAP but not overextended (VWAP stays tick-based)
7. `RVOL ≥ 1.5` — volume surge confirmation
8. `chip_score ≥ -0.3` — institutions not heavily selling
9. `qty × price ≥ 11,000` — order value above minimum

### Exit Logic (4 conditions, in priority order)
```
A. ATR stop  : stop_price = max(entry - 1.5×ATR, entry × (1 - STOP_LOSS_PCT))
B. Breakeven : move stop to entry when profit ≥ BREAKEVEN_TRIGGER
C. Trailing  : exit when pullback from peak ≥ max(0.6×ATR/peak, TRAILING_PULLBACK)
D. Time stop : force exit after TIME_STOP_BDAYS business days if peak profit never reached TRAILING_START
```

**Odd-lot T+1 rule**: positions entered today are NEVER exited today (regulatory rule for intraday odd-lot trading).

## chips.py — Institutional Flow (Smart Date Fallback)

TWSE publishes institutional data at ~14:30 each day.
- Before 14:40: `chips_sentiment()` automatically uses the **previous trading day** data
- After 14:40: uses today's data
- If the target date has no data (weekend/holiday), auto-retries up to 5 days back

No date argument needed: `chips_sentiment("2330")` always returns the most recent valid data.

## BidAsk Subscription (Odd-lot Quote Monitoring)

Shioaji has no separate odd-lot snapshot API. Bot subscribes to `QuoteType.BidAsk` for all PINNED_STOCKS at startup:
- `self._odd_quotes: dict[str, tuple[float, float]]` — cached (bid, ask) per stock
- `check_slippage_safe()` uses this cache first; falls back to `api.snapshots()` if cache is empty

## 5-Year Backtest Results (yfinance, 2021–2026, daily-K)

| Code | Name | Win Rate | Profit Factor | Max DD | Sharpe | Net P&L |
|------|------|---------|--------------|--------|--------|---------|
| 2059 | 川湖 | — | 3.21 | — | 4.52 | positive |
| 1590 | 亞德客 | — | 3.15 | — | 6.83 | positive |
| 2603 | 長榮 | — | 2.41 | — | 5.13 | positive |
| 3324 | 雙鴻 | — | 1.69 | — | 3.60 | positive |
| 2454 | 聯發科 | 53.5% | 1.53 | -22.9% | 2.73 | +6,086 TWD |
| 2330 | 台積電 | 46.3% | 1.33 | -19.5% | 1.93 | +6,366 TWD |

MA50 trend filter reduced max drawdown from -43% → -19% on 2330.

## Development Rules

- Always use `simulation=True` during development
- Never commit API keys or credentials
- `api.list_trades()` requires account parameter: `api.list_trades(api.stock_account)`
- Simulation `account_balance()` returns 0 — bot handles this gracefully (keeps default budget)
- Funnel scanner (`scanner.py`) runs once daily at 09:20 via `run_funnel_if_needed()`; its picks are merged into `watch_list` on top of `PINNED_STOCKS`
- Test with `/shioaji-init` skill to scaffold new features
- Python 3.12+ required (pandas-ta dependency)
