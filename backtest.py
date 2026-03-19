"""
1.4 回測框架 — AI Trade Backtester
====================================
支援兩種資料來源：
  --yf   使用 yfinance（免費，5+ 年，不需登入）
  (預設) 使用 Shioaji kbars（需模擬帳戶登入，約 1 年）

驗證策略：
  - 勝率（Win Rate）
  - 最大回撤（Max Drawdown）
  - 夏普比率（Sharpe Ratio）
  - 獲利因子（Profit Factor）

執行方式：
    # yfinance 5 年回測（不需登入）
    python backtest.py --code 2330 --start 2021-01-01 --yf

    # 多標的比較
    python backtest.py --code 2330,2454,2317 --start 2021-01-01 --yf

    # Shioaji 回測（需模擬帳戶）
    python backtest.py --code 2330 --start 2025-01-01
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

load_dotenv()

TZ_TW = timezone(timedelta(hours=8))

# ── 回測參數（與 bot.py 一致）────────────────────────────
STOP_LOSS_PCT     = 0.025    # 與 bot.py 一致（2.5%）
TRAILING_START    = 0.015
TRAILING_PULLBACK = 0.01
TRAILING_ATR_MULT = 0.6      # 動態回撤：0.6×ATR（與 bot.py 一致）
BREAKEVEN_TRIGGER = 0.02     # 成本保衛：獲利 2% 後止損移至成本
POSITION_SIZE     = 15_000   # 每筆預算（元）
RSI_OVERBOUGHT    = 70
ATR_MAX_PCT       = 0.03     # 放寬至 3%（原 2% 過濾掉太多正常波動）
MA_TREND_PERIOD   = 50       # 趨勢過濾：股價需在 MA50 之上才進場
TRADE_COST_PCT    = 0.004    # 手續費 + 證交稅


# ═══════════════════════════════════════════════════════════════
# 資料結構
# ═══════════════════════════════════════════════════════════════

@dataclass
class Trade:
    code:        str
    entry_date:  str
    exit_date:   str
    entry_price: float
    exit_price:  float
    qty:         int
    reason:      str

    @property
    def gross_pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def cost(self) -> float:
        return (self.entry_price + self.exit_price) * self.qty * TRADE_COST_PCT / 2

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost

    @property
    def ret_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price - TRADE_COST_PCT


@dataclass
class BacktestResult:
    code:         str
    start:        str
    end:          str
    source:       str = "shioaji"
    trades:       list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        if not self.trades:
            return {"error": "無任何成交紀錄"}

        rets   = [t.ret_pct for t in self.trades]
        wins   = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]

        win_rate      = len(wins) / len(self.trades)
        avg_win       = sum(t.net_pnl for t in wins)   / max(len(wins), 1)
        avg_loss      = sum(t.net_pnl for t in losses) / max(len(losses), 1)
        profit_factor = (abs(sum(t.net_pnl for t in wins)) /
                         max(abs(sum(t.net_pnl for t in losses)), 1))

        curve    = pd.Series(self.equity_curve)
        max_dd   = ((curve - curve.cummax()) / curve.cummax()).min()

        s = pd.Series(rets)
        sharpe = (s.mean() / s.std() * (252 ** 0.5)) if s.std() > 0 else 0.0

        return {
            "標的":       self.code,
            "資料來源":   self.source,
            "期間":       f"{self.start} ~ {self.end}",
            "總交易次數": len(self.trades),
            "勝率":       f"{win_rate:.1%}",
            "平均獲利":   f"{avg_win:+.0f} 元",
            "平均虧損":   f"{avg_loss:+.0f} 元",
            "獲利因子":   f"{profit_factor:.2f}",
            "最大回撤":   f"{max_dd:.2%}",
            "夏普比率":   f"{sharpe:.2f}",
            "淨損益合計": f"{sum(t.net_pnl for t in self.trades):+.0f} 元",
        }

    def print_summary(self) -> None:
        s = self.summary()
        if "error" in s:
            print(f"\n[回測] {s['error']}")
            return
        width = 44
        print("\n" + "=" * width)
        print(f"  回測結果：{s['標的']}  {s['期間']}")
        print(f"  資料來源：{s['資料來源']}")
        print("=" * width)
        skip = {"標的", "資料來源", "期間"}
        for k, v in s.items():
            if k in skip:
                continue
            print(f"  {k:<12} {v}")
        print("=" * width)
        if self.trades:
            print(f"\n  各筆交易（共 {len(self.trades)} 筆）：")
            print(f"  {'進場日':<12} {'出場日':<12} {'進價':>8} {'出價':>8} {'淨損益':>10}  原因")
            print("  " + "-" * 64)
            for t in self.trades:
                print(
                    f"  {t.entry_date:<12} {t.exit_date:<12} "
                    f"{t.entry_price:>8.1f} {t.exit_price:>8.1f} "
                    f"{t.net_pnl:>+10.0f}  {t.reason}"
                )


# ═══════════════════════════════════════════════════════════════
# 資料來源：yfinance
# ═══════════════════════════════════════════════════════════════

def _fetch_yf(code: str, start: str, end: str) -> pd.DataFrame:
    """
    從 Yahoo Finance 下載台股日 K。
    上市加 .TW，上櫃加 .TWO；失敗時自動切換。
    回傳欄位：date, Open, High, Low, Close, Volume
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[錯誤] 請先安裝 yfinance：uv add yfinance 或 pip install yfinance")
        return pd.DataFrame()

    # 結束日多抓一天（yfinance end 為排他）
    end_dt = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    for suffix in [".TW", ".TWO"]:
        ticker = f"{code}{suffix}"
        raw = yf.download(ticker, start=start, end=end_dt,
                          auto_adjust=True, progress=False)
        if not raw.empty:
            break
    else:
        print(f"[警告] yfinance 找不到 {code}（已試 .TW / .TWO）")
        return pd.DataFrame()

    # 攤平 MultiIndex 欄位（yfinance >= 0.2 會產生）
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw.reset_index()
    df.columns = [str(c) for c in df.columns]

    # 統一欄位名
    col_map = {
        "Date": "date", "Open": "Open", "High": "High",
        "Low": "Low", "Close": "Close", "Volume": "Volume",
        "Datetime": "date",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _fetch_yf_market(start: str, end: str) -> pd.DataFrame:
    """以 0050.TW 代表大盤"""
    return _fetch_yf("0050", start, end)


# ═══════════════════════════════════════════════════════════════
# 回測引擎
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    使用日 K 棒模擬策略邏輯：
      進場條件：大盤 MA20 向上 + RSI < 70 + 收盤 > VWAP 近似
               + ATR 未過熱（ATR/股價 < 2%）
      出場條件：
        A. ATR 止損（取嚴格者：ATR 止損 vs 固定 2%）
        B. 成本保衛（獲利 2% 後止損移至成本）
        C. 動態移動止盈（0.6×ATR 回落）

    api=None 時使用 yfinance 資料（不需登入 Shioaji）。
    """

    def __init__(self, api=None):
        self.api = api

    # ── Shioaji 資料來源 ─────────────────────────────────────

    def _get_kbars_sj(self, code: str, start: str, end: str) -> pd.DataFrame:
        contract = self.api.Contracts.Stocks[code]
        kbars = self.api.kbars(contract, start=start, end=end)
        df = pd.DataFrame({**kbars.model_dump()})
        df["date"] = pd.to_datetime(df["ts"]).dt.date.astype(str)
        df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        return df

    def _get_market_sj(self, start: str, end: str) -> pd.DataFrame:
        return self._get_kbars_sj("0050", start, end)

    # ── 共用回測核心 ─────────────────────────────────────────

    def run(self, code: str, start: str, end: str,
            params: dict | None = None,
            use_yf: bool = False) -> BacktestResult:
        """
        params 可覆寫回測參數（供 optimize.py grid search 使用）：
          {"stop_loss": 0.02, "trailing_start": 0.015,
           "trailing_pullback": 0.01, "rsi_overbought": 70}
        use_yf=True：改用 yfinance 資料（5+ 年，免登入）
        """
        p_stop    = (params or {}).get("stop_loss",        STOP_LOSS_PCT)
        p_trail_s = (params or {}).get("trailing_start",   TRAILING_START)
        p_trail_p = (params or {}).get("trailing_pullback", TRAILING_PULLBACK)
        p_rsi     = (params or {}).get("rsi_overbought",   RSI_OVERBOUGHT)

        source = "yfinance" if use_yf else "shioaji"
        result = BacktestResult(code=code, start=start, end=end, source=source)

        # ── 下載股票 & 大盤資料 ──────────────────────────────
        if use_yf:
            print(f"[回測] yfinance 下載 {code} ({start} ~ {end})...")
            df = _fetch_yf(code, start, end)
            print(f"[回測] yfinance 下載 0050（大盤）...")
            mkt_raw = _fetch_yf_market(start, end)
        else:
            print(f"[回測] Shioaji 下載 {code} kbars ({start} ~ {end})...")
            df = self._get_kbars_sj(code, start, end)
            print(f"[回測] Shioaji 下載 0050（大盤）kbars...")
            mkt_raw = self._get_market_sj(start, end)

        if len(df) < 30:
            print(f"[回測] 資料不足（{len(df)} 根），請擴大時間範圍。")
            return result
        if mkt_raw.empty:
            print("[回測] 大盤資料下載失敗，終止。")
            return result

        print(f"[回測] {code} 取得 {len(df)} 根日K，開始模擬...")

        # ── 計算技術指標 ─────────────────────────────────────
        df["MA20"]        = df["Close"].rolling(20).mean()
        df["MA50"]        = df["Close"].rolling(MA_TREND_PERIOD).mean()
        df["RSI14"]       = ta.rsi(df["Close"], length=14)
        df["ATR14"]       = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        df["VWAP_approx"] = (df["High"] + df["Low"] + df["Close"]) / 3

        mkt_raw["MA20"] = mkt_raw["Close"].rolling(20).mean()
        mkt = mkt_raw.set_index("date")

        capital = float(POSITION_SIZE)
        result.equity_curve.append(capital)

        position = None   # None 或 dict

        for _, row in df.iterrows():
            date  = row["date"]
            close = row["Close"]
            atr   = row["ATR14"]  if not pd.isna(row.get("ATR14", float("nan")))  else 0.0
            rsi   = row["RSI14"]  if not pd.isna(row.get("RSI14", float("nan")))  else 50.0

            # 大盤過濾
            mkt_row = mkt.loc[date] if date in mkt.index else None
            mkt_ok  = (mkt_row is not None and
                       not pd.isna(mkt_row.get("MA20")) and
                       mkt_row["Close"] > mkt_row["MA20"])

            # ── 出場邏輯 ─────────────────────────────────────
            if position:
                position["max_price"] = max(position["max_price"], close)
                profit_pct   = (close - position["entry_price"]) / position["entry_price"]
                pullback_pct = ((position["max_price"] - close) /
                                position["max_price"])
                reason = None
                exit_p = close  # 預設出場價

                # A. 止損（ATR 與固定 2% 取嚴格者）
                pos_atr   = position["atr"]
                atr_stop  = position["entry_price"] - 1.5 * pos_atr if pos_atr > 0 else 0
                pct_stop  = position["entry_price"] * (1 - p_stop)
                stop_line = max(atr_stop, pct_stop)

                # 跳空：當日 Low 已在止損線以下
                low = row.get("Low", close)
                if low <= stop_line:
                    exit_p = min(close, stop_line)   # 保守估計在止損線出場
                    reason = f"止損({profit_pct:.2%})"

                # B. 成本保衛（調整止損線，不出場）
                elif profit_pct >= BREAKEVEN_TRIGGER and position["stop_price"] < position["entry_price"]:
                    position["stop_price"] = position["entry_price"]

                # C. 動態移動止盈
                elif close >= position["trail_price"]:
                    dyn_pullback = max(
                        (TRAILING_ATR_MULT * pos_atr / position["max_price"]) if pos_atr > 0 else 0,
                        p_trail_p
                    )
                    if pullback_pct >= dyn_pullback:
                        exit_p = close
                        reason = f"移動止盈(高點回吐{pullback_pct:.2%},獲利{profit_pct:.2%})"

                if reason:
                    t = Trade(
                        code=code,
                        entry_date=position["entry_date"],
                        exit_date=date,
                        entry_price=position["entry_price"],
                        exit_price=exit_p,
                        qty=position["qty"],
                        reason=reason,
                    )
                    result.trades.append(t)
                    capital += t.net_pnl
                    result.equity_curve.append(capital)
                    position = None

            # ── 進場邏輯 ─────────────────────────────────────
            if position is None and mkt_ok:
                rsi_ok   = rsi < p_rsi
                vwap_ok  = close > row["VWAP_approx"] * 0.999
                atr_pct  = atr / close if close > 0 else 1.0
                atr_ok   = atr_pct <= ATR_MAX_PCT          # ATR 過熱保護
                ma20_ok  = not pd.isna(row["MA20"])
                # C. MA50 趨勢過濾：股價須在 MA50 之上，避免橫盤/下跌段頻繁進場
                ma50_val = row.get("MA50", float("nan"))
                ma50_ok  = (not pd.isna(ma50_val)) and (close > ma50_val)

                if rsi_ok and vwap_ok and atr_ok and ma20_ok and ma50_ok:
                    qty = max(int(POSITION_SIZE / close), 1)
                    if atr > 0:
                        risk_qty = int((POSITION_SIZE * p_stop) / atr)
                        qty = max(min(risk_qty, qty), 1)

                    atr_stop_p = close - 1.5 * atr if atr > 0 else close * (1 - p_stop)
                    pct_stop_p = close * (1 - p_stop)
                    stop_p     = max(atr_stop_p, pct_stop_p)
                    trail_p    = close + max(1.0 * atr, close * p_trail_s)

                    position = {
                        "entry_date":  date,
                        "entry_price": close,
                        "qty":         qty,
                        "atr":         atr,
                        "stop_price":  stop_p,
                        "trail_price": trail_p,
                        "max_price":   close,
                    }

        # 強制平倉最後一筆
        if position:
            last = df.iloc[-1]
            t = Trade(
                code=code,
                entry_date=position["entry_date"],
                exit_date=last["date"],
                entry_price=position["entry_price"],
                exit_price=last["Close"],
                qty=position["qty"],
                reason="回測結束強制平倉",
            )
            result.trades.append(t)
            capital += t.net_pnl
            result.equity_curve.append(capital)

        return result


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AI Trade 日K 回測框架")
    parser.add_argument("--code",  default="2330",
                        help="股票代碼，逗號分隔多標的：2330,2454,2317")
    parser.add_argument("--start", default="2021-01-01",
                        help="回測起始日 YYYY-MM-DD")
    parser.add_argument("--end",   default=datetime.now(TZ_TW).strftime("%Y-%m-%d"),
                        help="回測結束日（預設今天）")
    parser.add_argument("--yf",    action="store_true",
                        help="使用 yfinance 資料（免費 5+ 年，不需登入）")
    parser.add_argument("--sim",   action="store_true",
                        help="使用 Shioaji 模擬帳戶（需 .env 中設有 API_KEY）")
    args = parser.parse_args()

    use_yf = args.yf
    codes  = [c.strip() for c in args.code.split(",")]

    if use_yf:
        # yfinance 模式：不需登入
        engine = BacktestEngine(api=None)
    else:
        # Shioaji 模式
        import shioaji as sj
        api = sj.Shioaji(simulation=True)
        api.login(
            api_key=os.environ["API_KEY"].strip(),
            secret_key=os.environ["SECRET_KEY"].strip(),
            fetch_contract=False,
        )
        api.fetch_contracts(contract_download=True, contracts_timeout=30_000)
        engine = BacktestEngine(api=api)

    # ── 單標的 ──────────────────────────────────────────────
    if len(codes) == 1 and codes[0] != "all":
        result = engine.run(codes[0], args.start, args.end, use_yf=use_yf)
        result.print_summary()

    # ── 多標的比較 ────────────────────────────────────────────
    else:
        targets = (["2330", "2317", "2454", "3661", "3037"]
                   if codes[0] == "all" else codes)
        rows = []
        for code in targets:
            r = engine.run(code, args.start, args.end, use_yf=use_yf)
            r.print_summary()
            s = r.summary()
            if "error" not in s:
                rows.append(s)

        if len(rows) > 1:
            print("\n" + "=" * 60)
            print("  多標的比較")
            print("=" * 60)
            compare = pd.DataFrame(rows).set_index("標的").drop(columns=["資料來源", "期間"])
            print(compare.to_string())

    if not use_yf:
        api.logout()


if __name__ == "__main__":
    main()
