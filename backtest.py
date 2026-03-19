"""
1.4 回測框架 — AI Trade Backtester
====================================
使用 Shioaji 歷史 kbars 驗證交易策略，計算：
  - 勝率（Win Rate）
  - 最大回撤（Max Drawdown）
  - 夏普比率（Sharpe Ratio）
  - 獲利因子（Profit Factor）

執行方式：
    python backtest.py --code 2330 --start 2025-01-01 --end 2025-12-31
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
STOP_LOSS_PCT     = 0.02
TRAILING_START    = 0.015
TRAILING_PULLBACK = 0.01
POSITION_SIZE     = 15_000   # 每筆預算（元）
SENTIMENT_THRESHOLD = 0.6    # 情緒門檻（回測固定用 +0.7 模擬利多）
RSI_OVERBOUGHT    = 70
TRADE_COST_PCT    = 0.004    # 手續費 + 證交稅（買進 0.1425% + 賣出 0.1425% + 0.3% ≈ 0.6%，用 0.4% 低估）


# ═══════════════════════════════════════════════════════════════
# 資料結構
# ═══════════════════════════════════════════════════════════════

@dataclass
class Trade:
    code: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: int
    reason: str

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
    code: str
    start: str
    end: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> dict:
        if not self.trades:
            return {"error": "無任何成交紀錄"}

        rets = [t.ret_pct for t in self.trades]
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]

        win_rate = len(wins) / len(self.trades)
        avg_win  = sum(t.net_pnl for t in wins)  / len(wins)  if wins   else 0
        avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0
        profit_factor = (abs(sum(t.net_pnl for t in wins)) /
                         max(abs(sum(t.net_pnl for t in losses)), 1))

        # 最大回撤
        curve = pd.Series(self.equity_curve)
        roll_max = curve.cummax()
        drawdown = (curve - roll_max) / roll_max
        max_dd = drawdown.min()

        # 夏普比率（假設無風險利率 0）
        s = pd.Series(rets)
        sharpe = (s.mean() / s.std() * (252 ** 0.5)) if s.std() > 0 else 0

        return {
            "標的":        self.code,
            "期間":        f"{self.start} ~ {self.end}",
            "總交易次數":  len(self.trades),
            "勝率":        f"{win_rate:.1%}",
            "平均獲利":    f"{avg_win:+.0f} 元",
            "平均虧損":    f"{avg_loss:+.0f} 元",
            "獲利因子":    f"{profit_factor:.2f}",
            "最大回撤":    f"{max_dd:.2%}",
            "夏普比率":    f"{sharpe:.2f}",
            "淨損益合計":  f"{sum(t.net_pnl for t in self.trades):+.0f} 元",
        }

    def print_summary(self) -> None:
        s = self.summary()
        width = 40
        print("=" * width)
        print(f"  回測結果：{s.get('標的', '')}  {s.get('期間', '')}")
        print("=" * width)
        for k, v in s.items():
            if k in ("標的", "期間"):
                continue
            print(f"  {k:<12} {v}")
        print("=" * width)
        if self.trades:
            print("\n  各筆交易：")
            print(f"  {'進場日':<12} {'出場日':<12} {'進價':>8} {'出價':>8} {'淨損益':>10} {'原因'}")
            print("  " + "-" * 60)
            for t in self.trades:
                print(
                    f"  {t.entry_date:<12} {t.exit_date:<12} "
                    f"{t.entry_price:>8.1f} {t.exit_price:>8.1f} "
                    f"{t.net_pnl:>+10.0f}  {t.reason}"
                )


# ═══════════════════════════════════════════════════════════════
# 回測引擎
# ═══════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    使用日 K 棒模擬策略邏輯：
      進場條件：大盤 MA20 向上 + RSI < 70 + 收盤 > VWAP（用日 K 近似）
      出場條件：強制止損 -2% 或移動止盈
    注意：回測跳過 AI 情緒分析（假設市場情緒固定為 +0.7 利多）
    """

    def __init__(self, api):
        self.api = api

    def _get_kbars(self, code: str, start: str, end: str) -> pd.DataFrame:
        contract = self.api.Contracts.Stocks[code]
        kbars = self.api.kbars(contract, start=start, end=end)
        df = pd.DataFrame({**kbars.model_dump()})
        df["date"] = pd.to_datetime(df["ts"]).dt.date.astype(str)
        df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        return df

    def _get_market_df(self, start: str, end: str) -> pd.DataFrame:
        """以 0050 代表大盤"""
        return self._get_kbars("0050", start, end)

    def run(self, code: str, start: str, end: str, params: dict | None = None) -> BacktestResult:
        """
        params 可覆寫回測參數，供 optimize.py 的 grid search 使用：
          params = {"stop_loss": 0.02, "trailing_start": 0.015,
                    "trailing_pullback": 0.01, "rsi_overbought": 70}
        """
        p_stop     = (params or {}).get("stop_loss",        STOP_LOSS_PCT)
        p_trail_s  = (params or {}).get("trailing_start",   TRAILING_START)
        p_trail_p  = (params or {}).get("trailing_pullback", TRAILING_PULLBACK)
        p_rsi      = (params or {}).get("rsi_overbought",   RSI_OVERBOUGHT)
        result = BacktestResult(code=code, start=start, end=end)

        print(f"[回測] 下載 {code} kbars ({start} ~ {end})...")
        df = self._get_kbars(code, start, end)
        if len(df) < 30:
            print(f"[回測] 資料不足（{len(df)} 根），請擴大時間範圍。")
            return result

        print(f"[回測] 下載 0050（大盤）kbars...")
        mkt = self._get_market_df(start, end)
        mkt["MA20"] = mkt["Close"].rolling(20).mean()
        mkt = mkt.set_index("date")

        # 計算技術指標
        df["MA20"]  = df["Close"].rolling(20).mean()
        df["RSI14"] = ta.rsi(df["Close"], length=14)
        df["ATR14"] = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        # 日 K 用 (H+L+C)/3 近似 VWAP
        df["VWAP_approx"] = (df["High"] + df["Low"] + df["Close"]) / 3

        capital = POSITION_SIZE
        result.equity_curve.append(capital)

        position = None  # None 或 dict{entry_price, entry_date, qty, max_price}

        for i, row in df.iterrows():
            date = row["date"]
            close = row["Close"]
            atr   = row["ATR14"] if not pd.isna(row.get("ATR14", float("nan"))) else None
            rsi   = row["RSI14"] if not pd.isna(row.get("RSI14", float("nan"))) else None

            # 大盤過濾
            mkt_row = mkt.get(date) if date in mkt.index else None
            ma20_ok = (mkt_row is not None and
                       not pd.isna(mkt_row.get("MA20")) and
                       mkt_row["Close"] > mkt_row["MA20"])

            # ── 出場邏輯（每天執行）──
            if position:
                position["max_price"] = max(position["max_price"], close)
                profit_pct  = (close - position["entry_price"]) / position["entry_price"]
                pullback_pct = (position["max_price"] - close) / position["max_price"]
                reason = None

                if profit_pct <= -p_stop:
                    reason = f"止損({profit_pct:.2%})"
                elif profit_pct > p_trail_s and pullback_pct >= p_trail_p:
                    reason = f"移動止盈(高點回吐{pullback_pct:.2%},獲利{profit_pct:.2%})"

                if reason:
                    t = Trade(
                        code=code,
                        entry_date=position["entry_date"],
                        exit_date=date,
                        entry_price=position["entry_price"],
                        exit_price=close,
                        qty=position["qty"],
                        reason=reason,
                    )
                    result.trades.append(t)
                    capital += t.net_pnl
                    result.equity_curve.append(capital)
                    position = None

            # ── 進場邏輯（無持倉時執行）──
            if position is None and ma20_ok:
                rsi_ok  = (rsi is None or rsi < p_rsi)
                vwap_ok = close > row["VWAP_approx"] * 0.999

                if rsi_ok and vwap_ok and not pd.isna(row["MA20"]):
                    qty = max(int(POSITION_SIZE / close), 1)
                    if atr and atr > 0:
                        risk_qty = int((POSITION_SIZE * p_stop) / atr)
                        qty = max(min(risk_qty, qty), 1)
                    position = {
                        "entry_date":  date,
                        "entry_price": close,
                        "qty":         qty,
                        "max_price":   close,
                    }

        # 強制平倉最後一筆（以最後收盤價出場）
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
    parser = argparse.ArgumentParser(description="AI Trade 回測框架")
    parser.add_argument("--code",  default="2330",      help="股票代碼（預設 2330）")
    parser.add_argument("--start", default="2025-01-01", help="回測起始日 YYYY-MM-DD")
    parser.add_argument("--end",   default=datetime.now(TZ_TW).strftime("%Y-%m-%d"), help="回測結束日")
    args = parser.parse_args()

    import shioaji as sj
    api = sj.Shioaji(simulation=True)
    api.login(
        api_key=os.environ["API_KEY"].strip(),
        secret_key=os.environ["SECRET_KEY"].strip(),
        fetch_contract=False,
    )
    api.fetch_contracts(contract_download=True, contracts_timeout=30000)

    engine = BacktestEngine(api)
    result = engine.run(args.code, args.start, args.end)
    result.print_summary()

    # 多標的比較
    if args.code == "all":
        targets = ["2330", "2317", "2454", "3661"]
        results = []
        for code in targets:
            r = engine.run(code, args.start, args.end)
            results.append(r.summary())
        print("\n=== 多標的比較 ===")
        compare = pd.DataFrame(results).set_index("標的")
        print(compare.to_string())

    api.logout()


if __name__ == "__main__":
    main()
