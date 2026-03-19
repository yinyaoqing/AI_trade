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
│   ├── scanner.py            # 3-layer funnel scanner (FunnelScanner)
│   ├── strategy.py           # Multi-strategy framework (StrategyAllocator)
│   └── chips.py              # Institutional flow analysis
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

# Subscribe quotes
api.quote.subscribe(stock, quote_type=sj.constant.QuoteType.Tick)

# Place order
order = api.Order(
    price=100,
    quantity=1,
    action=sj.constant.Action.Buy,
    price_type=sj.constant.StockPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
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
| `STOP_LOSS_PCT` | 0.02 | Fixed stop-loss threshold (combined with ATR stop, takes stricter) |
| `TRAILING_START` | 0.015 | Trailing stop activation profit |
| `TRAILING_PULLBACK` | 0.01 | Trailing stop fallback pullback (when ATR unavailable) |
| `TRAILING_ATR_MULT` | 0.6 | Dynamic trailing: exit when price pulls back 0.6×ATR from peak |
| `BREAKEVEN_TRIGGER` | 0.02 | Move stop to breakeven when profit reaches 2% |
| `TIME_STOP_MINUTES` | 30 | Time stop: exit if price stays within ±0.5% of entry for N minutes. Set `0` to disable (swing strategy). |
| `RVOL_MIN` | 1.5 | Relative volume filter: current bar must be 1.5× 5-bar average |
| `VWAP_MAX_GAP` | 0.03 | Max allowed VWAP deviation (3%) — avoids chasing overextended moves |
| `RSI_DYNAMIC` | `True` | Allow RSI threshold to relax to 75 in trending markets |
| `MARKET_INDEX` | `"0050"` | Market index ticker for regime detection |
| `PINNED_STOCKS` | 14 tickers | Fixed watchlist always scanned regardless of funnel results |

## Key Backtest Parameters (backtest.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `STOP_LOSS_PCT` | 0.03 | Stop-loss (3% — wider than bot to reduce whipsaws in daily-K simulation) |
| `ATR_MAX_PCT` | 0.03 | Skip entry if ATR/price > 3% (gap risk protection) |
| `MA_TREND_PERIOD` | 50 | Trend filter: only enter when price > MA50 |
| `TRAILING_ATR_MULT` | 0.6 | Dynamic trailing stop multiplier |
| `BREAKEVEN_TRIGGER` | 0.02 | Breakeven stop trigger |

## Strategy Architecture

### StrategyAllocator (strategy.py)
Detects market regime from 0050 20-day annualised volatility:
- **TRENDING** (vol < 1.5%): 80% momentum / 20% mean-reversion
- **RANGING** (vol ≥ 1.5%): 30% momentum / 70% mean-reversion

### scan_candidates() — correct entry point for scanning
Always call `bot.scan_candidates(watch_list, score, analysis, alloc)`.
Do NOT call `bot.scan_mean_reversion()` or `bot.scan_and_buy()` — these do not exist.

### Exit Logic (4 conditions, in priority order)
```
A. ATR stop  : stop_price = max(entry - 1.5×ATR, entry × (1 - STOP_LOSS_PCT))
B. Breakeven : move stop to entry when profit ≥ BREAKEVEN_TRIGGER
C. Trailing  : exit when pullback from peak ≥ max(0.6×ATR/peak, TRAILING_PULLBACK)
D. Time stop : exit if within ±TIME_STOP_BAND of entry after TIME_STOP_MINUTES (0 = disabled)
```

## 5-Year Backtest Results (yfinance, 2021–2026, daily-K)

| Code | Win Rate | Profit Factor | Max DD | Sharpe | Net P&L |
|------|---------|--------------|--------|--------|---------|
| 2330 | 46.3% | 1.33 | -19.5% | 1.93 | +6,366 TWD |
| 2454 | 53.5% | 1.53 | -22.9% | 2.73 | +6,086 TWD |
| 2317 | 32.6% | 0.93 | -49.4% | -0.43 | -1,140 TWD |

2330 and 2454 fit the strategy well. 2317 underperforms due to high volatility — needs tighter stops.

## Development Rules

- Always use `simulation=True` during development
- Never commit API keys or credentials
- `api.list_trades()` requires account parameter: `api.list_trades(api.stock_account)`
- Test with `/shioaji-init` skill to scaffold new features
- Python 3.12+ required (pandas-ta dependency)
