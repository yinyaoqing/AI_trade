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
from src.ai_trade.chips import chips_sentiment, chips_summary           # 2.2 籌碼流向
from src.ai_trade.strategy import (                                      # 3.2 多策略
    StrategyAllocator, mean_reversion_signal, MarketRegime, AllocationResult
)
from src.ai_trade.scanner import FunnelScanner                          # 漏斗掃描器

load_dotenv()

# =============================================================================
# 1. 參數設定
# =============================================================================

INITIAL_CAPITAL   = 26000   # 原始投入資金（元），用於計算累計損益
TOTAL_BUDGET      = 26000   # 總預算（元）
MAX_POSITIONS     = 2       # 最多同時持有部位數
POSITION_SIZE     = TOTAL_BUDGET // MAX_POSITIONS  # 初始值，MIN_ORDER_VALUE 定義後由 _calc_position_size() 修正


STOP_LOSS_PCT        = 0.03   # 強制止損：虧損 3%（回測驗證：2% 橫盤假止損過多，3% 最大回撤控制較優，折衷取 2.5%）
TRAILING_START       = 0.015   # 移動止盈啟動點：獲利達 1.5%
TRAILING_PULLBACK    = 0.015    # 移動止盈觸發（ATR 不足時的保底固定回撤）
TRAILING_ATR_MULT    = 0.6     # 動態回撤：從最高點回落 0.6×ATR 時出場（ATR 夠大時優先）
BREAKEVEN_TRIGGER    = 0.02    # 成本保衛：獲利達 2% 時自動將止損上移至成本價
TIME_STOP_MINUTES    = 0      # 時間停損：進場後 X 分鐘仍在成本區則主動出場
TIME_STOP_BAND       = 0.005   # 成本區定義：距進場價 ±0.5% 以內視為「原地踏步」
SLIPPAGE_LIMIT       = 0.01    # 滑點保護：買賣價差 > 1%（零股市場天生價差較大，原 0.5% 過嚴）
MIN_ORDER_VALUE      = 9_000   # 最小下單金額（元）：確保手續費占比 < 0.1%，避免最低手續費侵蝕獲利


