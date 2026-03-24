"""
帳戶狀態確認工具（正式環境）
================================
- 使用 simulation=False 連線真實帳戶
- 查詢：帳戶餘額、交割款、目前持倉及未實現損益
- 結果同步推播至 Telegram

執行方式：
    uv run python check_account.py
"""

import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

TZ_TW = timezone(timedelta(hours=8))


def now_tw() -> datetime:
    return datetime.now(TZ_TW)


def send_telegram(msg: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[Telegram] 未設定 BOT_TOKEN / CHAT_ID，跳過推播")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=15,
        )
        if resp.ok:
            print("[Telegram] 推播成功")
        else:
            print(f"[Telegram] 推播失敗 {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        print(f"[Telegram] 推播例外: {e}")


def check_account() -> None:
    import shioaji as sj

    api_key    = os.environ.get("API_KEY", "")
    secret_key = os.environ.get("SECRET_KEY", "")
    ca_path    = os.environ.get("CA_CERT_PATH", "")
    ca_passwd  = os.environ.get("CA_PASSWORD", "")

    if not api_key or not secret_key:
        print("[錯誤] .env 未設定 API_KEY / SECRET_KEY")
        sys.exit(1)

    # ── 1. 建立正式連線（simulation=False）──────────────────────────
    print("[連線] 正式環境（simulation=False）...")
    api = sj.Shioaji(simulation=False)

    try:
        accounts = api.login(
            api_key=api_key,
            secret_key=secret_key,
            fetch_contract=False,
        )
        print(f"[連線] 登入成功，帳號：{[str(a.account_id) for a in accounts]}")
    except Exception as e:
        print(f"[連線] 登入失敗: {e}")
        sys.exit(1)

    # ── 2. 啟用 CA 憑證（下單必要，查詢亦建議啟用）──────────────────
    if ca_path and os.path.exists(ca_path):
        try:
            api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd)
            print(f"[CA] 憑證啟用成功：{ca_path}")
        except Exception as e:
            print(f"[CA] 憑證啟用失敗（查詢仍可繼續）：{e}")
    else:
        print(f"[CA] 憑證路徑不存在，跳過啟用：{ca_path!r}")

    stock_account = api.stock_account
    lines: list[str] = []
    ts = now_tw().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"[帳戶狀態確認] {ts}")
    lines.append(f"帳號：{stock_account.account_id}  ({stock_account.username})")
    lines.append("")

    # ── 3. 帳戶餘額（現貨交割款）────────────────────────────────────
    try:
        bal = api.account_balance()
        acc_balance = float(bal.acc_balance)
        lines.append(f"[帳戶餘額]")
        lines.append(f"  交割款可用餘額：{acc_balance:,.0f} 元")
        bal_date = getattr(bal, "date", "")
        if bal_date:
            lines.append(f"  資料時間：{bal_date}")
    except Exception as e:
        lines.append(f"[帳戶餘額] 查詢失敗：{e}")

    lines.append("")

    # ── 4. 目前持倉 ──────────────────────────────────────────────────
    try:
        held = api.list_positions(stock_account)
        if not held:
            lines.append("[持倉] 目前無持股")
        else:
            total_pnl = 0.0
            lines.append(f"[持倉] 共 {len(held)} 筆")
            for p in held:
                code      = p.code
                qty       = int(getattr(p, "quantity", 0))
                avg_price = float(getattr(p, "price", None) or getattr(p, "average_price", 0))
                last      = float(getattr(p, "last_price", avg_price) or avg_price)
                pnl       = (last - avg_price) * qty
                pct       = (last - avg_price) / avg_price * 100 if avg_price else 0
                total_pnl += pnl
                lines.append(
                    f"  {code}  {qty}股  均價={avg_price:.2f}  "
                    f"現價={last:.2f}  損益={pnl:+.0f}元 ({pct:+.2f}%)"
                )
            lines.append(f"  ────────────────────────────────")
            lines.append(f"  合計未實現損益：{total_pnl:+,.0f} 元")
    except Exception as e:
        lines.append(f"[持倉] 查詢失敗：{e}")

    # ── 5. 輸出並推播 ─────────────────────────────────────────────────
    report = "\n".join(lines)
    print("\n" + report + "\n")
    send_telegram(report)

    # ── 6. 登出 ───────────────────────────────────────────────────────
    try:
        api.logout()
        print("[連線] 已登出")
    except Exception:
        pass


if __name__ == "__main__":
    check_account()
