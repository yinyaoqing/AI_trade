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
from collections import deque
from dataclasses import dataclass, field

os.environ.setdefault("PYTHONUNBUFFERED", "1")
sys.stdout.reconfigure(line_buffering=True)

import shioaji as sj
import pandas as pd
import pandas_ta as ta
import requests
from openai import OpenAI
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

TZ_TW = timezone(timedelta(hours=8))  # 台灣時間 UTC+8


def now_tw() -> datetime:
    """回傳台灣當地時間（不論伺服器在哪）"""
    return datetime.now(TZ_TW)
from src.ai_trade.news import NewsAggregator
from src.ai_trade.scanner import FunnelScanner
from src.ai_trade.chips import chips_sentiment, chips_summary           # 2.2 籌碼流向
from src.ai_trade.strategy import (                                      # 3.2 多策略
    StrategyAllocator, mean_reversion_signal, MarketRegime, AllocationResult
)

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

STOP_LOSS_PCT        = 0.025   # 強制止損：虧損 2.5%（回測驗證：2% 橫盤假止損過多，3% 最大回撤控制較優，折衷取 2.5%）
TRAILING_START       = 0.015   # 移動止盈啟動點：獲利達 1.5%
TRAILING_PULLBACK    = 0.01    # 移動止盈觸發（ATR 不足時的保底固定回撤）
TRAILING_ATR_MULT    = 0.6     # 動態回撤：從最高點回落 0.6×ATR 時出場（ATR 夠大時優先）
BREAKEVEN_TRIGGER    = 0.02    # 成本保衛：獲利達 2% 時自動將止損上移至成本價
TIME_STOP_MINUTES    = 30      # 時間停損：進場後 X 分鐘仍在成本區則主動出場
TIME_STOP_BAND       = 0.005   # 成本區定義：距進場價 ±0.5% 以內視為「原地踏步」
SLIPPAGE_LIMIT       = 0.005   # 滑點保護：買賣價差 > 0.5% 不交易

# Phase 1 優化參數
SENTIMENT_ENABLED  = False   # 新聞情緒評分開關：False → 跳過 AI 分析，直接進入策略掃描
SENTIMENT_SMOOTH_N = 3      # 1.1 情緒平滑：保留最近 N 次分數取均值
RISK_PER_TRADE     = TOTAL_BUDGET * STOP_LOSS_PCT   # 1.2 ATR 動態部位：每筆承擔最大損失 (元)
RSI_OVERBOUGHT     = 70                             # 1.3 RSI 超買門檻：超過不進場

# Phase 2 優化參數
TRADE_COST_PCT     = 0.004   # 2.3 手續費+證交稅估算（買0.1425%+賣0.1425%+賣0.3% ≈ 0.585%，保守用0.4%）

# 進場條件強化參數
RVOL_MIN           = 1.5     # 相對成交量門檻：現量須為 5 日均量的 1.5 倍以上（量能確認突破）
RSI_DYNAMIC        = True    # 動態 RSI：上升趨勢中允許放寬至 RSI_OVERBOUGHT_LAX
RSI_OVERBOUGHT_LAX = 75      # 動態 RSI 放寬門檻（RSI 持續向上時適用）
VWAP_MAX_GAP       = 0.03    # VWAP 乖離率上限：現價超過 VWAP 3% 視為過熱，不追
ATR_MAX_PCT        = 0.03    # ATR 過熱保護：ATR/股價 > 3% 視為跳空風險過高，不進場
MA_TREND_PERIOD    = 50      # 趨勢過濾均線：個股現價需在 MA50 之上才進場（回測驗證有效）
MARKET_INDEX       = "0050"  # 大盤指數代碼（主板用 0050，中小型股可改 0051）

SCAN_INTERVAL          = 60    # 主循環間隔（秒）
NEWS_DIGEST_INTERVAL   = 1800  # 非交易時間新聞推播間隔（秒）

# 固定監控標的（不受漏斗掃描影響，每輪必掃）
# 回測驗證後精選（2021–2026 yfinance 日K，PF ≥ 1.2、夏普 ≥ 1.0、淨損益為正）
# 移除：2317(PF=0.93)、3037(PF=0.53)、2383(PF=0.43)、2368(PF=0.67)、3661/3443(無成交)、6805(資料不足)
PINNED_STOCKS: tuple[str, ...] = ("2059", "8210", "3324", "2454", "3017", "2330", "8996")

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
    # 2.1 ATR 自適應止損：進場時計算，取代固定百分比
    atr:         float = 0.0   # 進場時 ATR 值（元）
    stop_price:  float = 0.0   # 動態止損價（entry - 1.5×ATR，最多 -2%）
    trail_price: float = 0.0   # 移動止盈啟動價（entry + 1.0×ATR，最少 1.5%）
    # 進場輔助資訊（供績效日誌 2.3 使用）
    entry_score: float = 0.0
    entry_rsi:   float = 0.0
    entry_vwap:  float = 0.0
    entry_chips: float = 0.0   # 法人淨買超（股，2.2）
    max_price:   float = field(init=False)
    entry_time:  datetime = field(init=False)   # 進場時間（time stop 使用）

    def __post_init__(self):
        self.max_price  = self.entry_price
        self.entry_time = now_tw()
        # 若未帶入 ATR，退回固定百分比
        if self.stop_price == 0.0:
            self.stop_price = self.entry_price * (1 - STOP_LOSS_PCT)
        if self.trail_price == 0.0:
            self.trail_price = self.entry_price * (1 + TRAILING_START)

    def update_max(self, current: float) -> None:
        if current > self.max_price:
            self.max_price = current

    def profit_pct(self, current: float) -> float:
        return (current - self.entry_price) / self.entry_price

    def pullback_pct(self, current: float) -> float:
        if self.max_price <= 0:
            return 0.0
        return (self.max_price - current) / self.max_price