def _calc_position_size(total_budget: float) -> int:
    """
    計算單筆可用預算，依實際可承接部位數動態退化：

      affordable       = total_budget // MIN_ORDER_VALUE  （預算最多能開幾個有效部位）
      effective_positions = min(affordable, MAX_POSITIONS)

      effective > 0 → POSITION_SIZE = total_budget // effective_positions
      effective = 0 → 回傳原始值（< MIN_ORDER_VALUE，進場時會被擋下）

    範例（MIN_ORDER_VALUE=9,000）：
      30,000 / MAX_POS=3 → affordable=3, effective=3, size=10,000
      20,000 / MAX_POS=3 → affordable=2, effective=2, size=10,000
      12,000 / MAX_POS=3 → affordable=1, effective=1, size=12,000
       8,000 / MAX_POS=3 → affordable=0, blocked
    """
    affordable          = int(total_budget // MIN_ORDER_VALUE)
    effective_positions = min(affordable, MAX_POSITIONS)
    if effective_positions <= 0:
        return int(total_budget // MAX_POSITIONS)   # < MIN_ORDER_VALUE，進場時自動擋下
    return int(total_budget // effective_positions)


POSITION_SIZE = _calc_position_size(TOTAL_BUDGET)

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

# 漏斗掃描觸發時間（每日一次，開盤 15 分鐘後）
FUNNEL_SCAN_HOUR   = 9    # 09:20 觸發
FUNNEL_SCAN_MINUTE = 20
FUNNEL_MAX_RESULTS = 5    # 漏斗最多取幾檔加入當日監控清單

# 固定監控標的（不受漏斗掃描影響，每輪必掃）
# 回測驗證後精選（2021–2026 yfinance 日K，PF ≥ 1.2、夏普 ≥ 1.0、淨損益為正）
# ★★ = 0050 成分股（零股流動性最佳）；★ = 非 0050 但回測表現優異
# 移除：2317(PF=0.93)、3037(PF=0.53)、2383(PF=0.43)、2368(PF=0.67)、3661/3443(無成交)、6805(資料不足)
PINNED_STOCKS: tuple[str, ...] = (
    # ── 原有回測驗證清單 ────────────────────────────────────────
    "2059",   # ★  川湖  PF=3.21 Sharpe=4.52
    "8210",   # ★  上緯  PF=1.87 Sharpe=3.63
    "3324",   # ★  雙鴻  PF=1.69 Sharpe=3.60
    "2454",   # ★★ 聯發科 PF=1.53 Sharpe=2.73（0050成分）
    "3017",   # ★  奇鋐  PF=1.50 Sharpe=2.32
    "2330",   # ★★ 台積電 PF=1.33 Sharpe=1.93（0050成分）
    "8996",   # ★  高力  PF=1.20 Sharpe=1.14
    # ── 0050 成分股回測驗證通過（PF≥1.1、夏普≥0.6）────────────
    "1590",   # ★★ 亞德客 PF=3.15 Sharpe=6.83（0050成分）
    "2603",   # ★★ 長榮   PF=2.41 Sharpe=5.13（0050成分，航運）
    "2609",   # ★★ 陽明   PF=1.58 Sharpe=2.55（0050成分，航運）
    "2357",   # ★★ 華碩   PF=1.28 Sharpe=1.35（0050成分）
    "2379",   # ★★ 瑞昱   PF=1.13 Sharpe=0.63（0050成分）
)

# 長期持有清單：列在此處的股票不納入止損/止盈監控，由人工決定出場時機
# 適合基本面持股、核心持倉等不希望被短期波動觸發自動賣出的標的
LONG_TERM_HOLD: frozenset[str] = frozenset([
    "0050",   # ★★ 元大台灣50（長期核心持倉，包含台積電等優質藍籌）
    # 範例：
    # "2330",   # 台積電（長期核心持倉）
    # "0050",   # 元大台灣50
])

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

        self._simulation = False   # ← 切換正式交易時改為 False
        self.api = sj.Shioaji(simulation=self._simulation)
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
        self.watch_list: list[str] = list(PINNED_STOCKS)
        self._sentiment_scores: deque[float] = deque(maxlen=SENTIMENT_SMOOTH_N)  # 1.1 情緒平滑
        self.allocator = StrategyAllocator(self.api)                             # 3.2 多策略分配器
        self._last_regime: str = ""              # 策略配置上次推播的 regime，相同則不重複推播
        self._market_trend_up: bool = False      # 大盤趨勢狀態（含遲滯帶）
        self.funnel = FunnelScanner(self.api, get_ai_sentiment)                  # 漏斗掃描器
        self._funnel_done_today: bool = False    # 當日是否已執行漏斗掃描

        # 零股即時報價快取：key=股票代碼, value=(bid, ask)
        # 由 _subscribe_odd_quotes() 持續更新，check_slippage_safe() 優先使用
        self._odd_quotes: dict[str, tuple[float, float]] = {}

        # 委託追蹤：key=stock_code, value={"action","price","qty","amount","trade_obj"}
        # 用於凍結已委託但未成交的資金 / 部位
        self._pending_orders: dict[str, dict] = {}
        # 賣單部位備份：若賣單取消可恢復
        self._pending_sell_positions: dict[str, "Position"] = {}

        # 查詢帳戶餘額，動態決定實際可用預算
        self._init_budget()

        # 訂閱 PINNED_STOCKS BidAsk 報價（供零股滑點判斷使用）
        self._subscribe_odd_quotes()

        # 啟動時同步實際持倉
        self._sync_positions_from_api()

    # ------------------------------------------------------------------
    # 漏斗掃描：每日 09:20 執行一次，結果合併至 watch_list
    # ------------------------------------------------------------------
    def run_funnel_if_needed(self) -> None:
        """
        在主循環中呼叫。條件：
          1. 現在時間 ≥ FUNNEL_SCAN_HOUR:FUNNEL_SCAN_MINUTE
          2. 今日尚未執行過
        執行後將精選標的加入 watch_list（PINNED_STOCKS 永遠保留）。
        跨日（日期變更）自動重置旗標。
        """
        now = now_tw()

        # 跨日重置
        if hasattr(self, "_funnel_last_date") and self._funnel_last_date != now.date():
            self._funnel_done_today = False
            self.watch_list = list(PINNED_STOCKS)   # 重置為基本清單
        self._funnel_last_date = now.date()

        if self._funnel_done_today:
            return
        if now.hour < FUNNEL_SCAN_HOUR or (
            now.hour == FUNNEL_SCAN_HOUR and now.minute < FUNNEL_SCAN_MINUTE
        ):
            remaining = (FUNNEL_SCAN_HOUR * 60 + FUNNEL_SCAN_MINUTE) - (now.hour * 60 + now.minute)
            print(f"[漏斗] 尚未到 {FUNNEL_SCAN_HOUR:02d}:{FUNNEL_SCAN_MINUTE:02d}，剩約 {remaining} 分鐘。")
            return

        print(f"[漏斗] {now.strftime('%H:%M')} 觸發每日漏斗掃描...")
        try:
            results = self.funnel.run(max_results=FUNNEL_MAX_RESULTS)
        except Exception as e:
            print(f"[漏斗] 掃描失敗: {e}")
            self._funnel_done_today = True
            return

        self._funnel_done_today = True

        # 合併：PINNED_STOCKS 優先，漏斗結果補充（不重複）
        pinned = list(PINNED_STOCKS)
        funnel_codes = [r.code for r in results if r.code not in pinned]
        self.watch_list = pinned + funnel_codes

        if funnel_codes:
            codes_str = "、".join(funnel_codes)
            print(f"[漏斗] 精選 {len(funnel_codes)} 檔新增至監控：{codes_str}")
            send_notify(
                f"[漏斗掃描完成] {now.strftime('%H:%M')}\n"
                f"精選 {len(results)} 檔（新增 {len(funnel_codes)} 檔）：\n"
                + "\n".join(f"  {r}" for r in results)
                + f"\n當日監控清單：{self.watch_list}"
            )
        else:
            print("[漏斗] 本日無新增標的（已全在 PINNED_STOCKS 內或未通過篩選）。")

    # ------------------------------------------------------------------
    # 零股報價訂閱：訂閱 PINNED_STOCKS BidAsk，快取最新買賣報價
    # Shioaji 無獨立零股 snapshot API；即時報價需透過 quote.subscribe()
    # ------------------------------------------------------------------
    def _subscribe_odd_quotes(self) -> None:
        """訂閱所有 PINNED_STOCKS 的 BidAsk 即時報價（盤中零股滑點保護用）"""

        @self.api.on_bidask_stk_v1()
        def _on_bidask(_exchange, bidask) -> None:
            """接收 BidAsk 推播並更新快取（bid_price[0] / ask_price[0] 為最優報價）"""
            code = getattr(bidask, "code", None)
            if code is None:
                return
            try:
                bid = float(bidask.bid_price[0]) if bidask.bid_price else 0.0
                ask = float(bidask.ask_price[0]) if bidask.ask_price else 0.0
                if bid > 0 and ask > 0:
                    self._odd_quotes[code] = (bid, ask)
            except Exception:
                pass

        subscribed, failed = 0, 0
        for code in PINNED_STOCKS:
            try:
                contract = self.api.Contracts.Stocks[code]
                self.api.quote.subscribe(
                    contract,
                    quote_type=sj.constant.QuoteType.BidAsk,
                    version=sj.constant.QuoteVersion.v1,
                )
                subscribed += 1
            except Exception as e:
                print(f"[報價訂閱] {code} 失敗: {e}")
                failed += 1
        print(f"[報價訂閱] BidAsk 訂閱完成：成功 {subscribed} 檔，失敗 {failed} 檔")

    def _init_budget(self, notify: bool = False) -> None:
        """
        以「安全可動用現金」作為 TOTAL_BUDGET：
          安全可動用現金 = 交割款餘額 + 未交割淨額（應收 - 應付）
        模擬帳戶不支援 account_balance，回傳 0 時沿用頂部設定值。
        notify=True 時將結果推播至 Telegram（定期更新時使用）。
        """
        global TOTAL_BUDGET, POSITION_SIZE, RISK_PER_TRADE
        try:
            bal = self.api.account_balance()
            acc_balance = float(bal.acc_balance)
            if acc_balance <= 0:
                # 模擬帳戶不支援 account_balance，回傳 0，沿用設定值
                print(f"[預算] 帳戶餘額回傳 0（模擬帳戶限制），沿用設定值 {TOTAL_BUDGET:,} 元")
                return

            # 查詢未交割淨額
            # 規則：
            #   應付（負）：s_date <= today → 已反映在 acc_balance，不重複計算
            #               s_date >  today → 未來待扣款，計入
            #   應收（正）：全部計入（含今日與未來待收）
            net_settlement = 0.0
            payable = receivable = 0.0
            settlement_lines: list[str] = []
            try:
                settlements = self.api.settlements(self.api.stock_account)
                if settlements:
                    today_dt = now_tw().date()
                    for s in settlements:
                        s_date = s.date if hasattr(s.date, "year") else s.date
                        if s.amount < 0:
                            if s_date <= today_dt:
                                note = "（已扣款）"
                            else:
                                payable += s.amount
                                note = ""
                            settlement_lines.append(
                                f"  {s.date} T+{s.T} 應付 {s.amount:+,.0f} 元 {note}"
                            )
                        else:
                            receivable += s.amount
                            settlement_lines.append(
                                f"  {s.date} T+{s.T} 應收 {s.amount:+,.0f} 元"
                            )
                    net_settlement = payable + receivable
                    print(
                        f"[預算] 未交割：應付（未扣）{payable:,.0f} 元  "
                        f"應收 {receivable:+,.0f} 元  淨額 {net_settlement:+,.0f} 元"
                    )
            except Exception as e:
                print(f"[預算] 未交割查詢失敗，以 0 計算：{e}")

            # 扣除委託中但未成交的買單凍結金額
            frozen = self.pending_buy_amount()
            available = acc_balance + net_settlement - frozen

            TOTAL_BUDGET   = available
            POSITION_SIZE  = _calc_position_size(TOTAL_BUDGET)
            RISK_PER_TRADE = TOTAL_BUDGET * STOP_LOSS_PCT
            print(
                f"[預算] 交割款餘額：{acc_balance:,.0f} 元  淨額：{net_settlement:+,.0f} 元"
                + (f"  凍結：{frozen:,.0f} 元" if frozen else "") +
                f"\n  → 安全可動用現金 TOTAL_BUDGET={TOTAL_BUDGET:,.0f}  "
                f"POSITION_SIZE={POSITION_SIZE:,}  "
                f"RISK_PER_TRADE={RISK_PER_TRADE:,.0f}"
            )

            # 安全警告：單筆預算低於最小下單金額時無法進場
            warn_msg = ""
            if POSITION_SIZE < MIN_ORDER_VALUE:
                warn_msg = (
                    f"\n⚠️ 單筆預算 {POSITION_SIZE:,} 元 < 最低下單 {MIN_ORDER_VALUE:,} 元，無法進場。"
                    f"\n建議帳戶至少 {MIN_ORDER_VALUE * MAX_POSITIONS:,} 元。"
                )
                print(f"[預算] {warn_msg}")

            if notify:
                frozen_line = f"委託凍結：{frozen:,.0f} 元\n" if frozen else ""
                msg = (
                    f"[預算更新] {now_tw().strftime('%H:%M:%S')}\n"
                    f"交割款餘額：{acc_balance:,.0f} 元\n"
                    f"未交割明細：\n" + ("\n".join(settlement_lines) if settlement_lines else "  （無待交割）") + "\n"
                    f"應付：{payable:,.0f} 元  應收：{receivable:+,.0f} 元  淨額：{net_settlement:+,.0f} 元\n"
                    + frozen_line +
                    f"─────────────────────\n"
                    f"安全可動用現金：{TOTAL_BUDGET:,.0f} 元\n"
                    f"單筆上限：{POSITION_SIZE:,} 元"
                    + warn_msg
                )
                send_notify(msg)

        except Exception as e:
            print(f"[預算] 查詢餘額失敗，沿用設定值 {TOTAL_BUDGET:,} 元：{e}")

    # ------------------------------------------------------------------
    # 持倉同步：將 API 實際持倉載入 self.positions
    # ------------------------------------------------------------------
    def _sync_positions_from_api(self) -> None:
        """查詢券商實際持倉，載入 self.positions，避免重啟後遺漏持股"""
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
            if not held:
                print("[持倉] 目前無持股")
                return

            # 查詢今日買入紀錄，用於精確判斷 T+1
            today_buys: set[str] = set()
            try:
                try:
                    trades = self.api.list_trades(self.api.stock_account)
                except TypeError:
                    trades = self.api.list_trades()
                today_str = now_tw().strftime("%Y-%m-%d")
                for t in (trades or []):
                    action = str(getattr(getattr(t, "order", None), "action", ""))
                    code   = getattr(getattr(t, "contract", None), "code", "")
                    ts     = str(getattr(getattr(t, "status", None), "order_datetime", ""))
                    if "Buy" in action and ts.startswith(today_str):
                        today_buys.add(code)
            except Exception as e:
                print(f"[持倉] list_trades 查詢失敗（T+1 判斷可能不準）: {e}")

            print(f"[持倉] 查詢到 {len(held)} 筆持股，同步中...")
            if today_buys:
                print(f"[持倉] 今日買入：{today_buys}（T+1 不可當日賣出）")

            for p in held:
                code = p.code
                if code in self.positions:
                    continue  # 已有紀錄，不覆蓋
                avg_price = getattr(p, "price", None) or getattr(p, "average_price", 0)
                qty       = getattr(p, "quantity", 0)
                pos = Position(
                    code=code,
                    entry_price=float(avg_price),
                    qty=int(qty),
                )
                if code in today_buys:
                    # 今日買入：保留 entry_time 為今天（__post_init__ 已設），T+1 保護生效
                    t1_label = "（今日買入，T+1）"
                else:
                    # 非今日買入：設為昨天，允許出場
                    pos.entry_time = now_tw() - timedelta(days=1)
                    t1_label = ""
                self.positions[code] = pos
                last  = float(getattr(p, "last_price", avg_price) or avg_price)
                pnl   = (last - float(avg_price)) * int(qty)
                print(
                    f"  {code}  均價={avg_price}  持股={qty}股  "
                    f"現值≈{last}  損益={pnl:+.0f}元{t1_label}"
                )
        except Exception as e:
            print(f"[持倉] 查詢失敗: {e}")

    def get_positions_summary(self) -> str:
        """回傳持倉摘要字串（供啟動通知與定時推播使用）"""
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
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

    def calc_total_pnl(self) -> str:
        """
        計算累計損益：
          總資產 = 帳戶餘額 + 所有未交割金額 + 持倉市值
          累計損益 = 總資產 - INITIAL_CAPITAL
        """
        try:
            bal = self.api.account_balance()
            acc_balance = float(bal.acc_balance)
        except Exception:
            return "（餘額查詢失敗，無法計算）"

        # 未交割淨額（全部，不分 T+0 / T+1）
        net_settlement = 0.0
        try:
            settlements = self.api.settlements(self.api.stock_account)
            if settlements:
                net_settlement = sum(s.amount for s in settlements)
        except Exception:
            pass

        # 持倉市值
        position_value = 0.0
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
            for p in (held or []):
                qty  = int(getattr(p, "quantity", 0))
                last = float(getattr(p, "last_price", 0) or 0)
                position_value += qty * last
        except Exception:
            pass

        total_asset = acc_balance + net_settlement + position_value
        pnl = total_asset - INITIAL_CAPITAL
        pnl_pct = pnl / INITIAL_CAPITAL * 100 if INITIAL_CAPITAL else 0

        lines = [
            f"[累計損益]",
            f"  原始資金：{INITIAL_CAPITAL:,.0f} 元",
            f"  帳戶餘額：{acc_balance:,.0f} 元",
            f"  未交割淨額：{net_settlement:+,.0f} 元",
            f"  持倉市值：{position_value:,.0f} 元",
            f"  ────────────────────",
            f"  總資產：{total_asset:,.0f} 元",
            f"  累計損益：{pnl:+,.0f} 元（{pnl_pct:+.2f}%）",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 啟動時檢查昨日遺留委託，回報狀態並處理未成交買單
    # ------------------------------------------------------------------
    def startup_order_check(self) -> str:
        """
        啟動時呼叫：
        1. 查詢 list_trades() 取得所有委託
        2. 篩出未完全成交的委託（Submitted / PartFilled）
        3. 對未成交買單：重新評估是否仍符合條件
        4. 回傳摘要字串供 Telegram 推播
        """
        lines: list[str] = ["[委託狀態檢查]"]
        try:
            try:
                trades = self.api.list_trades(self.api.stock_account)
            except TypeError:
                trades = self.api.list_trades()
        except Exception as e:
            return f"[委託狀態檢查] 查詢失敗: {e}"

        if not trades:
            lines.append("  無任何委託紀錄")
            return "\n".join(lines)

        pending_buys: list = []
        pending_sells: list = []
        filled_count = 0

        for t in (trades or []):
            status_obj = getattr(t, "status", None)
            order_obj  = getattr(t, "order", None)
            status_str = str(getattr(status_obj, "status", ""))
            action_str = str(getattr(order_obj, "action", ""))
            code       = getattr(getattr(t, "contract", None), "code", "?")
            order_qty  = int(getattr(order_obj, "quantity", 0))
            deal_qty   = int(getattr(status_obj, "deal_quantity", 0))

            if "Filled" in status_str:
                filled_count += 1
                continue
            if "Cancelled" in status_str:
                continue

            # 仍在委託中（Submitted / PartFilled 等）
            price = float(getattr(order_obj, "price", 0))
            info = {
                "trade": t, "code": code, "action": action_str,
                "price": price, "order_qty": order_qty,
                "deal_qty": deal_qty, "status": status_str,
            }
            if "Buy" in action_str:
                pending_buys.append(info)
                lines.append(
                    f"  ⏳ 買單 {code} {deal_qty}/{order_qty}股 @ {price}  {status_str}"
                )
            elif "Sell" in action_str:
                pending_sells.append(info)
                lines.append(
                    f"  ⏳ 賣單 {code} {deal_qty}/{order_qty}股 @ {price}  {status_str}"
                )

        if not pending_buys and not pending_sells:
            lines.append(f"  所有委託已成交或取消（已成交 {filled_count} 筆）")
            return "\n".join(lines)

        # ── 未成交買單：重新評估 ──
        if pending_buys:
            lines.append("")
            lines.append("[未成交買單重新評估]")
            for info in pending_buys:
                code = info["code"]
                result = self._reevaluate_pending_buy(info)
                lines.append(f"  {code}: {result}")

        return "\n".join(lines)

    def _reevaluate_pending_buy(self, info: dict) -> str:
        """
        重新評估未成交買單：
        - 仍符合條件 → 保留
        - 不符合條件但有更好標的 → 取消並買入新標的
        - 不符合條件且無更好標的 → 取消，釋放資金
        """
        code      = info["code"]
        trade_obj = info["trade"]

        # 1) 重新評估原標的
        still_valid = False
        try:
            c = self._eval_momentum(code, 1.0)
            if c:
                still_valid = True
        except Exception:
            pass

        if still_valid:
            return "仍符合買入條件，保留委託"

        # 2) 原標的不符合 → 取消委託
        try:
            self.api.cancel_order(trade_obj)
            print(f"[委託重評] {code} 已取消")
        except Exception as e:
            return f"取消失敗: {e}，保留委託"

        # 移除 positions 中的記錄（若有）
        if code in self.positions:
            del self.positions[code]
        self._pending_orders.pop(code, None)

        # 3) 掃描是否有更好標的
        best_candidate = None
        for watch_code in self.watch_list:
            if watch_code in self.positions:
                continue
            try:
                c = self._eval_momentum(watch_code, 1.0)
                if c and (best_candidate is None or c.score > best_candidate.score):
                    best_candidate = c
            except Exception:
                continue

        if best_candidate:
            self._execute_buy(best_candidate, 1.0, "（啟動重評替換）")
            return (
                f"已取消，改買 {best_candidate.code} "
                f"（評分 {best_candidate.score:.3f}，價 {best_candidate.price}）"
            )
        else:
            return "已取消，目前無更好標的"

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
        """
        0050 收盤價是否在 20 日均線之上（含遲滯帶防抖動）。
        突破 MA20×1.001 → 轉多；跌破 MA20×0.999 → 轉空。
        介於之間維持上次判斷，避免每分鐘翻轉。
        """
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

            HYST = 0.001  # 遲滯帶 0.1%
            if current > ma20 * (1 + HYST):
                self._market_trend_up = True
            elif current < ma20 * (1 - HYST):
                self._market_trend_up = False
            # else: 維持 self._market_trend_up 上次值

            label = "趨勢向上" if self._market_trend_up else "趨勢偏弱"
            print(f"[大盤] 0050={current:.2f}  MA20={ma20:.2f}  {label}")
            return self._market_trend_up
        except Exception as e:
            print(f"[大盤] 取得失敗: {e}")
            return False

    # ------------------------------------------------------------------
    # 滑點保護
    # ------------------------------------------------------------------
    def check_slippage_safe(self, contract) -> bool:
        """買賣價差是否在允許範圍內。
        優先使用 BidAsk 即時訂閱快取（零股實際報價），
        若快取尚未到位則退回 snapshots()（整股報價，略有偏差）。
        """
        code = contract.code
        source = "即時BidAsk"
        try:
            cached = self._odd_quotes.get(code)
            if cached:
                bid, ask = cached
            else:
                # 快取尚未建立（剛啟動或訂閱失敗），退回 snapshot
                source = "snapshot(整股)"
                snap = self.api.snapshots([contract])[0]
                bid  = snap.buy_price
                ask  = snap.sell_price

            if bid == 0 or ask == 0:
                print(f"[滑點] {code} 無報價（{source}），跳過。")
                return False
            spread = (ask - bid) / bid
            if spread > SLIPPAGE_LIMIT:
                print(f"[滑點] {code} 價差 {spread:.2%} > {SLIPPAGE_LIMIT:.2%}（{source}），暫緩。")
                return False
            print(f"[滑點] {code} 價差 {spread:.2%} 合格（{source}）。")
            return True
        except Exception as e:
            print(f"[滑點] {code} 檢查失敗: {e}")
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
            # 最小下單金額：防止手續費侵蝕（零股最低手續費陷阱）
            if qty * current_price < MIN_ORDER_VALUE:
                print(f"[動能/{stock_code}] 下單金額 {qty * current_price:,.0f} 元 < 最低 {MIN_ORDER_VALUE:,} 元，跳過。")
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
            # 再以 2.5% 為下限，避免 ATR 過小時止損空間不足 0.15% 造成假止損
            atr_stop_p = current_price - 1.5 * atr_val
            pct_stop_p = current_price * (1 - STOP_LOSS_PCT)
            stop_p  = min(max(atr_stop_p, pct_stop_p), current_price * 0.975)
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
            # 最小下單金額：防止手續費侵蝕（零股最低手續費陷阱）
            if qty * sig.current_price < MIN_ORDER_VALUE:
                print(f"[均值回歸/{stock_code}] 下單金額 {qty * sig.current_price:,.0f} 元 < 最低 {MIN_ORDER_VALUE:,} 元，跳過。")
                return None
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
        """對已通過評估的候選標的執行買進下單，並確認成交後更新部位"""
        contract = self.api.Contracts.Stocks[c.code]
        ok = self._place_odd_order(contract, c.price, c.qty, sj.constant.Action.Buy)
        if not ok:
            print(f"[買進] {c.code} 下單被拒，跳過。")
            return

        # 確認成交：以實際成交價格/數量建立部位
        fill = self._confirm_fill(c.code, "Buy")
        actual_price = fill["deal_price"] if fill and fill["deal_qty"] > 0 else c.price
        actual_qty   = fill["deal_qty"]   if fill and fill["deal_qty"] > 0 else c.qty

        # 以實際成交價重算止損/止盈
        stop_p  = actual_price * (1 - STOP_LOSS_PCT)
        trail_p = actual_price * (1 + TRAILING_START)
        if c.atr_val > 0:
            stop_p = max(actual_price - 1.5 * c.atr_val, stop_p)

        pos = Position(
            code=c.code,
            entry_price=actual_price,
            qty=actual_qty,
            atr=c.atr_val,
            stop_price=stop_p,
            trail_price=trail_p,
            entry_score=sentiment_score,
            entry_rsi=c.rsi,
            entry_vwap=c.vwap,
            entry_chips=c.chip_score,
        )
        self.positions[c.code] = pos
        self._trade_log("BUY", pos, actual_price)
        tag = "買進" if c.strategy == "momentum" else "均值回歸買進"
        fill_note = ""
        if fill and fill["deal_qty"] > 0 and abs(actual_price - c.price) > 0.01:
            fill_note = f"\n實際成交：{actual_qty}股 @ {actual_price:.2f}"
        elif fill and fill["deal_qty"] == 0:
            fill_note = "\n⏳ 尚未成交，待後續確認"
        send_notify(
            f"[{tag}] {c.code}\n"
            f"委託: {c.price} x {c.qty}股\n"
            f"VWAP: {c.vwap:.2f}  RSI: {c.rsi:.1f}  法人: {c.chip_score:+.2f}\n"
            f"止損價: {stop_p:.2f}  止盈啟動: {trail_p:.2f}\n"
            f"ATR: {c.atr_val:.2f}  情緒: {sentiment_score:+.2f}  {analysis}"
            + fill_note
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

            # 長期持有清單：跳過所有自動出場監控，由人工決定
            if code in LONG_TERM_HOLD:
                print(f"[監控] {code} 為長期持有，跳過自動出場監控。")
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
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
            hold = next((p for p in held if p.code == code), None)
            qty = hold.quantity if hold else pos.qty
        except Exception:
            qty = pos.qty

        ok = self._place_odd_order(contract, price, qty, sj.constant.Action.Sell)
        if not ok:
            print(f"[警告] {code} 賣單被拒，部位保留，下輪繼續監控。")
            return

        # 確認成交：以實際成交價計算損益
        fill = self._confirm_fill(code, "Sell")
        actual_price = fill["deal_price"] if fill and fill["deal_qty"] > 0 else price
        actual_qty   = fill["deal_qty"]   if fill and fill["deal_qty"] > 0 else qty

        profit_pct = (actual_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
        net_pnl    = (actual_price - pos.entry_price) * actual_qty * (1 - TRADE_COST_PCT)
        self._trade_log("SELL", pos, actual_price, reason=reason)   # 2.3
        # 移除部位，但 _pending_orders 保留 Position 副本
        # 若賣單被取消，_sync_pending_orders 會將部位恢復
        self._pending_sell_positions[code] = pos
        del self.positions[code]
        fill_note = ""
        if fill and fill["deal_qty"] > 0 and abs(actual_price - price) > 0.01:
            fill_note = f"\n實際成交：{actual_qty}股 @ {actual_price:.2f}"
        elif fill and fill["deal_qty"] == 0:
            fill_note = "\n⏳ 尚未成交，待後續確認"
        send_notify(
            f"[賣出] {code}  {reason}\n"
            f"委託: {price} x {qty}股\n"
            f"成本: {pos.entry_price}  獲利: {profit_pct:+.2%}\n"
            f"淨損益: {net_pnl:+.0f} 元"
            + fill_note
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
        if ok:
            code = contract.code
            act_str = str(action).split(".")[-1]  # "Buy" or "Sell"
            self._pending_orders[code] = {
                "action": act_str,
                "price": price,
                "qty": qty,
                "amount": price * qty,
                "trade": trade,
            }
            print(f"[委託追蹤] {act_str} {code} x{qty} @ {price} 加入追蹤")
        return ok

    # ------------------------------------------------------------------
    # 下單後即時確認成交：等待數秒後查 update_status 確認成交結果
    # ------------------------------------------------------------------
    def _confirm_fill(self, code: str, expected_action: str) -> dict | None:
        """
        下單後短暫等待，查詢成交結果。
        回傳 {"deal_qty": int, "deal_price": float, "status": str} 或 None（查詢失敗）。
        """
        pending = self._pending_orders.get(code)
        if not pending:
            return None
        trade_obj = pending["trade"]

        # 等待交易所回報（零股通常 1~3 秒內回報）
        time.sleep(3)

        try:
            self.api.update_status(self.api.stock_account)
        except Exception:
            pass

        try:
            status_obj = getattr(trade_obj, "status", None)
            status_str = str(getattr(status_obj, "status", ""))
            deal_qty   = int(getattr(status_obj, "deal_quantity", 0))
            # 計算成交均價（從 deals 列表）
            deals = getattr(trade_obj, "deals", []) or []
            if deals:
                total_amt = sum(float(getattr(d, "price", 0)) * int(getattr(d, "quantity", 0)) for d in deals)
                total_qty = sum(int(getattr(d, "quantity", 0)) for d in deals)
                deal_price = total_amt / total_qty if total_qty > 0 else pending["price"]
            else:
                deal_price = pending["price"]

            result = {"deal_qty": deal_qty, "deal_price": deal_price, "status": status_str}
            print(
                f"[成交確認] {expected_action} {code}  "
                f"狀態={status_str}  成交={deal_qty}/{pending['qty']}股"
                + (f"  均價={deal_price:.2f}" if deal_qty > 0 else "")
            )
            return result
        except Exception as e:
            print(f"[成交確認] {code} 查詢失敗: {e}")
            return None

    # ------------------------------------------------------------------
    # 委託成交確認：同步 list_trades() 更新委託狀態
    # ------------------------------------------------------------------
    def _sync_pending_orders(self) -> None:
        """
        檢查 _pending_orders 內的委託是否已成交。
        - 買單全部成交 → 移除追蹤（部位已在 self.positions）
        - 買單未成交   → 保留追蹤，凍結資金
        - 賣單全部成交 → 移除追蹤，確認部位已清除
        - 賣單未成交   → 保留追蹤，部位恢復至 self.positions（防止空出 slot 又買入）
        """
        if not self._pending_orders:
            return
        try:
            try:
                trades = self.api.list_trades(self.api.stock_account)
            except TypeError:
                trades = self.api.list_trades()
        except Exception as e:
            print(f"[委託追蹤] list_trades 失敗: {e}")
            return

        # 建立 ordno → trade 索引（用於比對成交狀態）
        trade_map: dict[str, object] = {}
        for t in (trades or []):
            ordno = getattr(getattr(t, "status", None), "ordno", None) or \
                    getattr(getattr(t, "order", None), "ordno", None)
            if ordno:
                trade_map[ordno] = t

        resolved: list[str] = []
        for code, info in self._pending_orders.items():
            pending_trade = info.get("trade")
            ordno = getattr(getattr(pending_trade, "order", None), "ordno", None) or \
                    getattr(getattr(pending_trade, "status", None), "ordno", None)
            if not ordno:
                resolved.append(code)
                continue

            live = trade_map.get(ordno)
            if live is None:
                continue

            status_obj = getattr(live, "status", None)
            status_str = str(getattr(status_obj, "status", ""))
            order_qty = info["qty"]
            deal_qty  = int(getattr(status_obj, "deal_quantity", 0))

            if "Filled" in status_str or deal_qty >= order_qty:
                # 全部成交
                print(f"[委託追蹤] {info['action']} {code} 已全部成交（{deal_qty}/{order_qty}）")
                self._pending_sell_positions.pop(code, None)  # 清除賣單備份
                resolved.append(code)
            elif "Cancelled" in status_str:
                print(f"[委託追蹤] {info['action']} {code} 已取消（成交 {deal_qty}/{order_qty}）")
                if info["action"] == "Buy" and code in self.positions:
                    if deal_qty == 0:
                        del self.positions[code]
                        print(f"[委託追蹤] {code} 買單取消且未成交，移除部位")
                elif info["action"] == "Sell" and code not in self.positions:
                    # 賣單取消 → 從備份恢復部位
                    backup = self._pending_sell_positions.pop(code, None)
                    if backup:
                        self.positions[code] = backup
                        print(f"[委託追蹤] {code} 賣單取消，部位已恢復，下輪繼續監控")
                        send_notify(f"[委託追蹤] ⚠️ {code} 賣單取消（{deal_qty}/{order_qty}），部位已恢復監控")
                resolved.append(code)
            else:
                # 仍在委託中
                if info["action"] == "Sell" and code not in self.positions:
                    # 賣單未成交但部位已被移除 → 發出警告
                    print(f"[委託追蹤] ⚠️ {code} 賣單未成交（{deal_qty}/{order_qty}），部位已移除")

        for code in resolved:
            del self._pending_orders[code]

    def pending_buy_amount(self) -> float:
        """回傳所有委託中買單的凍結金額"""
        return sum(
            info["amount"]
            for info in self._pending_orders.values()
            if info["action"] == "Buy"
        )

    # ------------------------------------------------------------------
    # 部位校驗：比對 self.positions 與 API 實際持倉
    # ------------------------------------------------------------------
    def _verify_positions(self) -> None:
        """
        買入前呼叫。比對 self.positions 與 list_positions：
        - API 有但 bot 沒有 → 補入（可能是手動買入或上次未紀錄）
        - bot 有但 API 沒有 → 移除（可能已成交賣出但 bot 未同步）
        - 數量不一致 → 以 API 為準更新
        """
        try:
            held = self.api.list_positions(self.api.stock_account, unit=sj.constant.Unit.Share)
        except Exception as e:
            print(f"[部位校驗] 查詢失敗: {e}")
            return

        api_codes = {}
        for p in (held or []):
            code = p.code
            qty  = int(getattr(p, "quantity", 0))
            if qty > 0:
                api_codes[code] = {
                    "qty": qty,
                    "price": float(getattr(p, "price", None) or getattr(p, "average_price", 0)),
                    "last": float(getattr(p, "last_price", 0) or 0),
                }

        changed = False

        # API 有但 bot 沒有 → 補入
        for code, info in api_codes.items():
            if code not in self.positions:
                # 排除正在賣出中的（pending sell）
                if code in self._pending_orders and self._pending_orders[code]["action"] == "Sell":
                    continue
                pos = Position(code=code, entry_price=info["price"], qty=info["qty"])
                pos.entry_time = now_tw() - timedelta(days=1)  # 預設為非今日
                self.positions[code] = pos
                print(f"[部位校驗] 補入 {code} {info['qty']}股 均價={info['price']}")
                changed = True

        # bot 有但 API 沒有 → 移除
        stale = [code for code in self.positions if code not in api_codes]
        for code in stale:
            # 排除正在買入中的（pending buy）
            if code in self._pending_orders and self._pending_orders[code]["action"] == "Buy":
                continue
            print(f"[部位校驗] 移除 {code}（API 已無持倉）")
            del self.positions[code]
            changed = True

        # 數量不一致 → 以 API 為準
        for code in list(self.positions):
            if code in api_codes and self.positions[code].qty != api_codes[code]["qty"]:
                old_qty = self.positions[code].qty
                self.positions[code].qty = api_codes[code]["qty"]
                print(f"[部位校驗] {code} 數量更新 {old_qty} → {api_codes[code]['qty']}股")
                changed = True

        if not changed:
            print("[部位校驗] 部位一致")

    # ------------------------------------------------------------------
    # 定期狀態報告：委託 + 部位狀況推播至 Telegram
    # ------------------------------------------------------------------
    def periodic_status_report(self) -> None:
        """每 30 分鐘呼叫一次，推播委託與部位狀態"""
        lines = [f"[定期狀態報告] {now_tw().strftime('%H:%M:%S')}"]

        # 1) 同步委託狀態
        self._sync_pending_orders()

        # 2) 未完成委託
        if self._pending_orders:
            lines.append(f"\n[未完成委託] {len(self._pending_orders)} 筆")
            for code, info in self._pending_orders.items():
                lines.append(
                    f"  {info['action']} {code}  "
                    f"{info['qty']}股 @ {info['price']}  "
                    f"金額 {info['amount']:,.0f} 元"
                )
        else:
            lines.append("\n[未完成委託] 無")

        # 3) 校驗部位
        self._verify_positions()

        # 4) 目前持倉
        lines.append(f"\n{self.get_positions_summary()}")

        report = "\n".join(lines)
        print(report)
        send_notify(report)

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
    _mode = "simulation=True（模擬）" if bot._simulation else "simulation=False（正式交易⚠️）"
    print(f"AI 交易系統啟動  模式：{_mode}")
    print(f"監控清單：{list(PINNED_STOCKS)}")
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

    # ── 累計損益 ──
    pnl_summary = bot.calc_total_pnl()
    print(pnl_summary)

    # ── 啟動委託檢查：偵測遺留未成交單，重新評估買單 ──
    print("[啟動] 檢查遺留委託...")
    order_check_result = bot.startup_order_check()
    print(order_check_result)

    send_notify(
        f"[AI Trade 啟動]\n"
        f"模式：{'simulation=True（模擬）' if bot._simulation else 'simulation=False（正式交易⚠️）'}\n"
        f"部位上限：{MAX_POSITIONS} 檔 | 單筆：{POSITION_SIZE:,} 元\n"
        f"止損 {STOP_LOSS_PCT:.0%} | 移動止盈 {TRAILING_START:.1%}→{TRAILING_PULLBACK:.1%} | 滑點 {SLIPPAGE_LIMIT:.1%}\n"
        f"監控清單：{list(PINNED_STOCKS)}\n"
        f"啟動時間：{now_tw().strftime('%Y-%m-%d %H:%M:%S')} CST\n"
        f"\n[目前持倉]\n{positions_summary}\n"
        f"\n{pnl_summary}\n"
        f"\n{order_check_result}\n"
        f"\n[啟動情緒分析]\n"
        f"分數：{startup_score:+.2f}  {sentiment_label(startup_score)}\n"
        f"摘要：{startup_analysis}\n"
        f"\n[最新新聞]\n{startup_digest}"
    )

    last_digest_sent: float = time.time()
    last_budget_refresh: float = time.time()
    last_status_report: float = time.time()
    BUDGET_REFRESH_INTERVAL = 600    # 每 10 分鐘重查 settlements() 更新預算
    STATUS_REPORT_INTERVAL  = 1800   # 每 30 分鐘推播委託 + 部位狀態

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

                # 定期重查 settlements()，確保賣出應收款及時反映至 TOTAL_BUDGET
                if time.time() - last_budget_refresh >= BUDGET_REFRESH_INTERVAL:
                    bot._init_budget(notify=True)
                    last_budget_refresh = time.time()

                # 委託成交確認（每輪必跑，更新凍結資金 / 恢復取消部位）
                bot._sync_pending_orders()

                # 定期狀態報告（每 30 分鐘推播委託 + 部位至 Telegram）
                if time.time() - last_status_report >= STATUS_REPORT_INTERVAL:
                    bot.periodic_status_report()
                    last_status_report = time.time()

                # 出場監控（每輪必跑，不受任何過濾影響）
                bot.monitor_exit()

                # 漏斗掃描（09:20 每日一次，更新當日監控清單）
                bot.run_funnel_if_needed()

                # 滿倉檢查：持倉 + 待確認賣單（slot 尚未釋放）達 MAX_POSITIONS 時跳過
                pending_sells = len([v for v in bot._pending_orders.values() if v["action"] == "Sell"])
                active_slots  = len(bot.positions) + pending_sells
                if active_slots >= MAX_POSITIONS:
                    print(f"[策略] 已佔 {active_slots} 檔（持倉 {len(bot.positions)} + 待賣 {pending_sells}，上限 {MAX_POSITIONS}），跳過進場掃描。")
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

                    # 買入前校驗：確認委託已成交、部位與 API 一致
                    bot._sync_pending_orders()
                    bot._verify_positions()

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
                        f"監控：{', '.join(bot.watch_list)}\n"
                        f"{'─' * 28}\n{digest}"
                    )
                    last_digest_sent = time.time()

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\n[系統] 使用者中止。")
    finally:
        bot.logout()
