# AI Trade — 台股 AI 模擬交易機器人

基於 [Shioaji（永豐金 API）](https://sinotrade.github.io/) 建立的自動交易系統，結合 OpenAI 情緒分析與技術指標，於模擬環境中執行零股交易策略。每日 09:20 自動對全市場執行三層漏斗篩選，動態找出當日精選標的，並全程透過 Telegram 推播交易訊號、AI 分析摘要與新聞。

---

## 目錄

- [系統需求](#系統需求)
- [安裝步驟](#安裝步驟)
- [環境設定](#環境設定)
- [執行方式](#執行方式)
- [交易策略說明](#交易策略說明)
- [漏斗篩選系統](#漏斗篩選系統)
- [多策略框架](#多策略框架)
- [回測引擎](#回測引擎)
- [參數調整](#參數調整)
- [新聞來源說明](#新聞來源說明)
- [Telegram 通知設定](#telegram-通知設定)
- [API 測試狀態查詢](#api-測試狀態查詢)
- [注意事項](#注意事項)
- [專案結構](#專案結構)

---

## 系統需求

| 項目 | 需求 |
|------|------|
| Python | **3.12 以上** |
| 作業系統 | Windows / Linux / macOS |
| 帳戶 | 永豐金證券帳戶（含 API 申請） |
| 外部服務 | OpenAI API（選用）、Telegram Bot（選用） |

---

## 安裝步驟

### 1. 安裝 Python（若尚未安裝）

```powershell
winget install Python.Python.3.12
```

### 2. 安裝套件

```bash
# 使用 uv（推薦）
winget install astral-sh.uv
uv sync

# 或使用 pip
pip install shioaji python-dotenv pandas pandas-ta yfinance openai requests feedparser beautifulsoup4
```

---

## 環境設定

複製範本並填入您的金鑰：

```bash
copy .env.example .env
```

編輯 `.env`：

```env
# 永豐金 API（必要）
API_KEY=您的_API_Key
SECRET_KEY=您的_Secret_Key
CA_CERT_PATH=C:\path\to\your\cert.pfx
CA_PASSWORD=您的憑證密碼

# OpenAI（SENTIMENT_ENABLED=True 時必要，False 時可省略）
OPENAI_API_KEY=sk-...

# Telegram（選用，未填仍可執行）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321

# Proxy（選用，網路封鎖 Telegram 時設定）
# HTTPS_PROXY=http://127.0.0.1:7890
```

> **注意**：`TELEGRAM_BOT_TOKEN` 必須寫在同一行，中間不得有換行符號。

> **取得永豐金 API Key：** 登入永豐金網頁 → API 管理 → 建立 API Key
>
> **憑證下載：** 同上頁面下載 `.pfx` 憑證，記錄憑證密碼

---

## 執行方式

### 模擬交易機器人（主程式）

```bash
uv run python bot.py
```

程式依執行時間自動切換模式：

| 時段 | 行為 |
|------|------|
| 09:05 – 09:19 | 出場監控、等待漏斗掃描 |
| **09:20（每日一次）** | **三層漏斗掃描全市場，動態產生當日監控清單** |
| 09:21 – 13:25 | 每分鐘：AI 情緒分析 → 策略分配 → 進場掃描 → 出場監控 |
| 其他時間（非交易時間） | 每 **30 分鐘**推播今日最新新聞摘要（含連結）至 Telegram |

按 `Ctrl+C` 安全結束，自動登出並推播今日交易總結。

### 回測引擎（不需登入）

```bash
# 單標的 5 年回測（yfinance 免費資料）
uv run python backtest.py --code 2330 --start 2021-01-01 --yf

# 多標的比較
uv run python backtest.py --code 2330,2454,2317 --start 2021-01-01 --yf

# 近 1 年快速驗證
uv run python backtest.py --code 2454 --start 2025-01-01 --yf
```

### API 連線測試

```bash
uv run python main.py
```

執行登入、CA 激活、證券下單測試、期貨下單測試，並查詢帳戶 API 測試通過狀態。

---

## 交易策略說明

### 每日時序

```
程式啟動
  ├─ 抓取今日新聞 → GPT-4o 情緒分析（SENTIMENT_ENABLED=True 時）
  └─ 推播 Telegram：系統設定 + 情緒分析 + 新聞摘要（含連結）

09:05 – 09:19  開盤暖身
  ├─ 出場監控（止損 / 移動止盈）持續運作
  └─ 等待漏斗掃描時間

09:20  ── 漏斗掃描（每日一次）──
  ├─ Layer 1  流動性漏斗
  ├─ Layer 2  量價動能漏斗
  ├─ Layer 3  AI 情緒排序
  └─ 推播精選清單至 Telegram，更新當日監控清單

09:21 – 13:25  每 60 秒主循環
  ├─ [常駐] 出場監控（四種條件，見下方）
  ├─ [A] 大盤過濾（0050 > MA20）
  ├─ [B] 市場情緒分析（GPT-4o）→ 推播 Telegram
  │       SENTIMENT_ENABLED=False 時跳過，直接視為分數通過
  ├─ [C] 市場狀態判斷（StrategyAllocator）
  │       TRENDING  → 動能策略為主 + 均值回歸補充
  │       RANGING   → 均值回歸為主 + 動能策略補充
  └─ [D] 全局候選掃描（scan_candidates）
           ├─ 全部標的評估完畢後排序
           └─ 高分優先依序進場，直到部位滿

非交易時間
  └─ 每 30 分鐘推播新聞摘要（含連結）至 Telegram
```

---

### 進場條件

| 條件 | 說明 |
|------|------|
| 大盤 MA20 | 0050 收盤 > 20 日均線（多頭環境） |
| RSI < 70 | 避免追高；趨勢市中放寬至 RSI < 75（`RSI_DYNAMIC=True`） |
| 現價 > VWAP | 確認當日強勢，且乖離率不超過 3%（`VWAP_MAX_GAP=0.03`） |
| RVOL ≥ 1.5 | 現量為 5 日均量 1.5 倍以上，確認量能爆發（`RVOL_MIN=1.5`） |
| ATR/股價 ≤ 3% | ATR 過熱保護，排除跳空缺口風險 |
| 情緒分 > 0.6 | GPT-4o 市場情緒；`SENTIMENT_ENABLED=False` 時固定通過 |

---

### 出場邏輯

```
每輪監控（不受大盤/情緒過濾影響）
  │
  ├─ A. ATR 止損（嚴格取二者較高止損價）
  │      ATR止損價  = 進場價 - 1.5 × ATR
  │      固定止損價 = 進場價 × (1 - 2%)
  │      取兩者中較高的作為止損線 → 跌破立即賣出
  │
  ├─ B. 成本保衛
  │      獲利達 2% 後，止損線上移至進場成本
  │      → 確保不從獲利轉為虧損
  │
  ├─ C. 動態移動止盈
  │      獲利達 1.5%（TRAILING_START）後啟動
  │      從歷史高點回吐 0.6×ATR（或固定 1%，取較大者）→ 觸發賣出
  │
  └─ D. 時間停損（TIME_STOP_MINUTES > 0 時啟用）
         進場後 N 分鐘仍在成本 ±0.5% 內視為原地踏步 → 出場
         ※ 預設 30 分鐘，波段策略可設為 0（停用）
```

---

### 策略參數說明

**主程式（bot.py）**

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `TOTAL_BUDGET` | 45,000 元 | 總可用資金 |
| `MAX_POSITIONS` | 3 | 最多同時持有部位數 |
| `POSITION_SIZE` | 15,000 元 | 單次進場金額（自動計算） |
| `SENTIMENT_ENABLED` | `False` | 情緒評分開關：`False` → 跳過 AI 新聞分析，直接進入策略掃描（節省 OpenAI 費用） |
| `STOP_LOSS_PCT` | 2% | 固定止損門檻（與 ATR 止損取嚴格者） |
| `TRAILING_START` | 1.5% | 移動止盈啟動獲利點 |
| `TRAILING_PULLBACK` | 1% | 固定回吐觸發賣出（ATR 不足時的保底值） |
| `TRAILING_ATR_MULT` | 0.6 | 動態回撤倍數：從高點回落 0.6×ATR 觸發 |
| `BREAKEVEN_TRIGGER` | 2% | 成本保衛啟動獲利門檻 |
| `TIME_STOP_MINUTES` | 30 | 時間停損（分鐘），0 = 停用 |
| `TIME_STOP_BAND` | 0.5% | 成本區定義（進場價 ±N%） |
| `RVOL_MIN` | 1.5 | 相對成交量下限（現量 / 5日均量） |
| `RSI_DYNAMIC` | `True` | 動態 RSI：趨勢市中放寬超買門檻 |
| `RSI_OVERBOUGHT` | 70 | RSI 超買門檻（一般） |
| `RSI_OVERBOUGHT_LAX` | 75 | RSI 超買門檻（趨勢市放寬版） |
| `VWAP_MAX_GAP` | 3% | VWAP 乖離率上限，超過視為過熱不追 |
| `MARKET_INDEX` | `"0050"` | 大盤指數代碼 |
| `SLIPPAGE_LIMIT` | 0.5% | 最大允許買賣價差 |
| `SCAN_INTERVAL` | 60 秒 | 主循環掃描間隔 |
| `NEWS_DIGEST_INTERVAL` | 1,800 秒 | 非交易時間推播間隔 |
| `FUNNEL_SCAN_HOUR/MINUTE` | 09:20 | 漏斗掃描觸發時間 |
| `FUNNEL_MAX_RESULTS` | 5 | 漏斗精選最大標的數 |
| `PINNED_STOCKS` | 14 檔固定清單 | 不受漏斗掃描影響，每輪必掃的固定監控標的（2330、2317、2454、3661、3443、3017、3324、8996、3037、2383、2368、2059、8210、6805） |
| AI 進場門檻 | 0.6 | GPT-4o 分數須高於此值才進場（`SENTIMENT_ENABLED=False` 時固定通過） |

---

### 情緒分數判讀

| 分數範圍 | 判讀 | 進場行為 |
|----------|------|----------|
| +0.6 ~ +1.0 | 利多 | 執行多標的進場掃描 |
| -0.3 ~ +0.6 | 中性 | 不進場 |
| -1.0 ~ -0.3 | 利空 | 不進場 |

---

### Telegram 通知時機

| 事件 | 通知內容 |
|------|----------|
| 程式啟動 | 系統設定 + **目前持倉狀況** + 啟動情緒分析 + 新聞摘要（含連結） |
| 策略配置**變更**時 | 市場狀態（TRENDING/RANGING）+ 波動率 + 動能/均值回歸比重 |
| 每輪 AI 分析 | 情緒分數 + 利多/中性/利空 + 摘要文字 |
| 買進成交 | 股票代號、成交價、數量、VWAP、情緒分、摘要 |
| 出場觸發 | 出場原因（ATR止損 / 成本保衛 / 移動止盈 / 時間停損）+ 損益 |
| 非交易時間 | 每 30 分鐘推播今日最新新聞（含連結） |
| 程式結束 | 今日交易總結（成交紀錄 + 損益） |

---

## 漏斗篩選系統

監控清單由 `src/ai_trade/scanner.py` 的 `FunnelScanner` 每日 09:20 自動產生，不需手動設定。`PINNED_STOCKS` 中的固定標的會永遠保留在清單中，不受漏斗結果影響。

### 三層篩選流程

```
全市場 1,000+ 檔
      │
      ▼ Layer 1  流動性漏斗
      │  Shioaji AmountRank 取成交金額前 100 名
      │  驗證：5 日均量 > 3,000 張 或 5 日均額 > 5 億元
      │
      ▼ Layer 2  量價動能漏斗
      │  開盤 15 分鐘成交量 ≥ 昨日全天 20%
      │  漲幅介於 2% ~ 5%（避免追高）
      │  現價 > VWAP（價格強於當日均值）
      │
      ▼ Layer 3  AI 情緒排序
         對每支通過標的抓取個股新聞
         GPT-4o 情緒評分 ≥ 0.5 才保留
         依情緒分由高到低排序
         │
         └─ 精選 3~5 檔 → 推播至 Telegram → 更新當日監控清單
```

### 漏斗參數（scanner.py）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `LIQUIDITY_SCANNER_COUNT` | 100 | Layer 1 取前 N 名 |
| `MIN_VOLUME_K` | 3,000 張 | Layer 1 5 日均量下限 |
| `MIN_AMOUNT` | 5 億元 | Layer 1 5 日均額下限 |
| `OPEN_15MIN_VOL_RATIO` | 20% | Layer 2 開盤 15 分鐘量比 |
| `GAIN_MIN` | 2% | Layer 2 漲幅下限 |
| `GAIN_MAX` | 5% | Layer 2 漲幅上限 |
| `SENTIMENT_THRESHOLD` | 0.5 | Layer 3 情緒分下限 |

---

## 多策略框架

`StrategyAllocator` 根據 0050 近 20 日年化波動率自動判斷市場狀態，動態配置策略比重：

| 市場狀態 | 判斷條件 | 動能策略 | 均值回歸 |
|----------|----------|---------|---------|
| TRENDING（趨勢市） | 年化波動率 < 1.5% | 80% | 20% |
| RANGING（盤整市） | 年化波動率 ≥ 1.5% | 30% | 70% |

- **動能策略**：VWAP 突破 + RSI 未超買 + 量能放大（RVOL ≥ 1.5）
- **均值回歸**：RSI < 30 + 現價 < VWAP（超賣後反彈）
- 策略配置改變時才推播 Telegram，避免每分鐘重複通知

---

## 回測引擎

`backtest.py` 支援兩種資料來源，驗證策略在歷史資料上的表現：

### 資料來源

| 模式 | 指令 | 資料年限 | 需登入 |
|------|------|---------|--------|
| **yfinance**（推薦） | `--yf` | 5+ 年 | 否 |
| Shioaji 模擬帳戶 | `--sim`（預設） | ~1 年 | 是 |

### 快速執行

```bash
# 5 年回測，2330
uv run python backtest.py --code 2330 --start 2021-01-01 --yf

# 多標的比較
uv run python backtest.py --code 2330,2454,2317 --start 2021-01-01 --yf
```

### 回測參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `STOP_LOSS_PCT` | 3% | 固定止損（與 ATR 止損取嚴格者） |
| `ATR_MAX_PCT` | 3% | ATR 過熱保護上限（跳過高波動進場） |
| `MA_TREND_PERIOD` | 50 | 趨勢過濾均線：股價需在 MA50 之上才進場 |
| `TRAILING_ATR_MULT` | 0.6 | 動態移動止盈回撤倍數 |
| `BREAKEVEN_TRIGGER` | 2% | 成本保衛啟動門檻 |
| `POSITION_SIZE` | 15,000 元 | 每筆預算 |

### 5 年回測參考結果（yfinance，2021–2026）

| 標的 | 勝率 | 獲利因子 | 最大回撤 | 夏普 | 淨損益 |
|------|------|---------|---------|------|-------|
| 2330 台積電 | 46.3% | 1.33 | -19.5% | 1.93 | +6,366 元 |
| 2454 聯發科 | 53.5% | 1.53 | -22.9% | 2.73 | +6,086 元 |
| 2317 鴻海 | 32.6% | 0.93 | -49.4% | -0.43 | -1,140 元 |

> 2330 / 2454 策略匹配度高；2317 波動性大，建議使用更嚴格的止損設定。

---

## 參數調整

修改 `bot.py` 頂部的常數：

```python
TOTAL_BUDGET      = 45000   # 總預算（元）
MAX_POSITIONS     = 3       # 最多同時持有部位數
SENTIMENT_ENABLED = False   # False → 跳過 AI 分析，節省 OpenAI 費用
STOP_LOSS_PCT     = 0.02    # 固定止損比例（2%）
TRAILING_ATR_MULT = 0.6     # 動態移動止盈回撤倍數
BREAKEVEN_TRIGGER = 0.02    # 成本保衛啟動獲利門檻（2%）
RVOL_MIN          = 1.5     # 相對成交量下限
VWAP_MAX_GAP      = 0.03    # VWAP 乖離率上限（3%）
SCAN_INTERVAL     = 60      # 掃描間隔（秒）
FUNNEL_SCAN_HOUR   = 9      # 漏斗掃描觸發時（09:20）
FUNNEL_SCAN_MINUTE = 20
```

修改 `src/ai_trade/scanner.py` 頂部調整篩選條件：

```python
LIQUIDITY_SCANNER_COUNT = 100   # Layer 1 取前 N 名
MIN_VOLUME_K            = 3000  # 5 日均量下限（張）
MIN_AMOUNT              = 5e8   # 5 日均額下限（元）
OPEN_15MIN_VOL_RATIO    = 0.20  # 開盤 15 分鐘量比
GAIN_MIN                = 0.02  # 漲幅下限
GAIN_MAX                = 0.05  # 漲幅上限
SENTIMENT_THRESHOLD     = 0.5   # Layer 3 情緒分下限
```

---

## 新聞來源說明

新聞由 `src/ai_trade/news.py` 的 `NewsAggregator` 自動聚合，使用以下三個免費來源：

| 來源 | 類型 | 個股過濾 |
|------|------|----------|
| 鉅亨網 | JSON API | 全市場（大盤情緒） |
| Yahoo 奇摩股市 | RSS | 依股票代號 |
| Google News | RSS | 依股票代號 |

- 自動過濾**今日**新聞（台灣時區），非交易日回退至最新 N 則
- 結果依時間排序並去重

---

## Telegram 通知設定

### 建立 Bot

1. 在 Telegram 搜尋 `@BotFather`
2. 傳送 `/newbot`，依指示命名
3. 取得 **Bot Token**（格式：`123456:ABC-DEF...`）

### 取得 Chat ID

1. 對剛建立的 Bot 傳送任意訊息
2. 開啟瀏覽器，前往：
   ```
   https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates
   ```
3. 從回傳 JSON 取得 `message.chat.id`

### 填入 .env

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=987654321
```

### 網路封鎖 Telegram 時

若出現 `Read timed out` 錯誤，在 `.env` 加入 Proxy：

```env
HTTPS_PROXY=http://127.0.0.1:7890
```

| 工具 | 預設 Port |
|------|-----------|
| Clash | 7890 |
| V2Ray | 10809 |
| Shadowsocks | 1080 |

> 未設定 Telegram 時，通知訊息仍會印在 console，不影響交易功能。

---

## API 測試狀態查詢

執行 `uv run python main.py` 後，程式結尾會顯示：

```
=== 查詢 API 測試狀態 ===
帳戶 0610554 (AccountType.Stock): [PASS] 通過
帳戶 00271635 (AccountType.H):    [FAIL] 未通過 (請等待審核約5分鐘)
```

**期貨帳戶未通過時：**

1. 登入永豐金網頁後台
2. 找到期貨帳戶並簽署 API 服務條款
3. 等待約 5 分鐘後重新執行

---

## 部署至 GitHub Actions

### 前置步驟

#### 1. 將憑證轉為 Base64

```bash
# Windows PowerShell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\sinopac.pfx")) | clip

# macOS / Linux
base64 -i sinopac.pfx | pbcopy   # macOS
base64 -w 0 sinopac.pfx          # Linux（複製輸出）
```

#### 2. 在 GitHub 設定 Secrets

前往 **Repository → Settings → Secrets and variables → Actions → New repository secret**，新增以下項目：

| Secret 名稱 | 說明 |
|-------------|------|
| `API_KEY` | 永豐金 API Key |
| `SECRET_KEY` | 永豐金 Secret Key |
| `CA_CERT_B64` | .pfx 憑證的 Base64 字串 |
| `CA_PASSWORD` | 憑證密碼 |
| `OPENAI_API_KEY` | OpenAI API Key（`SENTIMENT_ENABLED=False` 時可省略） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID |
| `HTTPS_PROXY` | Proxy 設定（選用，如 `http://...`） |

#### 3. 啟用 Workflow

將程式碼推送至 GitHub 後，workflow 會在每週一至五 **08:50 台灣時間**自動啟動。

也可手動觸發：**Actions → AI Trade Bot → Run workflow**

---

### 執行時序

```
00:50 UTC（08:50 CST）  GitHub Actions 啟動
  ├─ 安裝套件、還原 CA 憑證
  └─ 執行 bot.py

01:05 UTC（09:05 CST）  台股開盤
  └─ bot.py 內部判斷進入交易時間

01:20 UTC（09:20 CST）  漏斗掃描觸發

05:25 UTC（13:25 CST）  台股收盤
  └─ bot.py 進入非交易時間模式

~05:50 UTC              job timeout（300 分鐘）或 Ctrl+C 結束
  └─ 憑證檔案自動清除
```

---

### 費用說明

| 項目 | 說明 |
|------|------|
| GitHub Actions 免費額度 | Public repo：無限制；Private repo：2,000 分鐘/月 |
| 本機器人用量 | 約 270 分鐘/日 × 20 交易日 = **5,400 分鐘/月** |
| 建議 | 使用 **Public repo**（程式碼不含金鑰，Secrets 獨立管理） |

> 若需使用 Private repo，建議升級至 GitHub Teams（3,000 分鐘/月）或自架 self-hosted runner。

---

## 注意事項

- **`simulation=True` 模式** — 所有委託均為模擬，不會動用真實資金
- **金鑰安全** — `.env` 已加入 `.gitignore`，請勿 commit 憑證或金鑰；若不慎外洩請立即至 BotFather 執行 `/revoke`
- **時區** — 所有交易時間判斷均以台灣時間（UTC+8）為基準，可安全部署至 GitHub Actions（UTC 伺服器）
- **連線限制** — 同一帳號最多 5 條同時連線，避免短時間重複啟動
- **流量限制** — 市場資料每 5 秒最多 50 筆請求；委託每 10 秒最多 250 筆
- **交易時間** — 盤中零股僅限 09:05–13:25，程式已自動判斷
- **OpenAI 費用** — 每分鐘一次 GPT-4o 呼叫，交易時段約 250 次/日，建議設定 API 用量上限；將 `SENTIMENT_ENABLED = False` 可完全停用 AI 分析以節省費用
- **回測限制** — yfinance 提供日 K 資料，無法模擬盤中分鐘級別的 VWAP 訊號；回測結果為策略方向性參考，非精確預測

---

## 專案結構

```
AI_trade/
├── bot.py                    # 主交易機器人
├── backtest.py               # 日K回測引擎（yfinance / Shioaji 雙模式）
├── minute_backtest.py        # 分鐘K回測引擎（Shioaji，需模擬帳戶）
├── main.py                   # 連線測試 & API 測試狀態查詢
├── src/ai_trade/
│   ├── __init__.py
│   ├── client.py             # ShioajiClient 封裝類別
│   ├── news.py               # 新聞聚合器（鉅亨網 / Yahoo / Google News）
│   ├── scanner.py            # 三層漏斗掃描器（FunnelScanner）
│   ├── strategy.py           # 多策略框架（StrategyAllocator）
│   └── chips.py              # 籌碼流向分析
├── .github/workflows/
│   └── trading_bot.yml       # GitHub Actions 自動排程
├── pyproject.toml            # 套件設定（pandas, pandas-ta, yfinance 已內含）
├── .env                      # 金鑰設定（git-ignored）
└── .env.example              # 金鑰範本
```
