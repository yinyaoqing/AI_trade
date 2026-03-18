"""
2.2 籌碼流向 — 三大法人買賣超
================================
來源：證交所 TWSE Open Data（免費，無需帳號）

提供：
  - get_institutional_flow(date)   → 全市場三大法人買賣超 DataFrame
  - get_stock_chips(code, date)    → 單一個股外資/投信/自營商淨買超（股）
  - chips_sentiment(code, date)    → 轉換為 -1.0 ~ 1.0 情緒分數（供 AI 參考）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests

TZ_TW    = timezone(timedelta(hours=8))
TIMEOUT  = 10
HEADERS  = {"User-Agent": "Mozilla/5.0 (AI-Trade-Bot/1.0)"}

# 淨買超超過此門檻視為強勢（股）
CHIPS_STRONG_BUY  =  1_000_000   # 淨買超 100 萬股以上 → 極度利多
CHIPS_STRONG_SELL = -1_000_000   # 淨賣超 100 萬股以上 → 極度利空


def _today_tw() -> str:
    return datetime.now(TZ_TW).strftime("%Y%m%d")


@lru_cache(maxsize=5)
def get_institutional_flow(date: str = "") -> dict[str, dict]:
    """
    取得指定日期三大法人買賣超，回傳 {股票代碼: {foreign, trust, dealer, total}}
    date 格式：YYYYMMDD，空字串表示今日
    """
    date = date or _today_tw()
    url  = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALL&response=json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") != "OK":
            return {}

        result: dict[str, dict] = {}
        for row in data.get("data", []):
            # 欄位：[代號, 名稱, 外資買, 外資賣, 外資淨, 投信買, 投信賣, 投信淨, 自營商(含避險)淨, 三大法人合計]
            if len(row) < 10:
                continue
            code = row[0].strip()

            def parse(val: str) -> int:
                return int(val.replace(",", "").replace("+", "") or 0)

            try:
                result[code] = {
                    "foreign": parse(row[4]),   # 外資淨買超（股）
                    "trust":   parse(row[7]),   # 投信淨買超（股）
                    "dealer":  parse(row[8]),   # 自營商淨買超（股）
                    "total":   parse(row[9]),   # 三大法人合計（股）
                }
            except (ValueError, IndexError):
                continue
        return result

    except Exception as e:
        print(f"[籌碼] TWSE T86 取得失敗: {e}")
        return {}


def get_stock_chips(code: str, date: str = "") -> dict:
    """回傳單一個股的三大法人資料；若無資料回傳空 dict"""
    flow = get_institutional_flow(date or _today_tw())
    return flow.get(code, {})


def chips_sentiment(code: str, date: str = "") -> float:
    """
    將三大法人合計淨買超轉換為 -1.0 ~ 1.0 的情緒分數：
      ≥ CHIPS_STRONG_BUY  → +1.0
      ≤ CHIPS_STRONG_SELL → -1.0
      線性內插其餘區間
    回傳 0.0 表示無資料或中立
    """
    chips = get_stock_chips(code, date)
    total = chips.get("total", 0)
    if total == 0:
        return 0.0
    score = max(-1.0, min(1.0, total / CHIPS_STRONG_BUY))
    return round(score, 2)


def chips_summary(code: str, date: str = "") -> str:
    """回傳可讀的籌碼摘要字串"""
    chips = get_stock_chips(code, date)
    if not chips:
        return f"[籌碼] {code} 今日無法人資料"
    total   = chips.get("total", 0)
    foreign = chips.get("foreign", 0)
    trust   = chips.get("trust", 0)
    dealer  = chips.get("dealer", 0)
    label   = "買超" if total > 0 else "賣超"
    return (
        f"[籌碼] {code}  三大法人{label} {abs(total):,}股\n"
        f"  外資 {foreign:+,}  投信 {trust:+,}  自營 {dealer:+,}"
    )


# ---------------------------------------------------------------------------
# 快速測試
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    codes = ["2330", "2317", "2454", "3661"]
    print(f"=== 三大法人籌碼（{_today_tw()}）===")
    for c in codes:
        score = chips_sentiment(c)
        summary = chips_summary(c)
        print(f"{summary}  → 情緒分: {score:+.2f}")
