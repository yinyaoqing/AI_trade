import os
import shioaji as sj
from dotenv import load_dotenv

load_dotenv()

api = sj.Shioaji(simulation=True)

accounts = api.login(
    api_key=os.environ["API_KEY"],
    secret_key=os.environ["SECRET_KEY"],
)
# 明確設定預設帳戶
api.set_default_account(accounts[0])  # 期貨帳戶 (AccountType.H)

api.activate_ca(
    ca_path=os.environ["CA_CERT_PATH"],
    ca_passwd=os.environ["CA_PASSWORD"],
)

print("Login success:", accounts)
print("shioaji version:", sj.__version__)

# --- 證券下單測試 ---
stock_contract = api.Contracts.Stocks.TSE["2890"]
stock_order = api.Order(
    price=18,
    quantity=1,
    action=sj.constant.Action.Buy,
    price_type=sj.constant.StockPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
    account=api.stock_account,
)
stock_trade = api.place_order(stock_contract, stock_order)
print("Stock trade:", stock_trade)

# --- 期貨下單測試 ---
futures_contract = min(
    [x for x in api.Contracts.Futures.TXF if x.code[-2:] not in ["R1", "R2"]],
    key=lambda x: x.delivery_date,
)
futures_order = api.Order(
    action=sj.constant.Action.Buy,
    price=15000,
    quantity=1,
    price_type=sj.constant.FuturesPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
    octype=sj.constant.FuturesOCType.Auto,
    account=accounts[0],  # Account(account_type='H') — 期貨帳戶
)
futures_trade = api.place_order(futures_contract, futures_order)
print("Futures trade:", futures_trade)

api.logout()

# --- 查詢是否通過 API 測試 ---
print("\n=== 查詢 API 測試狀態 ===")
api_prod = sj.Shioaji(simulation=False)
prod_accounts = api_prod.login(
    api_key=os.environ["API_KEY"],
    secret_key=os.environ["SECRET_KEY"],
)
for acc in prod_accounts:
    signed = getattr(acc, "signed", None)
    status = "[PASS] 通過" if signed else "[FAIL] 未通過 (請等待審核約5分鐘)"
    print(f"帳戶 {acc.account_id} ({acc.account_type}): {status}")
api_prod.logout()
