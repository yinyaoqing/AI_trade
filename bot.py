"""
AI 模擬交易機器人
- 模式：simulation=True（模擬盤，不動用真實資金）
- 策略：大盤月線過濾 + OpenAI 情緒分析 + VWAP 進場
        + 滑點保護 + 移動止盈 + 2% 強制止損
- 支援：多標的同時監控，最多 MAX_POSITIONS 個部位
"""

import os
import sys
import time
import threading
from dataclasses import dataclass, field

os.environ.setdefault("PYTHONUNBUFFERED", "1")
sys.stdout.reconfigure(line_buffering=True)

import shioaji as sj
import pandas as pd
import pandas_ta as ta
import requests
from openai import OpenAI
from datetime import datetime
from dotenv import load_dotenv
from src.ai_trade.news import NewsAggregator
from src.ai_trade.scanner import FunnelScanner

load_dotenv()

# =============================================================================
# 1. 參數設定
# =============================================================================

TOTAL_BUDGET      = 45000   # 總預算（元）
MAX_POSITIONS     = 3       # 最多同時持有部位數
POSITION_SIZE     = TOTAL_BUDGET // MAX_POSITIONS  # 每筆 15,000 元

# 漏斗掃描器設定
FUNNEL_SCAN_HOUR   = 9      # 漏斗掃描執行時間：09:20（開盤 15 分鐘後）
FUNNEL_SCAN_MINUTE = 20
FUNNEL_MAX_RESULTS = 5      # 最終精選標的上限

STOP_LOSS_PCT     = 0.02    # 強制止損：虧損 2% 立即賣出
TRAILING_START    = 0.015   # 移動止盈啟動點：獲利達 1.5%
TRAILING_PULLBACK = 0.01    # 移動止盈觸發：自高點回吐 1%
SLIPPAGE_LIMIT    = 0.005   # 滑點保護：買賣價差 > 0.5% 不交易

SCAN_INTERVAL          = 60    # 主循環間隔（秒）
NEWS_DIGEST_INTERVAL   = 1800  # 非交易時間新聞推播間隔（秒）

openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
tg_token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")


# =============================================================================
# 2. 資料結構
# =============================================================================

@dataclass
class Position:
    code: str
    entry_price: float
    qty: int
    max_price: float = field(init=False)

    def __post_init__(self):
        self.max_price = self.entry_price

    def update_max(self, current: float) -> None:
        if current > self.max_price:
            self.max_price = current

    def profit_pct(self, current: float) -> float:
        return (current - self.entry_price) / self.entry_price

    def pullback_pct(self, current: float) -> float:
        return (self.max_price - current) / self.max_price


# =============================================================================
# 3. 工具函數
# =============================================================================

