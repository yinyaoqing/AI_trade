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

LIQUIDITY_SCANNER_COUNT = 100   # Layer 1：每種 scanner 取前 N 名
MIN_VOLUME_K            = 500   # Layer 1：5 日均量下限（張，放寬讓中小型股可進）
MIN_AMOUNT              = 1e8   # Layer 1：5 日均金額下限（元，放寬至 1 億）
RVOL_MIN_LAYER1         = 1.5   # Layer 1：今日 RVOL 下限（爆量倍數）
RVOL_MAX_LAYER1         = 10.0  # Layer 1：RVOL 上限（避免過度炒作）
LAYER1_TOP_N            = 50    # Layer 1：通過後最終取前 N 名（依綜合分排序）

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
    # Layer 1：多維度熱絡度漏斗
    # ------------------------------------------------------------------
    def layer1_liquidity(self) -> list[str]:
        """
        三種 scanner 聯集 → 流動性過濾 → RVOL 評分排序 → 取前 N 名

        資料來源（聯集）：
          - AmountRank：成交金額排行（傳統流動性）
          - VolumeRank：成交量排行（小型股優勢）
          - ChangePercentRank：漲跌幅排行（題材股捕捉）

        過濾條件：
          - 5 日均量 ≥ MIN_VOLUME_K 張 OR 均金額 ≥ MIN_AMOUNT
          - RVOL（今日量/20日均量）介於 [1.5, 10]

        排序：
          綜合分 = log(成交金額)×0.3 + min(RVOL/5,1)×0.4 + |漲幅|×0.3
        """
        # === 1. 三種 scanner 聯集 ===
        all_codes: set[str] = set()
        scanner_specs = [
            ("AmountRank",        sj.constant.ScannerType.AmountRank),
            ("VolumeRank",        sj.constant.ScannerType.VolumeRank),
            ("ChangePercentRank", sj.constant.ScannerType.ChangePercentRank),
        ]
        for label, st in scanner_specs:
            try:
                items = self.api.scanners(
                    scanner_type=st,
                    ascending=False,
                    count=LIQUIDITY_SCANNER_COUNT,
                    timeout=30000,
                )
                got = [it.code for it in items if it.code]
                all_codes.update(got)
                print(f"[Layer1] {label}: {len(got)} 檔")
                time.sleep(RATE_LIMIT_DELAY)
            except Exception as e:
                print(f"[Layer1] {label} scanner 失敗: {e}")

        print(f"[Layer1] 三 scanner 聯集 → {len(all_codes)} 檔，計算 RVOL 與綜合分...")

        # === 2. 對每檔計算 RVOL + 流動性過濾 + 綜合分 ===
        import math
        today = datetime.now().strftime("%Y-%m-%d")
        start_d = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")

        scored: list[tuple[float, str]] = []  # (score, code)
        for code in all_codes:
            try:
                contract = self.api.Contracts.Stocks.get(code)
                if not contract:
                    continue
                kbars = self.api.kbars(contract, start=start_d, end=today)
                df = pd.DataFrame({**kbars.model_dump()})
                if df.empty or len(df) < 21:
                    continue

                # 流動性下限
                avg_vol    = df["Volume"].tail(5).mean() / 1000
                avg_amount = (df["Close"] * df["Volume"]).tail(5).mean()
                if avg_vol < MIN_VOLUME_K and avg_amount < MIN_AMOUNT:
                    continue

                # RVOL：今日成交量 / 過去 20 日（不含今日）均量
                today_vol = df["Volume"].iloc[-1]
                past20_vol = df["Volume"].iloc[-21:-1].mean()
                if past20_vol <= 0:
                    continue
                rvol = today_vol / past20_vol
                if not (RVOL_MIN_LAYER1 <= rvol <= RVOL_MAX_LAYER1):
                    continue

                # 漲幅
                ref_price = df["Close"].iloc[-2]
                cur_price = df["Close"].iloc[-1]
                gain = abs(cur_price - ref_price) / ref_price if ref_price > 0 else 0

                # 綜合分
                amt_norm  = math.log10(max(avg_amount, 1)) / 11   # 1e11 ≈ 1.0
                rvol_norm = min(rvol / 5.0, 1.0)
                score = amt_norm * 0.3 + rvol_norm * 0.4 + min(gain, 0.1) * 3.0

                scored.append((score, code))
                time.sleep(RATE_LIMIT_DELAY)
            except Exception:
                continue

        # === 3. 取前 N 名 ===
        scored.sort(key=lambda x: x[0], reverse=True)
        passed = [c for _, c in scored[:LAYER1_TOP_N]]
        print(f"[Layer1] 通過熱絡度過濾：{len(passed)} 檔（取前 {LAYER1_TOP_N}）")
        if passed[:10]:
            top10 = [f"{c}({s:.2f})" for s, c in scored[:10]]
            print(f"[Layer1] 前 10 名：{', '.join(top10)}")
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
