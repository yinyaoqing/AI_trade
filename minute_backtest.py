"""
分鐘 K 回測框架 — AI Trade MinuteBacktester
=============================================
使用 Shioaji 正式帳戶的分鐘 K 棒，精確模擬盤中交易邏輯：

  進場條件（與 bot.py scan_and_buy 一致）：
    1. 大盤（0050）> MA20
    2. 現價 > 盤中累積 VWAP（每日 09:00 重設）
    3. RSI(14) < 70
    4. VWAP 乖離率 < 3%（未過熱）
    5. 相對成交量 RVOL ≥ 1.5（量能確認）

  出場條件（與 bot.py monitor_exit 一致）：
    A. ATR 止損（進場時設定，entry - 1.5×ATR）
    B. 成本保衛（獲利達 2% → 止損上移至成本）
    C. 動態移動止盈（0.6×ATR 回落）
    D. 時間停損（進場後 30 分鐘仍在成本 ±0.5% → 主動出場）

限制：
  - 需正式帳戶（production）或歷史資料完整的帳戶
  - 模擬帳戶可能缺乏超過 1 年以前的分鐘資料
  - 每日需獨立 API 請求，內建 rate limiting

執行方式：
    python minute_backtest.py --code 2330 --start 2025-01-01 --end 2025-12-31
    python minute_backtest.py --code 2330 --start 2024-01-01 --end 2025-12-31 --plot
"""

import argparse
import os
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv

load_dotenv()

TZ_TW = timezone(timedelta(hours=8))

# ── 回測參數（與 bot.py 一致）────────────────────────────────────
STOP_LOSS_PCT      = 0.02
TRAILING_START     = 0.015
TRAILING_ATR_MULT  = 0.6     # 動態回撤：從最高點回落 0.6×ATR
TRAILING_PULLBACK  = 0.01    # ATR 不足時的保底固定回撤
BREAKEVEN_TRIGGER  = 0.02    # 成本保衛啟動獲利（2%）
# 時間停損：本策略是「波段策略」（設計持倉 1-5 天），不適合強制 30 分鐘出場。
# 改用「每日收盤平倉」作為日內上限，讓 ATR 止損和移動止盈自然觸發。
# 若要測試當沖模式，需改用「分鐘 ATR」而非「日 ATR」。
TIME_STOP_MINUTES  = 0       # 設為 0 = 停用時間停損（波段策略不適合此機制）
TIME_STOP_BAND     = 0.005   # 成本區 ±0.5%（TIME_STOP_MINUTES > 0 時才生效）
MAX_TRADES_PER_DAY = 1       # 每日最多進場次數（波段策略一天只開一次倉即可）
COOLDOWN_MINUTES   = 120     # 出場後冷卻期（波段策略延長為 2 小時）
POSITION_SIZE      = 15_000  # 每筆預算（元）
RISK_PER_TRADE     = 900     # 每筆最大承擔損失（元）
RSI_OVERBOUGHT     = 70
RVOL_MIN           = 1.5     # 相對成交量門檻
VWAP_MAX_GAP       = 0.03    # VWAP 乖離率上限
ATR_STOP_MULT      = 1.5     # 止損 = entry - 1.5 × ATR
ATR_TRAIL_MULT     = 1.0     # 移動止盈啟動 = entry + 1.0 × ATR
ATR_MAX_PCT        = 0.02    # ATR 過熱保護：ATR/股價 > 2% 時，隔夜跳空風險過高，跳過進場
TRADE_COST_PCT     = 0.004   # 手續費 + 證交稅

MARKET_OPEN   = time(9, 0)
MARKET_CLOSE  = time(13, 30)
SCAN_START    = time(9, 10)  # 前 10 分鐘開盤震盪大，略過不掃描


# ═══════════════════════════════════════════════════════════════
# 資料結構
# ═══════════════════════════════════════════════════════════════

@dataclass
class MinuteTrade:
    code:        str
    entry_dt:    str    # YYYY-MM-DD HH:MM
    exit_dt:     str
    entry_price: float
    exit_price:  float
    qty:         int
    reason:      str
    atr:         float = 0.0

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

    @property
    def hold_minutes(self) -> float:
        fmt = "%Y-%m-%d %H:%M"
        try:
            return (datetime.strptime(self.exit_dt, fmt) -
                    datetime.strptime(self.entry_dt, fmt)).total_seconds() / 60
        except Exception:
            return 0.0