def _telegram_post(token: str, chat_id: str, msg: str) -> None:
    proxies = {}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            proxies=proxies or None,
            timeout=15,
        )
        if not resp.ok:
            print(f"[Telegram] 回應錯誤 {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"[Telegram] 通知失敗: {e}")


def send_notify(msg: str) -> None:
    print(f"[Telegram] {msg}")
    if not tg_token or not tg_chat_id:
        return
    token = tg_token.strip()
    if "\n" in token or ":" not in token:
        print("[Telegram] 錯誤：TELEGRAM_BOT_TOKEN 格式不正確，請確認 .env 無換行。")
        return
    threading.Thread(
        target=_telegram_post,
        args=(token, tg_chat_id.strip(), msg),
        daemon=True,
    ).start()


def get_ai_sentiment(news_text: str) -> tuple[float, str]:
    """OpenAI 語意分析：回傳 (情緒分數 -1.0~1.0, 繁中摘要)"""
    try:
        prompt = (
            "你是台股分析師。請根據以下新聞標題分析對整體台股的影響，"
            "回傳格式如下（共兩行）：\n"
            "第一行：一個數字（-1.0 至 1.0），1.0 代表極度利多\n"
            "第二行：50 字以內的繁體中文分析摘要\n\n"
            "新聞：\n" + news_text
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
        )
        content = response.choices[0].message.content.strip()
        lines = content.splitlines()
        score = float(lines[0].strip())
        analysis = lines[1].strip() if len(lines) > 1 else ""
        return score, analysis
    except Exception as e:
        print(f"AI 分析失敗: {e}")
        return 0.0, ""


def ticks_to_df(ticks) -> pd.DataFrame:
    df = pd.DataFrame({**ticks.dict()})
    df["datetime"] = pd.to_datetime(df["ts"])
    return df.set_index("datetime").sort_index()


def sentiment_label(score: float) -> str:
    if score > 0.3:
        return "利多"
    if score < -0.3:
        return "利空"
    return "中性"


# =============================================================================
# 4. 核心交易邏輯
# =============================================================================

def _debug_env() -> None:
    """啟動時印出環境變數摘要（敏感值遮蔽），協助診斷 GitHub Actions 問題"""
    import base64

    def mask(v: str, show: int = 4) -> str:
        v = v.strip()
        if not v:
            return "(未設定)"
        if len(v) <= show * 2:
            return "***"
        return v[:show] + "***" + v[-show:]

    vars_info = {
        "API_KEY":            os.environ.get("API_KEY", ""),
        "SECRET_KEY":         os.environ.get("SECRET_KEY", ""),
        "CA_CERT_PATH":       os.environ.get("CA_CERT_PATH", ""),
        "CA_PASSWORD":        os.environ.get("CA_PASSWORD", ""),
        "OPENAI_API_KEY":     os.environ.get("OPENAI_API_KEY", ""),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID":   os.environ.get("TELEGRAM_CHAT_ID", ""),
    }

    print("[Debug] ── 環境變數摘要 ────────────────────────────")
    for k, v in vars_info.items():
        stripped = v.strip()
        print(f"  {k:<22}: {mask(stripped)}  (len={len(stripped)})")

    # SECRET_KEY 額外診斷
    sk = os.environ.get("SECRET_KEY", "").strip()
    if sk:
        print("[Debug] ── SECRET_KEY 診斷 ─────────────────────────")
        try:
            decoded = base64.b64decode(sk + "==")
            print(f"  base64解碼長度  : {len(decoded)} bytes (Shioaji 需要 32)")
            if len(decoded) != 32:
                print(f"  建議            : 重新從永豐金 API 管理頁複製正確的 SECRET_KEY")
        except Exception as ex:
            print(f"  base64解碼失敗  : {ex}")
        has_newline = "\n" in sk or "\r" in sk
        non_b64 = [c for c in sk if c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="]
        print(f"  含換行符號      : {has_newline}")
        print(f"  非base64字元    : {non_b64 if non_b64 else '無'}")

    # CA 憑證檔案檢查
    ca_path = os.environ.get("CA_CERT_PATH", "").strip()
    if ca_path:
        import pathlib
        p = pathlib.Path(ca_path)
        exists = p.exists()
        size   = p.stat().st_size if exists else 0
        print(f"[Debug] CA憑證     : {ca_path}  存在={exists}  大小={size} bytes")

    print("[Debug] ─────────────────────────────────────────────")


class AITradingBot:
    def __init__(self):
        _debug_env()

        self.api = sj.Shioaji(simulation=True)
        print("[初始化] Shioaji 實例建立完成")

        # 清除環境變數中可能夾帶的空白、換行（GitHub Actions Secrets 常見問題）
        api_key    = os.environ["API_KEY"].strip()
        secret_key = os.environ["SECRET_KEY"].strip()

        print(f"[初始化] 嘗試登入（API_KEY 長度={len(api_key)}，SECRET_KEY 長度={len(secret_key)}）")
        accounts = self.api.login(
            api_key=api_key,
            secret_key=secret_key,
            fetch_contract=False,
        )
        print(f"[初始化] 登入回應：{accounts}")

        print("[初始化] 下載合約中...")
        self.api.fetch_contracts(
            contract_download=True,
            contracts_timeout=30000,
            contracts_cb=lambda: print("[初始化] 合約下載完成"),
        )

        ca_path = os.environ["CA_CERT_PATH"].strip()
        ca_pass = os.environ["CA_PASSWORD"].strip()
        print(f"[初始化] 啟用 CA 憑證：{ca_path}")
        self.api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)
        print("[初始化] CA 憑證啟用成功")

        self.api.set_default_account(accounts[1])
        print(f"[初始化] 預設帳戶：{accounts[1]}")
        print(f"[初始化] 所有帳戶：{[str(a.account_id) for a in accounts]}")

        self.positions: dict[str, Position] = {}
        self.watch_list: list[str] = []          # 由漏斗掃描器動態更新
        self.funnel = FunnelScanner(self.api, get_ai_sentiment)
        self._funnel_done_today: str = ""        # 記錄已執行掃描的日期

    # ------------------------------------------------------------------
    # 漏斗掃描：每日 09:20 執行一次，動態更新監控清單
    # ------------------------------------------------------------------
    def run_funnel_if_needed(self, now: datetime) -> None:
        """09:20 後且今日尚未執行，才觸發漏斗掃描"""
        today = now.strftime("%Y-%m-%d")
        if self._funnel_done_today == today:
            return
        if not (now.hour == FUNNEL_SCAN_HOUR and now.minute >= FUNNEL_SCAN_MINUTE):
            return

        results = self.funnel.run(max_results=FUNNEL_MAX_RESULTS)
        self._funnel_done_today = today

        if not results:
            print("[漏斗] 今日無精選標的，維持現有監控清單。")
            return

        self.watch_list = [r.code for r in results]
        print(f"[漏斗] 更新監控清單：{self.watch_list}")

        # 推播精選結果
        lines = [f"[漏斗掃描結果] {now.strftime('%H:%M')}"]
        for i, r in enumerate(results, 1):
            lines.append(
                f"{i}. {r.code}  現價={r.current_price}  漲幅={r.gain_pct:+.2%}\n"
                f"   VWAP={r.vwap}  15分量比={r.open15_ratio:.1%}\n"
                f"   情緒={r.score:+.2f}  {r.analysis}"
            )
        send_notify("\n\n".join(lines))

    # ------------------------------------------------------------------
    # 大盤趨勢過濾
    # ------------------------------------------------------------------
    def check_market_trend(self) -> bool:
        """0050 收盤價是否在 20 日均線之上"""
        try:
            contract = self.api.Contracts.Stocks["0050"]
            kbars = self.api.kbars(
                contract,
                start="2025-09-01",
                end=datetime.now().strftime("%Y-%m-%d"),
            )
            df = pd.DataFrame({**kbars.dict()}).set_index("ts").sort_index()
            ma20 = df["Close"].rolling(20).mean().iloc[-1]
            current = df["Close"].iloc[-1]
            label = "趨勢向上" if current > ma20 else "趨勢偏弱"
            print(f"[大盤] 0050={current:.2f}  MA20={ma20:.2f}  {label}")
            return current > ma20
        except Exception as e:
            print(f"[大盤] 取得失敗: {e}")
            return False

    # ------------------------------------------------------------------
    # 滑點保護
    # ------------------------------------------------------------------
    def check_slippage_safe(self, contract) -> bool:
        """買賣價差是否在允許範圍內"""
        try:
            snap = self.api.snapshots([contract])[0]
            bid = snap.buy_price
            ask = snap.sell_price
            if bid == 0 or ask == 0:
                print(f"[滑點] {contract.code} 無報價，跳過。")
                return False
            spread = (ask - bid) / bid
            if spread > SLIPPAGE_LIMIT:
                print(f"[滑點] {contract.code} 價差 {spread:.2%} > {SLIPPAGE_LIMIT:.2%}，暫緩。")
                return False
            print(f"[滑點] {contract.code} 價差 {spread:.2%} 合格。")
            return True
        except Exception as e:
            print(f"[滑點] {contract.code} 檢查失敗: {e}")
            return False

    # ------------------------------------------------------------------
    # 進場掃描（單一標的）
    # ------------------------------------------------------------------
    def scan_and_buy(self, stock_code: str, score: float, analysis: str) -> None:
        """通過市場情緒後，對單一標的執行 VWAP + 滑點檢查並進場"""
        if stock_code in self.positions:
            print(f"[{stock_code}] 已持有，跳過。")
            return
        if len(self.positions) >= MAX_POSITIONS:
            print(f"[{stock_code}] 部位已滿（{MAX_POSITIONS}），跳過。")
            return

        try:
            contract = self.api.Contracts.Stocks[stock_code]

            # 滑點保護
            if not self.check_slippage_safe(contract):
                return

            # VWAP
            ticks = self.api.ticks(contract, date=datetime.now().strftime("%Y-%m-%d"))
            df = ticks_to_df(ticks)
            vwap = ta.vwap(df["High"], df["Low"], df["Close"], df["Volume"]).iloc[-1]
            current_price = df["Close"].iloc[-1]
            print(f"[{stock_code}] 現價={current_price}  VWAP={vwap:.2f}")

            if current_price <= vwap:
                print(f"[{stock_code}] 現價未突破 VWAP，不進場。")
                return

            qty = int(POSITION_SIZE / current_price)
            if qty < 1:
                print(f"[{stock_code}] 資金不足購買 1 股，跳過。")
                return

            self._place_odd_order(contract, current_price, qty, sj.constant.Action.Buy)
            self.positions[stock_code] = Position(
                code=stock_code,
                entry_price=current_price,
                qty=qty,
            )
            send_notify(
                f"[買進] {stock_code}\n"
                f"價格: {current_price}  數量: {qty} 股\n"
                f"VWAP: {vwap:.2f}\n"
                f"情緒分: {score:+.2f}  {analysis}"
            )
        except Exception as e:
            print(f"[{stock_code}] 進場失敗: {e}")

    # ------------------------------------------------------------------
    # 出場監控：移動止盈 + 強制止損
    # ------------------------------------------------------------------
    def monitor_exit(self) -> None:
        """每輪皆執行，不受情緒/大盤過濾影響"""
        if not self.positions:
            return

        contracts = [self.api.Contracts.Stocks[code] for code in self.positions]
        snapshots = self.api.snapshots(contracts)

        for snap in snapshots:
            code = snap.code
            pos = self.positions.get(code)
            if pos is None:
                continue

            current = snap.close
            pos.update_max(current)

            profit  = pos.profit_pct(current)
            pullback = pos.pullback_pct(current)

            print(
                f"[監控] {code}  現價={current}  成本={pos.entry_price}"
                f"  獲利={profit:+.2%}  歷史高={pos.max_price}"
                f"  回吐={pullback:.2%}"
            )

            reason = None

            # A. 強制止損
            if profit <= -STOP_LOSS_PCT:
                reason = f"強制止損（虧損 {profit:.2%}）"

            # B. 移動止盈：曾獲利超過啟動點，且從高點回吐超過觸發點
            elif profit > TRAILING_START and pullback >= TRAILING_PULLBACK:
                # 賣出前再次確認滑點
                contract = self.api.Contracts.Stocks[code]
                if self.check_slippage_safe(contract):
                    reason = f"移動止盈（高點 {pos.max_price}，回吐 {pullback:.2%}，獲利 {profit:.2%}）"

            if reason:
                self._execute_exit(code, current, reason)

    def _execute_exit(self, code: str, price: float, reason: str) -> None:
        pos = self.positions.get(code)
        if pos is None:
            return
        contract = self.api.Contracts.Stocks[code]
        # 查詢實際持倉數量
        try:
            held = self.api.list_positions(self.api.stock_account)
            hold = next((p for p in held if p.code == code), None)
            qty = hold.quantity if hold else pos.qty
        except Exception:
            qty = pos.qty

        self._place_odd_order(contract, price, qty, sj.constant.Action.Sell)
        profit_pct = pos.profit_pct(price)
        del self.positions[code]
        send_notify(
            f"[賣出] {code}  {reason}\n"
            f"賣出價: {price}  獲利: {profit_pct:+.2%}\n"
            f"成本: {pos.entry_price}  數量: {qty} 股"
        )

    # ------------------------------------------------------------------
    # 零股下單
    # ------------------------------------------------------------------
    def _place_odd_order(self, contract, price: float, qty: int, action) -> None:
        order = self.api.Order(
            price=price,
            quantity=qty,
            action=action,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_lot=sj.constant.StockOrderLot.IntradayOdd,
            account=self.api.stock_account,
        )
        trade = self.api.place_order(contract, order)
        print(f"[下單] {action} {contract.code} x{qty} @ {price}  狀態: {trade.status.status}")

    def logout(self) -> None:
        self.api.logout()
        print("[系統] 已登出")


# =============================================================================
# 5. 主程式
# =============================================================================

if __name__ == "__main__":
    bot = AITradingBot()
    market_agg = NewsAggregator(stock_code="")

    print("=" * 55)
    print("AI 模擬交易系統啟動（simulation=True）")
    print(f"漏斗掃描：每日 {FUNNEL_SCAN_HOUR:02d}:{FUNNEL_SCAN_MINUTE:02d} 動態更新監控清單")
    print(f"最大部位：{MAX_POSITIONS}  單筆：{POSITION_SIZE:,} 元")
    print(f"止損：{STOP_LOSS_PCT:.0%}  移動止盈啟動：{TRAILING_START:.1%}  回吐：{TRAILING_PULLBACK:.1%}")
    print(f"滑點上限：{SLIPPAGE_LIMIT:.1%}")
    print("=" * 55)

    # ── 啟動分析 ──
    print("[啟動分析] 抓取新聞中...")
    startup_news   = market_agg.fetch_headlines(limit=10)
    startup_digest = market_agg.format_telegram_digest(limit=10)
    startup_score, startup_analysis = (
        get_ai_sentiment(startup_news) if startup_news else (0.0, "無法取得新聞")
    )
    print(f"[啟動分析] 情緒分: {startup_score:+.2f}  {startup_analysis}")

    send_notify(
        f"[AI Trade 啟動]\n"
        f"模式：simulation=True\n"
        f"部位上限：{MAX_POSITIONS} 檔 | 單筆：{POSITION_SIZE:,} 元\n"
        f"止損 {STOP_LOSS_PCT:.0%} | 移動止盈 {TRAILING_START:.1%}→{TRAILING_PULLBACK:.1%} | 滑點 {SLIPPAGE_LIMIT:.1%}\n"
        f"漏斗掃描：每日 {FUNNEL_SCAN_HOUR:02d}:{FUNNEL_SCAN_MINUTE:02d} 動態更新監控清單\n"
        f"啟動時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"\n[啟動情緒分析]\n"
        f"分數：{startup_score:+.2f}  {sentiment_label(startup_score)}\n"
        f"摘要：{startup_analysis}\n"
        f"\n[最新新聞]\n{startup_digest}"
    )

    last_digest_sent: float = time.time()

    try:
        while True:
            now = datetime.now()
            in_market = (
                (now.hour == 9 and now.minute >= 5)
                or (9 < now.hour < 13)
                or (now.hour == 13 and now.minute <= 25)
            )

            if in_market:
                print(f"\n[{now.strftime('%H:%M:%S')}] 交易時間掃描  部位：{list(bot.positions.keys()) or '無'}")

                # 出場監控（每輪必跑，不受任何過濾影響）
                bot.monitor_exit()

                # 漏斗掃描：09:20 首次觸發，動態更新 watch_list
                bot.run_funnel_if_needed(now)

                # 尚未執行漏斗掃描（開盤前 15 分鐘），跳過進場
                if not bot.watch_list:
                    print(f"[策略] 等待漏斗掃描（{FUNNEL_SCAN_HOUR:02d}:{FUNNEL_SCAN_MINUTE:02d}）...")
                    time.sleep(SCAN_INTERVAL)
                    continue

                # 大盤過濾
                if not bot.check_market_trend():
                    print("[策略] 大盤月線以下，跳過進場掃描。")
                    time.sleep(SCAN_INTERVAL)
                    continue

                # 市場情緒分析（每輪一次，覆蓋所有標的）
                news_text = market_agg.fetch_headlines(limit=10)
                if not news_text:
                    print("[新聞] 無法取得今日新聞，跳過本輪。")
                    time.sleep(SCAN_INTERVAL)
                    continue

                score, analysis = get_ai_sentiment(news_text)
                print(f"[AI] 市場情緒 {score:+.2f}  {sentiment_label(score)}  {analysis}")
                send_notify(
                    f"[AI 市場情緒] {now.strftime('%H:%M')}\n"
                    f"分數：{score:+.2f}  {sentiment_label(score)}\n"
                    f"摘要：{analysis}"
                )

                if score > 0.6:
                    # 對漏斗精選清單每支股票掃描進場條件
                    for code in bot.watch_list:
                        bot.scan_and_buy(code, score, analysis)
                else:
                    print(f"[策略] 市場情緒不足（{score:.2f}），不進場。")

            else:
                print(f"[{now.strftime('%H:%M:%S')}] 非交易時間  部位：{list(bot.positions.keys()) or '無'}")
                if time.time() - last_digest_sent >= NEWS_DIGEST_INTERVAL:
                    digest = market_agg.format_telegram_digest(limit=10)
                    send_notify(
                        f"[新聞摘要] {now.strftime('%Y-%m-%d %H:%M')}\n"
                        f"監控：{', '.join(WATCH_LIST)}\n"
                        f"{'─' * 28}\n{digest}"
                    )
                    last_digest_sent = time.time()

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\n[系統] 使用者中止。")
    finally:
        bot.logout()
