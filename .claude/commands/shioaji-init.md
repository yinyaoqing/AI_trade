Initialize or scaffold a new Shioaji trading component for this project.

Based on the user's request, create the appropriate files using Shioaji API patterns:

## Instructions

Read CLAUDE.md for project context. Then based on what the user wants to build, scaffold the relevant code:

### If user wants a new strategy:
Create `src/ai_trade/strategy/<name>.py` with:
- A class inheriting from a base strategy
- `on_tick()` method for real-time data handling
- `on_bar()` method for OHLCV data
- Entry/exit logic skeleton

### If user wants the API client:
Create/update `src/ai_trade/client.py` with:
```python
import os
import shioaji as sj
from shioaji import constant

class ShioajiClient:
    def __init__(self, simulation: bool = True):
        self.api = sj.Shioaji(simulation=simulation)
        self._connected = False

    def login(self):
        api_key = os.environ["SHIOAJI_API_KEY"]
        secret_key = os.environ["SHIOAJI_SECRET_KEY"]
        accounts = self.api.login(api_key=api_key, secret_key=secret_key)
        self.api.set_default_account(accounts[0])
        self._connected = True
        return accounts

    def logout(self):
        if self._connected:
            self.api.logout()
            self._connected = False

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *args):
        self.logout()
```

### If user wants market data utilities:
Create `src/ai_trade/data/<name>.py` with tick/kbar subscription and historical data fetch patterns.

### If user wants order management:
Create `src/ai_trade/orders.py` with order placement, cancellation, and status tracking.

## Always:
1. Use `simulation=True` by default unless explicitly asked for live trading
2. Read API keys from environment variables, never hardcode
3. Include type hints
4. Add error handling for API rate limits (50 req/5s market data, 250 req/10s orders)
5. Reference Shioaji docs: https://sinotrade.github.io/llms-full.txt

## After scaffolding:
- Update `requirements.txt` if new dependencies added
- Remind user to install with `pip install shioaji` or `uv sync`