@dataclass
class MinuteBacktestResult:
    code:         str
    start:        str
    end:          str
    trades:       list[MinuteTrade] = field(default_factory=list)
    equity_curve: list[float]       = field(default_factory=list)

    def summary(self) -> dict:
        if not self.trades:
            return {"error": "無任何成交紀錄"}

        wins   = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        rets   = [t.ret_pct for t in self.trades]

        win_rate     = len(wins) / len(self.trades)
        avg_win      = sum(t.net_pnl for t in wins)   / max(len(wins), 1)
        avg_loss     = sum(t.net_pnl for t in losses) / max(len(losses), 1)
        profit_factor = (abs(sum(t.net_pnl for t in wins)) /
                         max(abs(sum(t.net_pnl for t in losses)), 1))
        avg_hold     = sum(t.hold_minutes for t in self.trades) / len(self.trades)

        curve  = pd.Series(self.equity_curve)
        max_dd = ((curve - curve.cummax()) / curve.cummax()).min()

        s = pd.Series(rets)
        # 分鐘 K 夏普：年化 = 每日 270 分鐘 × 250 交易日
        sharpe = (s.mean() / s.std() * ((270 * 250) ** 0.5)) if s.std() > 0 else 0.0

        return {
            "標的":         self.code,
            "回測期間":     f"{self.start} ~ {self.end}",
            "總交易次數":   len(self.trades),
            "勝率":         f"{win_rate:.1%}",
            "平均獲利":     f"{avg_win:+.0f} 元",
            "平均虧損":     f"{avg_loss:+.0f} 元",
            "獲利因子":     f"{profit_factor:.2f}",
            "最大回撤":     f"{max_dd:.2%}",
            "夏普比率(年化)": f"{sharpe:.2f}",
            "平均持有分鐘": f"{avg_hold:.0f} min",
            "淨損益合計":   f"{sum(t.net_pnl for t in self.trades):+.0f} 元",
        }

    def print_summary(self) -> None:
        s = self.summary()
        if "error" in s:
            print(f"\n[回測] {s['error']}")
            return
        width = 48
        print("\n" + "=" * width)
        print(f"  分鐘 K 回測結果：{s['標的']}  {s['回測期間']}")
        print("=" * width)
        skip = {"標的", "回測期間"}
        for k, v in s.items():
            if k in skip:
                continue
            print(f"  {k:<16} {v}")
        print("=" * width)
        print(f"\n  最近 20 筆交易（共 {len(self.trades)} 筆）：")
        print(f"  {'進場時間':<17} {'出場時間':<17} {'進價':>8} {'出價':>8} "
              f"{'持有':>6} {'淨損益':>10}  原因")
        print("  " + "-" * 78)
        for t in self.trades[-20:]:
            print(
                f"  {t.entry_dt:<17} {t.exit_dt:<17} "
                f"{t.entry_price:>8.1f} {t.exit_price:>8.1f} "
                f"{t.hold_minutes:>5.0f}m "
                f"{t.net_pnl:>+10.0f}  {t.reason}"
            )

    def plot(self) -> None:
        """繪製資金曲線（需安裝 matplotlib）"""
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8))

            # 資金曲線
            ax1.plot(self.equity_curve, color="steelblue", linewidth=1.5)
            ax1.axhline(self.equity_curve[0], color="gray", linestyle="--", linewidth=0.8)
            ax1.set_title(f"資金曲線：{self.code}  {self.start} ~ {self.end}")
            ax1.set_ylabel("資金（元）")
            ax1.grid(True, alpha=0.3)

            # 損益分布直方圖
            pnls = [t.net_pnl for t in self.trades]
            colors = ["green" if p > 0 else "red" for p in pnls]
            ax2.bar(range(len(pnls)), pnls, color=colors, alpha=0.7)
            ax2.axhline(0, color="black", linewidth=0.8)
            ax2.set_title("各筆損益")
            ax2.set_ylabel("淨損益（元）")
            ax2.grid(True, alpha=0.3)

            plt.tight_layout()
            filename = f"backtest_{self.code}_{self.start[:7]}_{self.end[:7]}.png"
            plt.savefig(filename, dpi=150)
            print(f"[圖表] 已儲存：{filename}")
            plt.show()
        except ImportError:
            print("[圖表] 需安裝 matplotlib：uv add matplotlib")


