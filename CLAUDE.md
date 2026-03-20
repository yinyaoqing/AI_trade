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
├── bot.py                    # Main trading bot (live simulation)
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

```bash
# Live simulation trading
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
| `SENTIMENT_ENABLED` | `False` | Toggle AI news sentiment. `False` skips OpenAI calls (saves cost), uses fixed score of `1.0`. |
| `TOTAL_BUDGET` | 45,000 | Total capital in TWD |
| `MAX_POSITIONS` | 3 | Max concurrent positions |
| `STOP_LOSS_PCT` | 0.025 | Fixed stop-loss threshold (2.5%, combined with ATR stop, takes stricter) |
| `SLIPPAGE_LIMIT` | 0.01 | Max bid-ask spread (1%) — wider for odd-lot market reality |
| `MIN_ORDER_VALUE` | 10,000 | Min order value in TWD — prevents fee erosion on tiny odd-lot trades |
| `TRAILING_START` | 0.015 | Trailing stop activation profit |
| `TRAILING_PULLBACK` | 0.01 | Trailing stop fallback pullback (when ATR unavailable) |
| `TRAILING_ATR_MULT` | 0.6 | Dynamic trailing: exit when price pulls back 0.6×ATR from peak |
| `BREAKEVEN_TRIGGER` | 0.02 | Move stop to breakeven when profit reaches 2% |
| `TIME_STOP_MINUTES` | 30 | Time stop: exit if price stays within ±0.5% of entry for N minutes. Set `0` to disable (swing strategy). |
| `RVOL_MIN` | 1.5 | Relative volume filter: current bar must be 1.5× 5-bar average |
| `VWAP_MAX_GAP` | 0.03 | Max allowed VWAP deviation (3%) — avoids chasing overextended moves |
| `ATR_MAX_PCT` | 0.03 | Skip entry if ATR/price > 3% (gap risk protection) |
| `MA_TREND_PERIOD` | 50 | Trend filter: only enter when price > MA50 |
| `RSI_DYNAMIC` | `True` | Allow RSI threshold to relax to 75 in trending markets |
| `MARKET_INDEX` | `"0050"` | Market index ticker for regime detection |
| `PINNED_STOCKS` | 12 tickers | Fixed watchlist — the only scan targets (funnel scan removed) |

## Key Backtest Parameters (backtest.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STOP_LOSS_PCT` | 0.025 | Stop-loss (2.5% — matches bot.py) |
| `ATR_MAX_PCT` | 0.03 | Skip entry if ATR/price > 3% (gap risk protection) |
| `MA_TREND_PERIOD` | 50 | Trend filter: only enter when price > MA50 |
| `TRAILING_ATR_MULT` | 0.6 | Dynamic trailing stop multiplier |
| `BREAKEVEN_TRIGGER` | 0.02 | Breakeven stop trigger |

## PINNED_STOCKS (12 tickers, backtest-validated 2021–2026)

```python
PINNED_STOCKS = (
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

### Main loop (every 60 seconds during 09:05–13:25)

```
1. monitor_exit()       — always runs first, ignores all filters
2. check_market_trend() — skip entry scan if 0050 < MA20
3. sentiment score      — SENTIMENT_ENABLED=False → fixed 1.0
4. allocator.allocate() — TRENDING vs RANGING regime
5. scan_candidates()    — evaluate all PINNED_STOCKS, rank, buy top scorers
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
5. `RSI < 70` (or 75 in trending market with `RSI_DYNAMIC=True`)
6. `0 < VWAP_gap ≤ 3%` — above VWAP but not overextended
7. `RVOL ≥ 1.5` — volume surge confirmation
8. `chip_score ≥ -0.3` — institutions not heavily selling
9. `qty × price ≥ 10,000` — order value above minimum

### Exit Logic (4 conditions, in priority order)
```
A. ATR stop  : stop_price = max(entry - 1.5×ATR, entry × (1 - STOP_LOSS_PCT))
B. Breakeven : move stop to entry when profit ≥ BREAKEVEN_TRIGGER
C. Trailing  : exit when pullback from peak ≥ max(0.6×ATR/peak, TRAILING_PULLBACK)
D. Time stop : exit if within ±TIME_STOP_BAND of entry after TIME_STOP_MINUTES (0 = disabled)
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
- Funnel scanner (`scanner.py`) is **not called** by bot.py — `watch_list` is fixed to `list(PINNED_STOCKS)`
- Test with `/shioaji-init` skill to scaffold new features
- Python 3.12+ required (pandas-ta dependency)
