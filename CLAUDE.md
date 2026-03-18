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
pip install shioaji
# or with uv:
uv sync
```

### Credentials
Set environment variables (never commit credentials):
```bash
SHIOAJI_API_KEY=your_api_key
SHIOAJI_SECRET_KEY=your_secret_key
```

Or use a `.env` file (git-ignored).

## Project Structure

```
AI_trade/
├── src/ai_trade/
│   ├── __init__.py
│   ├── client.py        # Shioaji API wrapper
│   ├── strategy/        # Trading strategies
│   └── data/            # Market data utilities
├── tests/
├── requirements.txt
└── pyproject.toml
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

# Logout
api.logout()
```

## Service Limits

| Type       | Rate Limit           |
|------------|----------------------|
| Market data| 50 req / 5s          |
| Accounting | 25 req / 5s          |
| Orders     | 250 req / 10s        |
| Subscriptions | max 200 active    |

## Key Bot Parameters (bot.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SENTIMENT_ENABLED` | `True` | Toggle AI news sentiment analysis. Set `False` to skip OpenAI calls and use a fixed score of `1.0`, saving API costs. |
| `TOTAL_BUDGET` | 45,000 | Total capital in TWD |
| `MAX_POSITIONS` | 3 | Max concurrent positions |
| `STOP_LOSS_PCT` | 0.02 | Hard stop-loss threshold |
| `TRAILING_START` | 0.015 | Trailing stop activation profit |
| `TRAILING_PULLBACK` | 0.01 | Trailing stop pullback trigger |

## Development Rules

- Always use `simulation=True` during development
- Never commit API keys or credentials
- Test with `/shioaji-init` skill to scaffold new features