# ═══════════════════════════════════════════════════════════════
# 分鐘 K 回測引擎
# ═══════════════════════════════════════════════════════════════

class MinuteBacktestEngine:
    """
    使用分鐘 K 棒精確模擬 bot.py 的盤中交易邏輯。

    設計原則：
    - 每天重設盤中 VWAP（09:00 起累積）
    - RSI(14) 用分鐘收盤計算（等同 bot 的盤中 RSI）
    - ATR 使用日 K 計算（與 bot.py get_atr_qty 一致）
    - 每日資料按需抓取，加入速率限制避免觸發 API 限流
    """

    API_DELAY_SEC = 0.2   # 每次 API 呼叫後等待（50 req/5s → 0.1s 間隔，保守用 0.2s）

    def __init__(self, api):
        self.api = api

    # ── 資料抓取 ────────────────────────────────────────────────

    def _fetch_minute_bars(self, code: str, trade_date: str) -> pd.DataFrame:
        """
        抓取單一交易日的分鐘 K 棒。
        date 格式：YYYY-MM-DD

        Shioaji 行為：給定 1 天範圍時回傳分鐘級別 kbars；
        若回傳為空（假日/停市/資料缺失），回傳空 DataFrame。
        """
        contract  = self.api.Contracts.Stocks[code]
        next_date = (datetime.strptime(trade_date, "%Y-%m-%d") +
                     timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            kbars = self.api.kbars(contract, start=trade_date, end=next_date)
            time_mod.sleep(self.API_DELAY_SEC)
            df = pd.DataFrame({**kbars.model_dump()})
        except Exception as e:
            print(f"  [警告] {code} {trade_date} 資料抓取失敗：{e}")
            return pd.DataFrame()

        if df.empty:
            return df

        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df["bar_time"] = df["ts"].dt.time

        # 只保留正式交易時間
        df = df[(df["bar_time"] >= MARKET_OPEN) &
                (df["bar_time"] <= MARKET_CLOSE)].copy()
        df = df.sort_values("ts").reset_index(drop=True)

        # 確認是否為分鐘級別（超過 5 根 bar 視為日內資料）
        if len(df) < 5:
            return pd.DataFrame()

        return df

    def _fetch_daily_bars(self, code: str, start: str, end: str) -> pd.DataFrame:
        """抓日 K 用於計算 ATR 和大盤 MA20"""
        try:
            contract = self.api.Contracts.Stocks[code]
        except (KeyError, TypeError):
            print(f"[錯誤] 合約 {code} 不存在，請確認代碼或是否已下載合約")
            return pd.DataFrame()
        try:
            kbars = self.api.kbars(contract, start=start, end=end)
            time_mod.sleep(self.API_DELAY_SEC)
            raw = kbars.model_dump()
            print(f"[Debug] {code} kbars keys={list(raw.keys())}  ts_count={len(raw.get('ts', []))}")
            df = pd.DataFrame(raw)
            print(f"[Debug] {code} 日K rows={len(df)}  start={start}  end={end}")
        except Exception as e:
            print(f"[錯誤] {code} 日K 抓取失敗：{type(e).__name__}: {e}")
            return pd.DataFrame()

        if df.empty:
            return df

        df["ts"]   = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df["date"] = df["ts"].dt.date.astype(str)
        df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        return df

    # ── 技術指標計算 ─────────────────────────────────────────────

    @staticmethod
    def _calc_vwap(df: pd.DataFrame) -> pd.Series:
        """
        盤中累積 VWAP（每日重設）
        公式：cumsum(close × volume) / cumsum(volume)
        注意：Shioaji 分鐘 K 的 Volume 單位為「股」
        """
        pv = df["Close"] * df["Volume"]
        cum_pv  = pv.cumsum()
        cum_vol = df["Volume"].cumsum().replace(0, 1)
        return cum_pv / cum_vol

    @staticmethod
    def _calc_rvol(df: pd.DataFrame, window: int = 5) -> pd.Series:
        """
        相對成交量（RVOL）：當前分鐘量 / 前 window 分鐘平均量
        window 預設 5（前 5 分鐘平均）
        """
        rolling_avg = df["Volume"].rolling(window, min_periods=1).mean().shift(1)
        return df["Volume"] / rolling_avg.replace(0, 1)

    def _get_daily_atr(self, code: str, as_of_date: str,
                       atr_cache: dict) -> float:
        """
        從日 K 快取中取得 ATR(14)
        atr_cache：{date_str: atr_value} 由 run() 預先建立
        找不到則退回 POSITION_SIZE × STOP_LOSS_PCT 最大損失
        """
        if as_of_date in atr_cache:
            return atr_cache[as_of_date]
        # 找最近有值的日期
        dates = sorted([d for d in atr_cache if d <= as_of_date], reverse=True)
        return atr_cache[dates[0]] if dates else 0.0

    # ── 主回測邏輯 ───────────────────────────────────────────────

    def run(self, code: str, start: str, end: str) -> MinuteBacktestResult:
        result = MinuteBacktestResult(code=code, start=start, end=end)
        capital = float(POSITION_SIZE)
        result.equity_curve.append(capital)

        print(f"\n[回測] {code}  {start} ~ {end}")

        # ── Step 1：抓日 K，建立 ATR 快取 & 大盤 MA20 快取 ──────
        print("[回測] 下載日 K（計算 ATR & 大盤 MA20）...")

        # 往前多抓 60 天讓 ATR(14) 有足夠暖機資料
        pre_start = (datetime.strptime(start, "%Y-%m-%d") -
                     timedelta(days=90)).strftime("%Y-%m-%d")

        daily_df  = self._fetch_daily_bars(code, pre_start, end)
        market_df = self._fetch_daily_bars("0050", pre_start, end)

        if daily_df.empty or market_df.empty:
            print("[回測] 日K 資料不足，終止。")
            return result

        # ATR(14) 快取
        daily_df["ATR14"] = ta.atr(
            daily_df["High"], daily_df["Low"], daily_df["Close"], length=14
        )
        atr_cache: dict[str, float] = {
            row["date"]: row["ATR14"]
            for _, row in daily_df.iterrows()
            if not pd.isna(row["ATR14"])
        }

        # 大盤 MA20 快取
        market_df["MA20"] = market_df["Close"].rolling(20).mean()
        mkt_cache: dict[str, dict] = {
            row["date"]: {"close": row["Close"], "ma20": row["MA20"]}
            for _, row in market_df.iterrows()
        }

        # ── Step 2：取得回測期間所有交易日 ──────────────────────
        trading_days = sorted([
            d for d in daily_df["date"].tolist()
            if start <= d <= end
        ])
        print(f"[回測] 共 {len(trading_days)} 個交易日，逐日掃描分鐘 K...")

        # ── Step 3：持倉狀態（跨日保留）──────────────────────────
        position: dict | None = None   # {entry_dt, entry_price, qty, atr,
                                        #  stop_price, trail_price, max_price,
                                        #  bars_held}
        last_exit_ts: datetime | None = None   # 最後一次出場時間（冷卻期使用）

        # RSI 跨日延續（用 rolling 狀態）
        rsi_series: list[float] = []   # 全期間收盤價（計算 RSI 用）

        # 大盤 5 日均線快取（用當日最新值）
        mkt_close_series: list[float] = []

        for day_idx, trade_date in enumerate(trading_days):
            daily_trade_count = 0   # 當日已進場次數（最多 MAX_TRADES_PER_DAY）
            # 大盤過濾
            mkt_row = mkt_cache.get(trade_date)
            if mkt_row is None or pd.isna(mkt_row.get("ma20")):
                continue
            mkt_close_series.append(mkt_row["close"])
            mkt_ok = mkt_row["close"] > mkt_row["ma20"]

            # 當日 ATR
            atr_val = self._get_daily_atr(code, trade_date, atr_cache)

            print(f"  {trade_date}  大盤{'✓' if mkt_ok else '✗'}  ATR={atr_val:.1f}", end="")

            # 抓分鐘 K
            df = self._fetch_minute_bars(code, trade_date)
            if df.empty:
                # ── 日 K fallback：無分鐘資料時用日 K OHLC 模擬出場 ──
                daily_row = daily_df[daily_df["date"] == trade_date]
                if position and not daily_row.empty:
                    row       = daily_row.iloc[0]
                    day_open  = row["Open"]
                    day_low   = row["Low"]
                    day_high  = row["High"]
                    day_close = row["Close"]
                    bar_dt    = trade_date + " 13:30"
                    profit_pct = (day_close - position["entry_price"]) / position["entry_price"]
                    reason = None
                    # 跳空低開：當日 Open 已在止損價以下
                    if day_open <= position["stop_price"]:
                        reason = f"止損-跳空({day_open:.0f}≤{position['stop_price']:.0f},{profit_pct:.2%})"
                        exit_price = day_open
                    # 日內觸及止損
                    elif day_low <= position["stop_price"]:
                        reason = f"止損({position['stop_price']:.0f},{profit_pct:.2%})"
                        exit_price = position["stop_price"]
                    # 成本保衛更新
                    elif (day_high - position["entry_price"]) / position["entry_price"] >= BREAKEVEN_TRIGGER:
                        if position["stop_price"] < position["entry_price"]:
                            position["stop_price"] = position["entry_price"]
                    if reason:
                        t = MinuteTrade(
                            code=code,
                            entry_dt=position["entry_dt"],
                            exit_dt=bar_dt,
                            entry_price=position["entry_price"],
                            exit_price=exit_price,
                            qty=position["qty"],
                            reason=reason,
                            atr=position["atr"],
                        )
                        result.trades.append(t)
                        capital += t.net_pnl
                        result.equity_curve.append(capital)
                        position = None
                        last_exit_ts = None
                        daily_trade_count += 1
                print(f"  （日K fallback{'，出場: ' + reason if position is None and result.trades else ''}）")
                continue

            print(f"  {len(df)} bars")

            # 盤中指標
            df["VWAP"] = self._calc_vwap(df)
            df["RVOL"] = self._calc_rvol(df)
            # VWAP 穿越：上一根在 VWAP 下方、這一根突破到上方（不含平盤）
            df["prev_close"] = df["Close"].shift(1)
            df["prev_vwap"]  = df["VWAP"].shift(1)
            df["vwap_cross"] = (
                (df["prev_close"] < df["prev_vwap"]) &   # 上一根在 VWAP 下
                (df["Close"] > df["VWAP"])                # 這一根突破到上
            )

            # RSI 跨日延續：把今日所有收盤加入全局序列
            rsi_series.extend(df["Close"].tolist())
            rsi_s = pd.Series(rsi_series)
            rsi_full = ta.rsi(rsi_s, length=14)
            # 對應回今日的 RSI 值
            today_rsi = rsi_full.iloc[-len(df):].reset_index(drop=True)
            df["RSI14"] = today_rsi.values

            # ── 逐分鐘模擬 ──────────────────────────────────────
            for i, bar in df.iterrows():
                bar_dt    = bar["ts"].strftime("%Y-%m-%d %H:%M")
                bar_time  = bar["bar_time"]
                close     = bar["Close"]
                vwap      = bar["VWAP"]
                rvol      = bar["RVOL"]
                rsi       = bar["RSI14"] if not pd.isna(bar["RSI14"]) else 50.0
                vwap_cross = bool(bar["vwap_cross"])

                # ── 出場監控（有持倉時每分鐘執行）──────────────
                if position:
                    position["max_price"] = max(position["max_price"], close)
                    profit_pct  = (close - position["entry_price"]) / position["entry_price"]
                    pullback_pct = ((position["max_price"] - close) /
                                    position["max_price"])
                    position["bars_held"] += 1
                    reason = None

                    # A. 止損
                    if close <= position["stop_price"]:
                        reason = f"止損({profit_pct:.2%})"

                    # B. 成本保衛：獲利達 2% → 止損移至成本
                    elif profit_pct >= BREAKEVEN_TRIGGER and \
                            position["stop_price"] < position["entry_price"]:
                        position["stop_price"] = position["entry_price"]

                    # C. 動態移動止盈
                    elif close >= position["trail_price"]:
                        pos_atr = position["atr"]
                        atr_pullback = TRAILING_ATR_MULT * pos_atr if pos_atr > 0 else 0
                        threshold = max(
                            atr_pullback / position["max_price"],
                            TRAILING_PULLBACK
                        )
                        if pullback_pct >= threshold:
                            reason = (f"移動止盈(高{position['max_price']:.1f},"
                                      f"回吐{pullback_pct:.2%},獲利{profit_pct:.2%})")

                    # D. 時間停損（TIME_STOP_MINUTES=0 時停用）
                    if not reason and TIME_STOP_MINUTES > 0:
                        in_band     = abs(profit_pct) <= TIME_STOP_BAND
                        not_trailed = close < position["trail_price"]
                        if (position["bars_held"] >= TIME_STOP_MINUTES and
                                in_band and not_trailed):
                            reason = f"時間停損({position['bars_held']}分,{profit_pct:+.2%})"

                    # 收盤不強制平倉（波段策略：留到隔日繼續監控）
                    # 若需要日內強制平倉，將以下設定啟用：
                    # if not reason and bar_time >= time(13, 25):
                    #     reason = "收盤平倉"

                    if reason:
                        t = MinuteTrade(
                            code=code,
                            entry_dt=position["entry_dt"],
                            exit_dt=bar_dt,
                            entry_price=position["entry_price"],
                            exit_price=close,
                            qty=position["qty"],
                            reason=reason,
                            atr=position["atr"],
                        )
                        result.trades.append(t)
                        capital += t.net_pnl
                        result.equity_curve.append(capital)
                        position = None
                        last_exit_ts = bar["ts"]      # 記錄出場時間，啟動冷卻期
                        daily_trade_count += 1        # 今日已成交計數

                # ── 進場掃描（無持倉、大盤正常、已過掃描起始時間）──
                in_cooldown = (
                    last_exit_ts is not None and
                    (bar["ts"] - last_exit_ts).total_seconds() / 60 < COOLDOWN_MINUTES
                )
                if (position is None and mkt_ok and
                        bar_time >= SCAN_START and bar_time < time(13, 0) and
                        daily_trade_count < MAX_TRADES_PER_DAY and
                        not in_cooldown):

                    if close <= 0 or vwap <= 0:
                        continue

                    vwap_gap = (close - vwap) / vwap
                    atr_pct  = atr_val / close if close > 0 else 0

                    # 六道進場條件
                    cond_vwap = vwap_cross                    # 本分鐘剛穿越 VWAP（由下往上）
                    cond_gap  = vwap_gap <= VWAP_MAX_GAP      # 未過熱（乖離 < 3%）
                    cond_rsi  = rsi < RSI_OVERBOUGHT          # RSI 未超買
                    cond_rvol = rvol >= RVOL_MIN              # 量能確認
                    cond_atr  = atr_pct <= ATR_MAX_PCT        # ATR 不過高（隔夜跳空風險）

                    if not (cond_vwap and cond_gap and cond_rsi and cond_rvol and cond_atr):
                        continue

                    # 部位計算（ATR 動態）
                    if atr_val > 0:
                        qty = min(
                            int(RISK_PER_TRADE / atr_val),
                            int(POSITION_SIZE / close)
                        )
                    else:
                        qty = max(int(POSITION_SIZE / close), 1)
                    qty = max(qty, 1)

                    # 止損 / 移動止盈啟動價
                    # ATR 止損加上固定 2% 上限：取「兩者中較高（損失較小）的止損價」
                    atr_stop = (close - ATR_STOP_MULT * atr_val if atr_val > 0
                                else close * (1 - STOP_LOSS_PCT))
                    pct_stop = close * (1 - STOP_LOSS_PCT)
                    stop_p   = max(atr_stop, pct_stop)   # 取較高者 = 更嚴格的止損
                    trail_p  = (close + ATR_TRAIL_MULT * atr_val if atr_val > 0
                                else close * (1 + TRAILING_START))

                    position = {
                        "entry_dt":    bar_dt,
                        "entry_price": close,
                        "qty":         qty,
                        "atr":         atr_val,
                        "stop_price":  stop_p,
                        "trail_price": trail_p,
                        "max_price":   close,
                        "bars_held":   0,
                    }

            # 跨日持倉：不強制平倉，留到下一交易日繼續監控

        # 最後一天仍有持倉 → 以最後 bar 收盤平倉
        if position and result.trades or position:
            last_day_df = self._fetch_minute_bars(code, trading_days[-1])
            last_close  = (last_day_df["Close"].iloc[-1]
                           if not last_day_df.empty
                           else position["entry_price"])
            t = MinuteTrade(
                code=code,
                entry_dt=position["entry_dt"],
                exit_dt=trading_days[-1] + " 13:30",
                entry_price=position["entry_price"],
                exit_price=last_close,
                qty=position["qty"],
                reason="回測結束平倉",
                atr=position["atr"],
            )
            result.trades.append(t)
            capital += t.net_pnl
            result.equity_curve.append(capital)

        return result


# ═══════════════════════════════════════════════════════════════
# 多股票比較
# ═══════════════════════════════════════════════════════════════

def run_multi(api, codes: list[str], start: str, end: str,
              plot: bool = False) -> None:
    engine = MinuteBacktestEngine(api)
    rows = []
    for code in codes:
        r = engine.run(code, start, end)
        r.print_summary()
        if plot:
            r.plot()
        s = r.summary()
        if "error" not in s:
            rows.append(s)

    if len(rows) > 1:
        print("\n=== 多標的比較 ===")
        df = pd.DataFrame(rows).set_index("標的")
        print(df.to_string())


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="AI Trade 分鐘 K 回測")
    parser.add_argument("--code",  default="2330",
                        help="股票代碼，逗號分隔多標的：2330,2454")
    parser.add_argument("--start", default="2025-01-01",
                        help="起始日 YYYY-MM-DD")
    parser.add_argument("--end",
                        default=datetime.now(TZ_TW).strftime("%Y-%m-%d"),
                        help="結束日 YYYY-MM-DD（預設今天）")
    parser.add_argument("--sim",   action="store_true",
                        help="使用模擬帳戶（simulation=True）")
    parser.add_argument("--plot",  action="store_true",
                        help="產生資金曲線圖（需安裝 matplotlib）")
    args = parser.parse_args()

    import shioaji as sj
    simulation = args.sim
    if not simulation:
        prod_key    = os.environ.get("PROD_API_KEY", "").strip()
        prod_secret = os.environ.get("PROD_SECRET_KEY", "").strip()
        if not prod_key or not prod_secret:
            print("[警告] 未設定 PROD_API_KEY / PROD_SECRET_KEY，退回使用模擬金鑰（建議加 --sim）")
            api_key    = os.environ["API_KEY"].strip()
            secret_key = os.environ["SECRET_KEY"].strip()
            simulation = True   # 強制轉模擬模式
        else:
            print(f"[正式帳戶] PROD_API_KEY len={len(prod_key)}")
            api_key    = prod_key
            secret_key = prod_secret
    else:
        print("[提示] 模擬帳戶模式（分鐘資料可能僅最近 3 個月）")
        api_key    = os.environ["API_KEY"].strip()
        secret_key = os.environ["SECRET_KEY"].strip()

    api = sj.Shioaji(simulation=simulation)
    api.login(api_key=api_key, secret_key=secret_key, fetch_contract=False)
    api.fetch_contracts(contract_download=True, contracts_timeout=30_000)

    codes = [c.strip() for c in args.code.split(",")]

    if len(codes) == 1:
        engine = MinuteBacktestEngine(api)
        result = engine.run(codes[0], args.start, args.end)
        result.print_summary()
        if args.plot:
            result.plot()
    else:
        run_multi(api, codes, args.start, args.end, plot=args.plot)

    api.logout()


if __name__ == "__main__":
    main()
