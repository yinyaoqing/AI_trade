"""
3.2 多策略框架
================
包含：
  - MarketRegime    — 市場狀態（趨勢 / 盤整）
  - detect_regime() — 依 0050 近 20 日實現波動率判斷市場狀態
  - MomentumSignal  — 原有動能策略訊號（VWAP 突破）
  - MeanReversionSignal — 均值回歸訊號（RSI 超賣 + 價格低於 VWAP）
  - StrategyAllocator   — 依市場狀態動態分配兩策略的預算比例
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd
import pandas_ta as ta


# ═══════════════════════════════════════════════════════════════
# 市場狀態
# ═══════════════════════════════════════════════════════════════

class MarketRegime(Enum):
    TRENDING   = "trending"    # 趨勢市：動能策略占優
    RANGING    = "ranging"     # 盤整市：均值回歸占優
    UNKNOWN    = "unknown"


# 0050 近 20 日年化波動率閾值
_VOL_THRESHOLD = 0.03   # 模擬測試用 3%（正式交易改回 0.18）


def detect_regime(api, lookback_days: int = 60) -> tuple[MarketRegime, float]:
    """
    取 0050 近 lookback_days 日 kbars，計算年化實現波動率。
    回傳 (MarketRegime, vol_annualized)
    """
    from datetime import datetime, timedelta, timezone
    TZ_TW = timezone(timedelta(hours=8))
    end   = datetime.now(TZ_TW).strftime("%Y-%m-%d")
    start = (datetime.now(TZ_TW) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    try:
        contract = api.Contracts.Stocks["0050"]
        kbars = api.kbars(contract, start=start, end=end)
        df = pd.DataFrame({**kbars.dict()}).sort_values("ts")
        if len(df) < 10:
            return MarketRegime.UNKNOWN, 0.0

        df["ret"] = df["Close"].pct_change()
        vol_daily = df["ret"].std()
        vol_ann   = vol_daily * (252 ** 0.5)
        regime    = MarketRegime.RANGING if vol_ann > _VOL_THRESHOLD else MarketRegime.TRENDING
        return regime, round(vol_ann, 4)

    except Exception as e:
        print(f"[策略] 市場狀態偵測失敗: {e}")
        return MarketRegime.UNKNOWN, 0.0


# ═══════════════════════════════════════════════════════════════
# 訊號類別
# ═══════════════════════════════════════════════════════════════

@dataclass
class Signal:
    code: str
    action: str          # "BUY" / "SKIP"
    strategy: str        # "momentum" / "mean_reversion"
    current_price: float
    vwap: float
    rsi: float
    reason: str


def momentum_signal(df: pd.DataFrame, code: str) -> Signal:
    """
    動能策略：收盤 > VWAP 且 RSI < 70
    """
    vwap  = ta.vwap(df["High"], df["Low"], df["Close"], df["Volume"]).iloc[-1]
    rsi   = ta.rsi(df["Close"], length=14).iloc[-1]
    price = df["Close"].iloc[-1]

    if pd.isna(vwap) or pd.isna(rsi):
        return Signal(code, "SKIP", "momentum", price, 0, 0, "指標資料不足")

    if price > vwap and rsi < 70:
        return Signal(code, "BUY", "momentum", price, round(float(vwap), 2),
                      round(float(rsi), 1), f"價格({price})突破VWAP({vwap:.2f}), RSI={rsi:.1f}")

    return Signal(code, "SKIP", "momentum", price, round(float(vwap), 2),
                  round(float(rsi), 1),
                  f"未突破VWAP({vwap:.2f})" if price <= vwap else f"RSI超買({rsi:.1f})")


def mean_reversion_signal(df: pd.DataFrame, code: str) -> Signal:
    """
    均值回歸策略：RSI < 30（超賣）且收盤 < VWAP（偏低）
    """
    vwap  = ta.vwap(df["High"], df["Low"], df["Close"], df["Volume"]).iloc[-1]
    rsi   = ta.rsi(df["Close"], length=14).iloc[-1]
    price = df["Close"].iloc[-1]

    if pd.isna(vwap) or pd.isna(rsi):
        return Signal(code, "SKIP", "mean_reversion", price, 0, 0, "指標資料不足")

    if rsi < 30 and price < vwap:
        return Signal(code, "BUY", "mean_reversion", price, round(float(vwap), 2),
                      round(float(rsi), 1),
                      f"超賣RSI={rsi:.1f}<30 且價格({price})<VWAP({vwap:.2f})")

    return Signal(code, "SKIP", "mean_reversion", price, round(float(vwap), 2),
                  round(float(rsi), 1),
                  f"RSI={rsi:.1f}" if rsi >= 30 else f"價格({price})≥VWAP({vwap:.2f})")


# ═══════════════════════════════════════════════════════════════
# 策略分配器
# ═══════════════════════════════════════════════════════════════

@dataclass
class AllocationResult:
    regime: MarketRegime
    vol_ann: float
    momentum_budget_pct: float       # 動能策略資金佔比
    mean_reversion_budget_pct: float  # 均值回歸資金佔比

    def momentum_budget(self, total: float) -> float:
        return total * self.momentum_budget_pct

    def mean_reversion_budget(self, total: float) -> float:
        return total * self.mean_reversion_budget_pct

    def describe(self) -> str:
        return (
            f"市場狀態: {self.regime.value}  "
            f"波動率: {self.vol_ann:.1%}  "
            f"動能佔比: {self.momentum_budget_pct:.0%}  "
            f"均值回歸佔比: {self.mean_reversion_budget_pct:.0%}"
        )


class StrategyAllocator:
    """
    依 0050 波動率動態分配兩策略預算：
      趨勢市 → 動能 80% / 均值回歸 20%
      盤整市 → 動能 30% / 均值回歸 70%
      未知   → 各 50%
    """

    ALLOC_MAP = {
        MarketRegime.TRENDING: (0.80, 0.20),
        MarketRegime.RANGING:  (0.30, 0.70),
        MarketRegime.UNKNOWN:  (0.50, 0.50),
    }

    def __init__(self, api):
        self.api = api
        self._cache: tuple[MarketRegime, float] | None = None
        self._cache_date: str = ""

    def allocate(self) -> AllocationResult:
        """每日快取，避免重複 API 呼叫"""
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        if self._cache_date != today:
            self._cache = detect_regime(self.api)
            self._cache_date = today

        regime, vol = self._cache
        mom_pct, mr_pct = self.ALLOC_MAP[regime]
        return AllocationResult(regime, vol, mom_pct, mr_pct)