@dataclass
class BuyCandidate:
    """掃描階段收集的候選進場標的，尚未下單"""
    code:        str
    strategy:    str    # "momentum" | "mean_reversion"
    price:       float
    qty:         int
    vwap:        float
    rsi:         float
    chip_score:  float
    atr_val:     float
    stop_price:  float
    trail_price: float
    score:       float  # 排序依據：VWAP 突破幅度 × 0.5 + 法人情緒 × 0.5

    def describe(self) -> str:
        tag = "動能" if self.strategy == "momentum" else "均值回歸"
        return (f"[候選/{tag}] {self.code}  "
                f"價={self.price}  VWAP={self.vwap:.2f}  "
                f"RSI={self.rsi:.1f}  法人={self.chip_score:+.2f}  "
                f"綜合分={self.score:.3f}")

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
    """將 Shioaji ticks 轉為 DataFrame，統一欄位名稱為 pandas_ta 所需格式（大寫）"""
    df = pd.DataFrame({**ticks.model_dump()})
    df["datetime"] = pd.to_datetime(df["ts"])
    df = df.set_index("datetime").sort_index()
    # Shioaji ticks 欄位皆為小寫，pandas_ta.vwap 需要大寫
    rename = {"open": "Open", "high": "High", "low": "Low",
              "close": "Close", "volume": "Volume", "amount": "Amount"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    # ticks 只有成交價（close），補齊 High/Low/Open 供 VWAP 計算
    for col in ("High", "Low", "Open"):
        if col not in df.columns and "Close" in df.columns:
            df[col] = df["Close"]
    return df


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
        self.watch_list: list[str] = list(PINNED_STOCKS)  # 固定標的，漏斗結果會合併
        self.funnel = FunnelScanner(self.api, get_ai_sentiment)
        self._funnel_done_today: str = ""        # 記錄已執行掃描的日期
        self._sentiment_scores: deque[float] = deque(maxlen=SENTIMENT_SMOOTH_N)  # 1.1 情緒平滑
        self.allocator = StrategyAllocator(self.api)                             # 3.2 多策略分配器
        self._last_regime: str = ""              # 策略配置上次推播的 regime，相同則不重複推播

        # 查詢帳戶餘額，動態決定實際可用預算
        self._init_budget()

        # 啟動時同步實際持倉
        self._sync_positions_from_api()

    def _init_budget(self) -> None:
        """查詢帳戶餘額，若小於 TOTAL_BUDGET 則以實際餘額為上限"""
        global TOTAL_BUDGET, POSITION_SIZE, RISK_PER_TRADE
        try:
            bal = self.api.account_balance()
            available = float(bal.acc_balance)
            if available <= 0:
                # 模擬帳戶不支援 account_balance，回傳 0，沿用設定值
                print(f"[預算] 帳戶餘額查詢回傳 0（模擬帳戶限制），沿用設定值 {TOTAL_BUDGET:,} 元")
                return
            effective = min(available, TOTAL_BUDGET)
            print(f"[預算] 帳戶餘額：{available:,.0f} 元  設定上限：{TOTAL_BUDGET:,} 元  → 實際預算：{effective:,.0f} 元")
            if effective != TOTAL_BUDGET:
                TOTAL_BUDGET  = effective
                POSITION_SIZE = int(TOTAL_BUDGET // MAX_POSITIONS)
                RISK_PER_TRADE = TOTAL_BUDGET * STOP_LOSS_PCT
                print(f"[預算] 已調整 POSITION_SIZE={POSITION_SIZE:,} 元  RISK_PER_TRADE={RISK_PER_TRADE:,.0f} 元")
        except Exception as e:
            print(f"[預算] 查詢餘額失敗，沿用設定值 {TOTAL_BUDGET:,} 元：{e}")

    # ------------------------------------------------------------------
    # 持倉同步：將 API 實際持倉載入 self.positions
    # ------------------------------------------------------------------
    def _sync_positions_from_api(self) -> None:
        """查詢券商實際持倉，載入 self.positions，避免重啟後遺漏持股"""
        try:
            held = self.api.list_positions(self.api.stock_account)
            if not held:
                print("[持倉] 目前無持股")
                return

            print(f"[持倉] 查詢到 {len(held)} 筆持股，同步中...")
            for p in held:
                code = p.code
                if code in self.positions:
                    continue  # 已有紀錄，不覆蓋
                avg_price = getattr(p, "price", None) or getattr(p, "average_price", 0)
                qty       = getattr(p, "quantity", 0)
                self.positions[code] = Position(
                    code=code,
                    entry_price=float(avg_price),
                    qty=int(qty),
                )
                last  = float(getattr(p, "last_price", avg_price) or avg_price)
                pnl   = (last - float(avg_price)) * int(qty)
                print(
                    f"  {code}  均價={avg_price}  持股={qty}股  "
                    f"現值≈{last}  損益={pnl:+.0f}元"
                )
        except Exception as e:
            print(f"[持倉] 查詢失敗: {e}")

    def get_positions_summary(self) -> str:
        """回傳持倉摘要字串（供啟動通知與定時推播使用）"""
        try:
            held = self.api.list_positions(self.api.stock_account)
        except Exception as e:
            return f"（持倉查詢失敗: {e}）"

        if not held:
            return "目前無持股"

        lines = []
        total_pnl = 0.0
        for p in held:
            code      = p.code
            qty       = int(getattr(p, "quantity", 0))
            avg_price = float(getattr(p, "price", None) or getattr(p, "average_price", 0))
            last      = float(getattr(p, "last_price", avg_price) or avg_price)
            # 自行計算損益，避免 API pnl 欄位單位不一致問題
            pnl = (last - avg_price) * qty
            total_pnl += pnl
            pct = (last - avg_price) / avg_price * 100 if avg_price else 0
            lines.append(
                f"  {code}  {qty}股  均價={avg_price}  現價={last}  "
                f"損益={pnl:+.0f}元 ({pct:+.2f}%)"
            )
        lines.append(f"  合計未實現損益：{total_pnl:+.0f} 元")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 漏斗掃描：每日 09:20 執行一次，動態更新監控清單
    # ------------------------------------------------------------------
    def run_funnel_if_needed(self, now: datetime) -> None:
        """09:20 後且今日尚未執行，才觸發漏斗掃描。
        若 Bot 在 09:20 之後才啟動，當輪立即補跑一次。"""
        today = now.strftime("%Y-%m-%d")
        if self._funnel_done_today == today:
            return
        past_scan_time = (now.hour > FUNNEL_SCAN_HOUR or
                          (now.hour == FUNNEL_SCAN_HOUR and now.minute >= FUNNEL_SCAN_MINUTE))
        if not past_scan_time:
            return

        results = self.funnel.run(max_results=FUNNEL_MAX_RESULTS)
        self._funnel_done_today = today

        if not results:
            print("[漏斗] 今日無精選標的，維持現有監控清單。")
            return

        funnel_codes = [r.code for r in results]
        # 固定標的永遠保留，漏斗結果去重合併
        merged = list(dict.fromkeys(list(PINNED_STOCKS) + funnel_codes))
        self.watch_list = merged
        print(f"[漏斗] 固定標的：{list(PINNED_STOCKS)}  漏斗精選：{funnel_codes}")
        print(f"[漏斗] 合併監控清單：{self.watch_list}")

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
    # 1.1 情緒平滑：加入新分數並回傳移動平均
    # ------------------------------------------------------------------
    def smooth_sentiment(self, raw: float) -> float:
        self._sentiment_scores.append(raw)
        smoothed = sum(self._sentiment_scores) / len(self._sentiment_scores)
        if len(self._sentiment_scores) > 1:
            print(f"[情緒平滑] 原始={raw:+.2f}  近{len(self._sentiment_scores)}次均值={smoothed:+.2f}")
        return smoothed

    # ------------------------------------------------------------------
    # 1.2 ATR 動態部位：依個股波動率計算合理股數
    # ------------------------------------------------------------------
    def get_atr_qty(self, contract, current_price: float) -> int:
        """回傳 ATR-based 股數（風險均等化），上限為固定預算所能買到的最大股數"""
        fallback = max(int(POSITION_SIZE / current_price), 1)
        try:
            end_date   = now_tw().strftime("%Y-%m-%d")
            start_date = (now_tw() - timedelta(days=60)).strftime("%Y-%m-%d")
            kbars = self.api.kbars(contract, start=start_date, end=end_date)
            df = pd.DataFrame({**kbars.model_dump()}).sort_values("ts")
            if len(df) < 15:
                return fallback
            atr = ta.atr(df["High"], df["Low"], df["Close"], length=14).iloc[-1]
            if not atr or pd.isna(atr) or atr <= 0:
                return fallback
            qty_by_risk   = int(RISK_PER_TRADE / atr)          # 風險控制上限
            qty_by_budget = int(POSITION_SIZE / current_price)  # 預算上限
            qty = max(min(qty_by_risk, qty_by_budget), 1)
            print(f"[ATR] {contract.code}  ATR={atr:.2f}  風險部位={qty_by_risk}股  預算上限={qty_by_budget}股  → {qty}股")
            return qty
        except Exception as e:
            print(f"[ATR] {contract.code} 計算失敗: {e}，改用預算法")
            return fallback

    # ------------------------------------------------------------------
    # 大盤趨勢過濾
    # ------------------------------------------------------------------
    def check_market_trend(self) -> bool:
        """0050 收盤價是否在 20 日均線之上"""
        try:
            contract = self.api.Contracts.Stocks[MARKET_INDEX]
            kbars = self.api.kbars(
                contract,
                start=(now_tw() - timedelta(days=90)).strftime("%Y-%m-%d"),
                end=now_tw().strftime("%Y-%m-%d"),
            )
            df = pd.DataFrame({**kbars.model_dump()}).set_index("ts").sort_index()
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
    # ------------------------------------------------------------------
    # 進場評估：掃描單一標的，回傳候選或 None（不下單）
    # ------------------------------------------------------------------
    def _eval_momentum(self, stock_code: str, sentiment_score: float) -> "BuyCandidate | None":
        """評估動能策略進場條件（VWAP 突破 + RSI + 法人），不執行下單"""
        if stock_code in self.positions:
            print(f"[{stock_code}] 已持有，跳過。")
            return None
        try:
            contract = self.api.Contracts.Stocks[stock_code]
            if not self.check_slippage_safe(contract):
                return None

            ticks = self.api.ticks(contract, date=now_tw().strftime("%Y-%m-%d"))
            df = ticks_to_df(ticks)
            vwap = ta.vwap(df["High"], df["Low"], df["Close"], df["Volume"]).iloc[-1]
            current_price = df["Close"].iloc[-1]

            # ── RSI 計算 ──────────────────────────────────────────────
            rsi_series = ta.rsi(df["Close"], length=14)
            rsi_val = float(rsi_series.iloc[-1]) if (rsi_series is not None and not rsi_series.empty) else 50.0

            # ── 動態 RSI 門檻（Gemini 建議 2）─────────────────────────
            # RSI 持續上升（近 3 根斜率為正）代表趨勢強勁，放寬至 RSI_OVERBOUGHT_LAX
            rsi_threshold = RSI_OVERBOUGHT
            if RSI_DYNAMIC and rsi_series is not None and len(rsi_series) >= 4:
                rsi_slope = rsi_series.iloc[-1] - rsi_series.iloc[-4]   # 近 3 步的變化
                if rsi_slope > 0:
                    rsi_threshold = RSI_OVERBOUGHT_LAX

            # ── VWAP 乖離率（Gemini 建議 4）──────────────────────────
            vwap_gap = (current_price - vwap) / vwap   # 正值 = 高於 VWAP 多少 %

            print(f"[動能/{stock_code}] 現價={current_price}  VWAP={vwap:.2f}  "
                  f"乖離={vwap_gap:+.2%}  RSI={rsi_val:.1f}(門檻={rsi_threshold})")

            if rsi_val >= rsi_threshold:
                print(f"[動能/{stock_code}] RSI={rsi_val:.1f} ≥ {rsi_threshold}，超買，跳過。")
                return None
            if vwap_gap <= 0:
                print(f"[動能/{stock_code}] 現價未突破 VWAP，跳過。")
                return None
            if vwap_gap > VWAP_MAX_GAP:
                print(f"[動能/{stock_code}] VWAP 乖離 {vwap_gap:.2%} > {VWAP_MAX_GAP:.0%}，過熱追高，跳過。")
                return None

            # ── 法人籌碼 ─────────────────────────────────────────────
            chip_score = chips_sentiment(stock_code)
            print(f"  {chips_summary(stock_code)}  法人分: {chip_score:+.2f}")
            if chip_score < -0.3:
                print(f"[動能/{stock_code}] 法人持續賣超，跳過。")
                return None

            # ── 相對成交量 RVOL（Gemini 建議 1）─────────────────────
            rvol = 1.0
            try:
                end_d   = now_tw().strftime("%Y-%m-%d")
                start_d = (now_tw() - timedelta(days=7)).strftime("%Y-%m-%d")
                kb5 = self.api.kbars(contract, start=start_d, end=end_d)
                kdf5 = pd.DataFrame({**kb5.model_dump()}).sort_values("ts")
                if len(kdf5) >= 2:
                    avg_vol = kdf5["Volume"].iloc[:-1].mean()   # 排除今天，取前幾日均量
                    today_vol = float(df["Volume"].sum())
                    rvol = today_vol / avg_vol if avg_vol > 0 else 1.0
            except Exception:
                pass
            print(f"  RVOL={rvol:.2f}（門檻={RVOL_MIN}）")
            if rvol < RVOL_MIN:
                print(f"[動能/{stock_code}] 量能不足（RVOL={rvol:.2f} < {RVOL_MIN}），跳過。")
                return None

            # ── ATR 動態部位與止損 ────────────────────────────────────
            qty = self.get_atr_qty(contract, current_price)
            if qty < 1:
                return None

            atr_val = 0.0
            kdf = pd.DataFrame()
            try:
                kb  = self.api.kbars(contract, start=start_d, end=end_d)
                kdf = pd.DataFrame({**kb.model_dump()}).sort_values("ts")
                atr_s = ta.atr(kdf["High"], kdf["Low"], kdf["Close"], length=14)
                atr_val = float(atr_s.iloc[-1]) if atr_s is not None and not atr_s.empty else 0.0
            except Exception:
                pass

            # ── ATR 過熱保護（跳空缺口風險）────────────────────────────
            if atr_val > 0 and (atr_val / current_price) > ATR_MAX_PCT:
                print(f"[動能/{stock_code}] ATR過熱 {atr_val/current_price:.2%} > {ATR_MAX_PCT:.0%}，跳過。")
                return None

            # ── MA50 趨勢過濾（回測驗證：加入後最大回撤從 -43% 降至 -19%）────
            if len(kdf) >= MA_TREND_PERIOD:
                ma50 = kdf["Close"].rolling(MA_TREND_PERIOD).mean().iloc[-1]
                if not pd.isna(ma50) and current_price < ma50:
                    print(f"[動能/{stock_code}] 現價 {current_price} < MA50 {ma50:.1f}，下降趨勢，跳過。")
                    return None

            # 止損：ATR 止損與固定止損取較嚴格者（止損價較高 = 損失較小），防跳空打滑
            atr_stop_p = current_price - 1.5 * atr_val
            pct_stop_p = current_price * (1 - STOP_LOSS_PCT)
            stop_p  = max(atr_stop_p, pct_stop_p)
            trail_p = current_price + max(1.0 * atr_val, current_price * TRAILING_START)

            # ── 綜合排序分：VWAP 突破幅度 40% + 法人情緒 40% + 量能 20%
            chip_norm  = (chip_score + 1) / 2          # -1~1 → 0~1
            rvol_norm  = min(rvol / 3.0, 1.0)          # 0~3x → 0~1（超過 3 倍不繼續加分）
            rank_score = vwap_gap * 0.4 + chip_norm * 0.4 + rvol_norm * 0.2

            return BuyCandidate(
                code=stock_code, strategy="momentum",
                price=current_price, qty=qty,
                vwap=float(vwap), rsi=rsi_val, chip_score=chip_score,
                atr_val=atr_val, stop_price=stop_p, trail_price=trail_p,
                score=rank_score,
            )
        except Exception as e:
            print(f"[動能/{stock_code}] 評估失敗: {e}")
            return None

    def _eval_mean_reversion(self, stock_code: str, budget: float) -> "BuyCandidate | None":
        """評估均值回歸進場條件（RSI<30 + 現價<VWAP），不執行下單"""
        if stock_code in self.positions:
            return None
        try:
            contract = self.api.Contracts.Stocks[stock_code]
            if not self.check_slippage_safe(contract):
                return None

            ticks = self.api.ticks(contract, date=now_tw().strftime("%Y-%m-%d"))
            df    = ticks_to_df(ticks)
            sig   = mean_reversion_signal(df, stock_code)

            print(f"[均值回歸/{stock_code}]  {sig.reason}")
            if sig.action != "BUY":
                return None

            chip_score = chips_sentiment(stock_code)
            if chip_score < -0.5:
                print(f"[均值回歸/{stock_code}] 法人大幅賣超，跳過。")
                return None

            qty = max(int(budget / sig.current_price), 1)
            # 排序分：RSI 低於 30 的距離（越低越強）+ 法人情緒
            rsi_gap   = max(30 - sig.rsi, 0) / 30               # 0~1
            chip_norm = (chip_score + 1) / 2
            rank_score = rsi_gap * 0.5 + chip_norm * 0.5

            return BuyCandidate(
                code=stock_code, strategy="mean_reversion",
                price=sig.current_price, qty=qty,
                vwap=sig.vwap, rsi=sig.rsi, chip_score=chip_score,
                atr_val=0.0,
                stop_price=sig.current_price * (1 - STOP_LOSS_PCT),
                trail_price=sig.current_price * (1 + TRAILING_START),
                score=rank_score,
            )
        except Exception as e:
            print(f"[均值回歸/{stock_code}] 評估失敗: {e}")
            return None

    def _execute_buy(self, c: "BuyCandidate", sentiment_score: float, analysis: str) -> None:
        """對已通過評估的候選標的執行買進下單"""
        contract = self.api.Contracts.Stocks[c.code]
        ok = self._place_odd_order(contract, c.price, c.qty, sj.constant.Action.Buy)
        if not ok:
            print(f"[買進] {c.code} 下單被拒，跳過。")
            return
        pos = Position(
            code=c.code,
            entry_price=c.price,
            qty=c.qty,
            atr=c.atr_val,
            stop_price=c.stop_price,
            trail_price=c.trail_price,
            entry_score=sentiment_score,
            entry_rsi=c.rsi,
            entry_vwap=c.vwap,
            entry_chips=c.chip_score,
        )
        self.positions[c.code] = pos
        self._trade_log("BUY", pos, c.price)
        tag = "買進" if c.strategy == "momentum" else "均值回歸買進"
        send_notify(
            f"[{tag}] {c.code}\n"
            f"價格: {c.price}  數量: {c.qty} 股\n"
            f"VWAP: {c.vwap:.2f}  RSI: {c.rsi:.1f}  法人: {c.chip_score:+.2f}\n"
            f"止損價: {c.stop_price:.2f}  止盈啟動: {c.trail_price:.2f}\n"
            f"ATR: {c.atr_val:.2f}  情緒: {sentiment_score:+.2f}  {analysis}"
        )

    def scan_candidates(
        self,
        watch_list: list,
        sentiment_score: float,
        analysis: str,
        alloc: "AllocationResult",
    ) -> None:
        """
        全局候選掃描：
        1. 對 watch_list 所有標的評估，收集通過條件的候選清單
        2. 依綜合評分排序（高分優先）
        3. 依序下單，直到部位滿為止
        """
        slots = MAX_POSITIONS - len(self.positions)
        if slots <= 0:
            return

        from src.ai_trade.strategy import MarketRegime
        candidates: list[BuyCandidate] = []

        # ── 評估階段（全部掃完）──────────────────────────────────
        for code in watch_list:
            if code in self.positions:
                print(f"[{code}] 已持有，跳過。")
                continue
            if alloc.regime == MarketRegime.RANGING:
                mr_budget = POSITION_SIZE * alloc.mean_reversion_budget_pct
                c = self._eval_mean_reversion(code, mr_budget)
                if c:
                    candidates.append(c)
                # 盤整市仍允許動能策略作補充
                c2 = self._eval_momentum(code, sentiment_score)
                if c2:
                    candidates.append(c2)
            else:
                c = self._eval_momentum(code, sentiment_score)
                if c:
                    candidates.append(c)
                # 趨勢市也收集均值回歸作補充
                mr_budget = POSITION_SIZE * alloc.mean_reversion_budget_pct
                c2 = self._eval_mean_reversion(code, mr_budget)
                if c2:
                    candidates.append(c2)

        if not candidates:
            print("[掃描] 本輪無符合條件的候選標的。")
            return

        # ── 排序階段（綜合評分高分優先）──────────────────────────
        # 同一股票若兩種策略都入選，只保留分數較高者
        best: dict[str, BuyCandidate] = {}
        for c in candidates:
            if c.code not in best or c.score > best[c.code].score:
                best[c.code] = c

        ranked = sorted(best.values(), key=lambda x: x.score, reverse=True)
        print(f"[掃描] 候選 {len(ranked)} 檔（排序後）：")
        for c in ranked:
            print(f"  {c.describe()}")

        # ── 執行階段（高分優先，直到部位滿）──────────────────────
        for c in ranked:
            if len(self.positions) >= MAX_POSITIONS:
                break
            if c.code in self.positions:
                continue
            self._execute_buy(c, sentiment_score, analysis)

    # ------------------------------------------------------------------
    # 出場監控：移動止盈 + 強制止損
    # ------------------------------------------------------------------

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

            # 盤中零股規則：當日進場的部位 T+1 才能賣，跳過出場監控避免當沖
            if pos.entry_time.date() == now_tw().date():
                print(f"[監控] {code} 今日新建部位（零股 T+1），跳過當日出場。")
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

            # ── A. ATR 自適應止損 ────────────────────────────────────
            if current <= pos.stop_price:
                reason = (f"止損（現價{current} ≤ 止損價{pos.stop_price:.2f}，"
                          f"虧損{profit:.2%}，ATR={pos.atr:.2f}）")

            # ── B. 成本保衛（Break-even Stop）────────────────────────
            # 獲利達 BREAKEVEN_TRIGGER 後，止損價自動上移至進場成本（不再允許虧損）
            elif profit >= BREAKEVEN_TRIGGER and pos.stop_price < pos.entry_price:
                pos.stop_price = pos.entry_price
                print(f"[成本保衛] {code}  獲利已達{profit:.2%}，止損上移至成本 {pos.entry_price}")

            # ── C. 動態移動止盈 ───────────────────────────────────────
            # 啟動後：從最高點回落 0.6×ATR（ATR 夠大時）或固定 TRAILING_PULLBACK
            elif current >= pos.trail_price:
                atr_pullback = TRAILING_ATR_MULT * pos.atr if pos.atr > 0 else 0
                pullback_threshold = max(atr_pullback / pos.max_price, TRAILING_PULLBACK)
                if pullback >= pullback_threshold:
                    contract = self.api.Contracts.Stocks[code]
                    if self.check_slippage_safe(contract):
                        reason = (f"移動止盈（高點{pos.max_price}，"
                                  f"回吐{pullback:.2%}≥門檻{pullback_threshold:.2%}，"
                                  f"獲利{profit:.2%}）")

            # ── D. 時間停損（Time Stop）───────────────────────────────
            # 進場後 TIME_STOP_MINUTES 分鐘內，價格仍在成本 ±TIME_STOP_BAND 區間
            # 且尚未啟動移動止盈（未突破 trail_price）→ 動能消失，主動離場
            if not reason:
                held_mins = (now_tw() - pos.entry_time).total_seconds() / 60
                in_band   = abs(profit) <= TIME_STOP_BAND
                not_trailed = current < pos.trail_price
                if held_mins >= TIME_STOP_MINUTES and in_band and not_trailed:
                    reason = (f"時間停損（持有{held_mins:.0f}分，"
                              f"價格停滯{profit:+.2%}，動能消失）")

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

        ok = self._place_odd_order(contract, price, qty, sj.constant.Action.Sell)
        if not ok:
            print(f"[警告] {code} 賣單被拒，部位保留，下輪繼續監控。")
            return
        profit_pct = pos.profit_pct(price)
        net_pnl    = (price - pos.entry_price) * qty * (1 - TRADE_COST_PCT)
        self._trade_log("SELL", pos, price, reason=reason)   # 2.3
        del self.positions[code]
        send_notify(
            f"[賣出] {code}  {reason}\n"
            f"賣出價: {price}  獲利: {profit_pct:+.2%}\n"
            f"成本: {pos.entry_price}  數量: {qty} 股\n"
            f"淨損益: {net_pnl:+.0f} 元"
        )

    # ------------------------------------------------------------------
    # 2.3 績效日誌：每筆進出場寫入 logs/trades_YYYYMMDD.csv
    # ------------------------------------------------------------------
    def _trade_log(self, action: str, pos: "Position", price: float, reason: str = "") -> None:
        import csv, pathlib
        log_dir = pathlib.Path("logs")
        log_dir.mkdir(exist_ok=True)
        today   = now_tw().strftime("%Y%m%d")
        fpath   = log_dir / f"trades_{today}.csv"
        is_new  = not fpath.exists()
        net_pnl = (price - pos.entry_price) * pos.qty * (1 - TRADE_COST_PCT) if action == "SELL" else 0.0
        row = {
            "timestamp":    now_tw().strftime("%Y-%m-%d %H:%M:%S"),
            "action":       action,
            "code":         pos.code,
            "price":        price,
            "qty":          pos.qty,
            "entry_price":  pos.entry_price,
            "stop_price":   round(pos.stop_price, 2),
            "trail_price":  round(pos.trail_price, 2),
            "atr":          round(pos.atr, 2),
            "entry_score":  round(pos.entry_score, 2),
            "entry_rsi":    round(pos.entry_rsi, 1),
            "entry_vwap":   round(pos.entry_vwap, 2),
            "entry_chips":  pos.entry_chips,
            "net_pnl":      round(net_pnl, 0),
            "reason":       reason,
        }
        with open(fpath, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if is_new:
                writer.writeheader()
            writer.writerow(row)
        print(f"[日誌] {action} {pos.code} @ {price}  寫入 {fpath}")

    # ------------------------------------------------------------------
    # 零股下單
    # ------------------------------------------------------------------
    def _place_odd_order(self, contract, price: float, qty: int, action) -> bool:
        """回傳 True 表示下單成功（op_code == '00'），False 表示交易所拒單"""
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
        op_code = getattr(getattr(trade, "operation", None), "op_code", "00")
        ok = (op_code == "00")
        status_str = trade.status.status if ok else f"拒單(op_code={op_code})"
        print(f"[下單] {action} {contract.code} x{qty} @ {price}  狀態: {status_str}")
        return ok

    def daily_summary(self) -> str:
        """產生今日交易總結，包含成交紀錄、損益與持倉狀況"""
        lines = [f"[今日交易總結] {now_tw().strftime('%Y-%m-%d')}"]
        lines.append("─" * 32)

        # 成交紀錄
        try:
            try:
                trades = self.api.list_trades(self.api.stock_account)
            except TypeError:
                trades = self.api.list_trades()   # 模擬帳戶不接受 account 參數
            today  = now_tw().strftime("%Y-%m-%d")
            today_trades = [
                t for t in (trades or [])
                if hasattr(t, "status") and
                str(getattr(t.status, "order_datetime", "")).startswith(today)
            ]
            if today_trades:
                lines.append(f"成交紀錄（{len(today_trades)} 筆）：")
                for t in today_trades:
                    action = getattr(t.order, "action", "-")
                    code   = getattr(t.contract, "code", "-")
                    price  = getattr(t.order, "price", "-")
                    qty    = getattr(t.order, "quantity", "-")
                    status = getattr(t.status, "status", "-")
                    lines.append(f"  {action} {code}  {qty}股 @ {price}  {status}")
            else:
                lines.append("成交紀錄：今日無成交")
        except Exception as e:
            lines.append(f"成交紀錄：查詢失敗 ({e})")

        lines.append("─" * 32)

        # 未實現損益（現有持倉）
        summary = self.get_positions_summary()
        lines.append(f"收盤持倉：\n{summary}")

        lines.append("─" * 32)

        # 已實現損益
        try:
            today_str = now_tw().strftime("%Y-%m-%d")
            pnl_list  = self.api.list_profit_loss(
                self.api.stock_account,
                begin_date=today_str,
                end_date=today_str,
            )
            if pnl_list:
                total_realized = sum(getattr(p, "profitloss", 0) or 0 for p in pnl_list)
                lines.append(f"已實現損益：{total_realized:+.0f} 元（{len(pnl_list)} 筆）")
            else:
                lines.append("已實現損益：今日無已實現損益")
        except Exception as e:
            lines.append(f"已實現損益：查詢失敗 ({e})")

        return "\n".join(lines)

    def logout(self) -> None:
        summary = self.daily_summary()
        print(f"\n{summary}")
        send_notify(summary)
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

    positions_summary = bot.get_positions_summary()
    print(f"[持倉]\n{positions_summary}")

    send_notify(
        f"[AI Trade 啟動]\n"
        f"模式：simulation=True\n"
        f"部位上限：{MAX_POSITIONS} 檔 | 單筆：{POSITION_SIZE:,} 元\n"
        f"止損 {STOP_LOSS_PCT:.0%} | 移動止盈 {TRAILING_START:.1%}→{TRAILING_PULLBACK:.1%} | 滑點 {SLIPPAGE_LIMIT:.1%}\n"
        f"漏斗掃描：每日 {FUNNEL_SCAN_HOUR:02d}:{FUNNEL_SCAN_MINUTE:02d} 動態更新監控清單\n"
        f"啟動時間：{now_tw().strftime('%Y-%m-%d %H:%M:%S')} CST\n"
        f"\n[目前持倉]\n{positions_summary}\n"
        f"\n[啟動情緒分析]\n"
        f"分數：{startup_score:+.2f}  {sentiment_label(startup_score)}\n"
        f"摘要：{startup_analysis}\n"
        f"\n[最新新聞]\n{startup_digest}"
    )

    last_digest_sent: float = time.time()

    try:
        while True:
            now = now_tw()   # 台灣時間
            in_market = (
                (now.hour == 9 and now.minute >= 5)
                or (9 < now.hour < 13)
                or (now.hour == 13 and now.minute <= 25)
            )

            if in_market:
                print(f"\n[{now.strftime('%H:%M:%S')} CST] 交易時間掃描  部位：{list(bot.positions.keys()) or '無'}")

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

                # 市場情緒分析（可透過 SENTIMENT_ENABLED 開關控制）
                if SENTIMENT_ENABLED:
                    news_text = market_agg.fetch_headlines(limit=10)
                    if not news_text:
                        print("[新聞] 無法取得今日新聞，跳過本輪。")
                        time.sleep(SCAN_INTERVAL)
                        continue

                    raw_score, analysis = get_ai_sentiment(news_text)
                    score = bot.smooth_sentiment(raw_score)   # 1.1 情緒平滑
                    print(f"[AI] 市場情緒 {score:+.2f}  {sentiment_label(score)}  {analysis}")
                    send_notify(
                        f"[AI 市場情緒] {now.strftime('%H:%M')}\n"
                        f"分數：{score:+.2f}  {sentiment_label(score)}\n"
                        f"摘要：{analysis}"
                    )
                else:
                    score    = 1.0   # 情緒關閉時視為中性偏多，直接進入策略掃描
                    analysis = "（情緒分析已關閉）"
                    print(f"[AI] 情緒分析已停用，以預設分數 {score:+.2f} 執行策略。")

                if score > 0.6:
                    # 3.2 多策略框架：依市場狀態決定策略比重
                    alloc = bot.allocator.allocate()
                    print(f"[策略] {alloc.describe()}")
                    # 策略配置：只在 regime 改變時推播，避免每分鐘重複通知
                    if alloc.regime.value != bot._last_regime:
                        bot._last_regime = alloc.regime.value
                        send_notify(
                            f"[策略配置變更] {alloc.regime.value}\n"
                            f"波動率：{alloc.vol_ann:.1%}\n"
                            f"動能：{alloc.momentum_budget_pct:.0%}  均值回歸：{alloc.mean_reversion_budget_pct:.0%}"
                        )

                    # 統一由 scan_candidates() 處理策略分配（已內建 RANGING/TRENDING 邏輯）
                    bot.scan_candidates(bot.watch_list, score, analysis, alloc)
                else:
                    print(f"[策略] 市場情緒不足（{score:.2f}），不進場。")

            else:
                print(f"[{now.strftime('%H:%M:%S')} CST] 非交易時間  部位：{list(bot.positions.keys()) or '無'}")
                if time.time() - last_digest_sent >= NEWS_DIGEST_INTERVAL:
                    digest = market_agg.format_telegram_digest(limit=10)
                    send_notify(
                        f"[新聞摘要] {now.strftime('%Y-%m-%d %H:%M')}\n"
                        f"監控：{', '.join(bot.watch_list) or '（待漏斗掃描）'}\n"
                        f"{'─' * 28}\n{digest}"
                    )
                    last_digest_sent = time.time()

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\n[系統] 使用者中止。")
    finally:
        bot.logout()
