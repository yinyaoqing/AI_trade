"""
漏斗式篩選系統（FunnelScanner）

三層篩選將全市場 1,000+ 檔縮減至 3-5 檔精選標的：

  Layer 1  流動性漏斗  — 用 Shioaji AmountRank/VolumeRank scanner 取前 N 名
  Layer 2  量價動能漏斗 — 開盤 15 分鐘成交量、VWAP、漲幅 2%~5%
  Layer 3  AI 情緒排序  — 對通過標的逐一 GPT-4o 新聞情緒評分並排序
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import pandas_ta as ta
import shioaji as sj

from src.ai_trade.news import NewsAggregator

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# 參數
# ---------------------------------------------------------------------------

LIQUIDITY_SCANNER_COUNT = 100   # Layer 1：取成交金額前 N 名
MIN_VOLUME_K            = 3000  # Layer 1：5 日均量下限（張）
MIN_AMOUNT              = 5e8   # Layer 1：5 日均金額下限（元）

OPEN_15MIN_VOL_RATIO    = 0.20  # Layer 2：開盤 15 分鐘量 ≥ 昨日全天 20%
GAIN_MIN                = 0.02  # Layer 2：漲幅下限 2%
GAIN_MAX                = 0.05  # Layer 2：漲幅上限 5%（避免追高）

SENTIMENT_THRESHOLD     = 0.5   # Layer 3：情緒分下限（進入最終清單）
RATE_LIMIT_DELAY        = 0.12  # Shioaji 每 5 秒 50 次 ≈ 每次間隔 0.1s


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    code: str
    score: float            # Layer 3 情緒分
    analysis: str           # GPT-4o 摘要
    vwap: float = 0.0
    current_price: float = 0.0
    gain_pct: float = 0.0
    open15_ratio: float = 0.0   # 開盤 15 分鐘量佔昨日全天比例

    def __str__(self) -> str:
        return (
            f"{self.code}  現價={self.current_price}  漲幅={self.gain_pct:+.2%}"
            f"  VWAP={self.vwap:.2f}  15分量比={self.open15_ratio:.1%}"
            f"  情緒={self.score:+.2f}"
        )


# ---------------------------------------------------------------------------
# 主類別
# ---------------------------------------------------------------------------

class FunnelScanner:
    """
    使用方式：
        scanner = FunnelScanner(api, get_ai_sentiment_fn)
        results = scanner.run()   # 回傳排序後的 ScanResult list
    """

    def __init__(self, api: sj.Shioaji, sentiment_fn):
        self.api = api
        self.sentiment_fn = sentiment_fn   # get_ai_sentiment(news_text) -> (float, str)

    # ------------------------------------------------------------------
    # Layer 1：流動性漏斗
    # ------------------------------------------------------------------
    def layer1_liquidity(self) -> list[str]:
        """
        用 Shioaji AmountRank scanner 取成交金額前 N 名，
        再用 5 日 kbar 驗證均量 / 均額是否達標。
        回傳通過的股票代號清單。
        """
        print(f"[Layer1] 掃描成交金額前 {LIQUIDITY_SCANNER_COUNT} 名...")
        try:
            items = self.api.scanners(
                scanner_type=sj.constant.ScannerType.AmountRank,
                ascending=False,
                count=LIQUIDITY_SCANNER_COUNT,
                timeout=30000,
            )
        except Exception as e:
            print(f"[Layer1] scanner 失敗: {e}")
            return []

        candidates = [item.code for item in items if item.code]
        print(f"[Layer1] scanner 回傳 {len(candidates)} 檔，驗證 5 日均量...")

        passed = []
        today = datetime.now().strftime("%Y-%m-%d")
        five_days_ago = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")

        for code in candidates:
            try:
                contract = self.api.Contracts.Stocks[code]
                kbars = self.api.kbars(contract, start=five_days_ago, end=today)
                df = pd.DataFrame({**kbars.model_dump()})
                if df.empty:
                    continue
                avg_vol    = df["Volume"].tail(5).mean() / 1000  # 張
                avg_amount = (df["Close"] * df["Volume"]).tail(5).mean()

                if avg_vol >= MIN_VOLUME_K or avg_amount >= MIN_AMOUNT:
                    passed.append(code)
                time.sleep(RATE_LIMIT_DELAY)
            except Exception:
                continue

        print(f"[Layer1] 通過流動性過濾：{len(passed)} 檔")
        return passed

    # ------------------------------------------------------------------
    # Layer 2：量價動能漏斗
    # ------------------------------------------------------------------
    def layer2_technical(self, candidates: list[str]) -> list[ScanResult]:
        """
        對 Layer 1 候選標的：
        1. 開盤 15 分鐘成交量 ≥ 昨日全天 20%
        2. 現價 > VWAP
        3. 漲幅介於 GAIN_MIN ~ GAIN_MAX（2%~5%）
        回傳通過的 ScanResult list（不含情緒分）。
        """
        print(f"[Layer2] 量價動能過濾，共 {len(candidates)} 檔...")
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        passed: list[ScanResult] = []

        # 批次取得快照減少 API 呼叫
        batch_size = 50
        for i in range(0, len(candidates), batch_size):
            batch_codes = candidates[i:i + batch_size]
            batch_contracts = [
                self.api.Contracts.Stocks[c]
                for c in batch_codes
                if self.api.Contracts.Stocks.get(c)
            ]
            try:
                snapshots = {snap.code: snap for snap in self.api.snapshots(batch_contracts)}
            except Exception as e:
                print(f"[Layer2] snapshots 失敗: {e}")
                continue
            time.sleep(RATE_LIMIT_DELAY)

            for code in batch_codes:
                snap = snapshots.get(code)
                if snap is None:
                    continue
                try:
                    ref_price = snap.reference   # 昨日收盤（參考價）
                    current   = snap.close
                    if ref_price == 0:
                        continue
                    gain = (current - ref_price) / ref_price

                    # 漲幅過濾
                    if not (GAIN_MIN <= gain <= GAIN_MAX):
                        continue

                    contract = self.api.Contracts.Stocks[code]

                    # 昨日全天成交量
                    kbars_yd = self.api.kbars(contract, start=yesterday, end=yesterday)
                    df_yd = pd.DataFrame({**kbars_yd.model_dump()})
                    yd_total_vol = df_yd["Volume"].sum() if not df_yd.empty else 0

                    # 今日 tick 資料
                    ticks = self.api.ticks(contract, date=today)
                    df_tk = pd.DataFrame({**ticks.model_dump()})
                    if df_tk.empty:
                        continue
                    df_tk["datetime"] = pd.to_datetime(df_tk["ts"])
                    df_tk = df_tk.set_index("datetime").sort_index()

                    # 開盤 15 分鐘成交量
                    open_time = df_tk.index[0].replace(hour=9, minute=0, second=0)
                    cutoff    = open_time + timedelta(minutes=15)
                    vol_15min = df_tk.loc[:cutoff, "Volume"].sum()
                    ratio_15  = vol_15min / yd_total_vol if yd_total_vol > 0 else 0

                    if ratio_15 < OPEN_15MIN_VOL_RATIO:
                        continue

                    # VWAP
                    vwap_series = ta.vwap(
                        df_tk["High"], df_tk["Low"], df_tk["Close"], df_tk["Volume"]
                    )
                    vwap = vwap_series.iloc[-1]
                    if current <= vwap:
                        continue

                    passed.append(ScanResult(
                        code=code,
                        score=0.0,
                        analysis="",
                        vwap=round(vwap, 2),
                        current_price=current,
                        gain_pct=gain,
                        open15_ratio=ratio_15,
                    ))
                    print(f"[Layer2] {code} 通過 ✓  漲幅={gain:+.2%}  15分量比={ratio_15:.1%}")
                    time.sleep(RATE_LIMIT_DELAY)

                except Exception as ex:
                    print(f"[Layer2] {code} 失敗: {ex}")
                    continue

        print(f"[Layer2] 通過量價過濾：{len(passed)} 檔")
        return passed

    # ------------------------------------------------------------------
    # Layer 3：AI 情緒排序
    # ------------------------------------------------------------------
    def layer3_sentiment(self, candidates: list[ScanResult]) -> list[ScanResult]:
        """
        對每個候選標的抓取個股新聞並評分，
        過濾掉情緒分 < SENTIMENT_THRESHOLD，
        依情緒分由高到低排序。
        """
        print(f"[Layer3] AI 情緒評分，共 {len(candidates)} 檔...")
        results: list[ScanResult] = []

        for item in candidates:
            try:
                agg = NewsAggregator(stock_code=item.code)
                news_text = agg.fetch_headlines(limit=8)
                if not news_text:
                    print(f"[Layer3] {item.code} 無新聞，跳過。")
                    continue
                score, analysis = self.sentiment_fn(news_text)
                item.score    = score
                item.analysis = analysis
                print(f"[Layer3] {item.code} 情緒分={score:+.2f}  {analysis[:30]}")
                if score >= SENTIMENT_THRESHOLD:
                    results.append(item)
            except Exception as e:
                print(f"[Layer3] {item.code} 失敗: {e}")

        results.sort(key=lambda x: x.score, reverse=True)
        print(f"[Layer3] 最終精選：{len(results)} 檔")
        return results

    # ------------------------------------------------------------------
    # 執行完整漏斗
    # ------------------------------------------------------------------
    def run(self, max_results: int = 5) -> list[ScanResult]:
        """
        執行三層漏斗，回傳排序後的精選標的（最多 max_results 檔）。
        建議在開盤 15 分鐘後（09:20 以後）呼叫。
        """
        t0 = time.time()
        print("\n" + "=" * 45)
        print(f"[FunnelScanner] 開始掃描 {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 45)

        layer1 = self.layer1_liquidity()
        if not layer1:
            print("[FunnelScanner] Layer1 無結果，終止。")
            return []

        layer2 = self.layer2_technical(layer1)
        if not layer2:
            print("[FunnelScanner] Layer2 無結果，終止。")
            return []

        layer3 = self.layer3_sentiment(layer2)

        elapsed = time.time() - t0
        print(f"[FunnelScanner] 完成，耗時 {elapsed:.1f}s，精選 {len(layer3)} 檔")
        print("=" * 45)
        return layer3[:max_results]
