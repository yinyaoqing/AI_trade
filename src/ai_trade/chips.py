"""
2.2 籌碼流向 — 三大法人買賣超（智能日期回溯版）
================================================
來源：證交所 TWSE Open Data（免費，無需帳號）

資料時效說明：
  台股三大法人買賣超每日約 14:30 公布。
  盤中（< 14:40）自動使用「前一交易日」資料，
  盤後（≥ 14:40）才讀取當日最新資料。
  遇週末、假日或非交易日無資料時，最多往前回溯 5 天。

提供：
  - get_institutional_flow(date)   → 全市場三大法人買賣超 dict
  - get_stock_chips(code, date)    → 單一個股外資/投信/自營商淨買超（股）
  - chips_sentiment(code, date)    → 轉換為 -1.0 ~ 1.0 情緒分數（供 AI 參考）
  - chips_summary(code, date)      → 人類可讀的籌碼摘要
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

# 資料公布緩衝時間：14:40 前視為今日資料尚未更新（留 10 分鐘緩衝）
_DATA_READY_HOUR   = 14
_DATA_READY_MINUTE = 40


def _get_target_date() -> str:
    """
    智能日期選擇：
      - 盤中（< 14:40）→ 返回昨天，讓後續回溯找到最近有效交易日
      - 盤後（≥ 14:40）→ 返回今天（資料已公布）
    """
    now = datetime.now(TZ_TW)
    if now.hour < _DATA_READY_HOUR or (
        now.hour == _DATA_READY_HOUR and now.minute < _DATA_READY_MINUTE
    ):
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")


@lru_cache(maxsize=10)
def _fetch_flow_for_date(date: str) -> dict[str, dict]:
    """
    取得「指定日期」三大法人買賣超（無回溯），結果依日期快取。
    返回空 dict 表示該日無資料（非交易日 / 資料尚未公布）。
    """
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date}&selectType=ALL&response=json"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") != "OK":
            return {}

        result: dict[str, dict] = {}
        for row in data.get("data", []):
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
        print(f"[籌碼] TWSE T86 取得失敗 ({date}): {e}")
        return {}


def get_institutional_flow(date: str = "") -> dict[str, dict]:
    """
    取得三大法人買賣超，回傳 {股票代碼: {foreign, trust, dealer, total}}。

    date 格式：YYYYMMDD，空字串表示「自動選日期」（盤中用昨日，盤後用今日）。
    若指定日期無資料，自動往前回溯最多 5 天（處理假日 / 非交易日）。
    """
    start_date = datetime.strptime(date or _get_target_date(), "%Y%m%d")

    for offset in range(5):
        target = (start_date - timedelta(days=offset)).strftime("%Y%m%d")
        flow = _fetch_flow_for_date(target)
        if flow:
            if offset > 0:
                print(f"[籌碼] {(start_date).strftime('%Y%m%d')} 無資料，自動遞補至 {target}")
            return flow

    print(f"[籌碼] 往前回溯 5 天仍無資料，回傳空集合")
    return {}


def get_stock_chips(code: str, date: str = "") -> dict:
    """回傳單一個股的三大法人資料；若無資料回傳空 dict"""
    return get_institutional_flow(date).get(code, {})


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
    """回傳可讀的籌碼摘要字串（含張數換算）"""
    chips = get_stock_chips(code, date)
    if not chips:
        return f"[籌碼] {code} 無法人資料（含回溯）"
    total   = chips.get("total", 0)
    foreign = chips.get("foreign", 0)
    trust   = chips.get("trust", 0)
    dealer  = chips.get("dealer", 0)
    label   = "買超" if total > 0 else "賣超"
    lots    = round(abs(total) / 1000)   # 換算為張，較直觀
    return (
        f"[籌碼] {code}  三大法人{label} {lots:,}張\n"
        f"  外資 {foreign:+,}股  投信 {trust:+,}股  自營 {dealer:+,}股"
    )


# ---------------------------------------------------------------------------
# 快速測試
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    codes = ["2330", "2317", "2454", "3661"]
    target = _get_target_date()
    print(f"=== 三大法人籌碼（目標日期：{target}，實際資料可能遞補至更早日期）===")
    for c in codes:
        score = chips_sentiment(c)
        summary = chips_summary(c)
        print(f"{summary}  → 情緒分: {score:+.2f}")
