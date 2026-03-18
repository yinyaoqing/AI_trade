"""
3.1 參數自動優化 — Grid Search
================================
對 stop_loss / trailing_start / trailing_pullback / rsi_overbought
進行窮舉組合，以「夏普比率」為主要評估指標，輸出最佳參數組合。

執行方式：
    python optimize.py --code 2330 --start 2025-01-01 --end 2025-12-31
    python optimize.py --code 2330 --start 2025-01-01 --top 5
"""

import argparse
import itertools
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv

from backtest import BacktestEngine, BacktestResult

load_dotenv()
TZ_TW = timezone(timedelta(hours=8))

# ── 搜尋空間（縮減組合數，節省 API 呼叫）────────────────────
PARAM_GRID = {
    "stop_loss":        [0.015, 0.020, 0.025, 0.030],
    "trailing_start":   [0.010, 0.015, 0.020, 0.025],
    "trailing_pullback":[0.008, 0.010, 0.012],
    "rsi_overbought":   [65,    70,    75],
}


def grid_search(
    engine: BacktestEngine,
    code: str,
    start: str,
    end: str,
    top_n: int = 10,
    verbose: bool = False,
) -> pd.DataFrame:
    """
    窮舉 PARAM_GRID 中所有組合，回傳以夏普比率排序的結果 DataFrame。
    """
    keys   = list(PARAM_GRID.keys())
    combos = list(itertools.product(*PARAM_GRID.values()))
    total  = len(combos)
    print(f"[優化] 搜尋 {total} 種參數組合  標的={code}  {start} ~ {end}")

    records = []
    for i, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))

        # 過濾不合理的組合（止盈啟動 ≤ 回吐容忍）
        if params["trailing_start"] <= params["trailing_pullback"]:
            continue

        result: BacktestResult = engine.run(code, start, end, params=params)
        if not result.trades:
            continue

        s = result.summary()
        # 將字串解析回數字
        def _f(key: str) -> float:
            v = s.get(key, "0").replace("%", "").replace("元", "").replace("+", "").strip()
            try:
                return float(v)
            except ValueError:
                return 0.0

        records.append({
            **params,
            "交易次數": len(result.trades),
            "勝率%":   round(_f("勝率") * 100 if "%" in s.get("勝率","") else _f("勝率"), 1),
            "夏普":    round(_f("夏普比率"), 3),
            "最大回撤%": round(_f("最大回撤"), 2),
            "獲利因子":  round(_f("獲利因子"), 2),
            "淨損益":    round(_f("淨損益合計"), 0),
        })

        if verbose or i % 20 == 0:
            print(f"  [{i}/{total}] stop={params['stop_loss']:.3f}  "
                  f"trail={params['trailing_start']:.3f}/{params['trailing_pullback']:.3f}  "
                  f"rsi={params['rsi_overbought']}  "
                  f"夏普={records[-1]['夏普']:.2f}")

    if not records:
        print("[優化] 無有效結果，請擴大搜尋範圍或延長回測期間。")
        return pd.DataFrame()

    df = pd.DataFrame(records).sort_values("夏普", ascending=False).reset_index(drop=True)
    return df.head(top_n)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Trade 參數優化")
    parser.add_argument("--code",  default="2330",       help="股票代碼")
    parser.add_argument("--start", default="2025-01-01", help="回測起始日")
    parser.add_argument("--end",   default=datetime.now(TZ_TW).strftime("%Y-%m-%d"))
    parser.add_argument("--top",   type=int, default=10, help="顯示前 N 名")
    parser.add_argument("--codes", nargs="+",            help="多標的同時優化（如 2330 2317）")
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

    targets = args.codes or [args.code]

    all_results = {}
    for code in targets:
        print(f"\n{'='*50}")
        print(f"  優化標的：{code}")
        print(f"{'='*50}")
        df = grid_search(engine, code, args.start, args.end, top_n=args.top)
        all_results[code] = df

        if df.empty:
            continue

        print(f"\n  ── 前 {args.top} 名參數組合 ─────────────────────")
        print(df.to_string(index=False))

        best = df.iloc[0]
        print(f"\n  ★ 最佳組合（夏普={best['夏普']:.3f}）")
        print(f"    stop_loss        = {best['stop_loss']}")
        print(f"    trailing_start   = {best['trailing_start']}")
        print(f"    trailing_pullback= {best['trailing_pullback']}")
        print(f"    rsi_overbought   = {best['rsi_overbought']}")
        print(f"\n    請將以上數值更新至 bot.py 對應參數。")

    # 多標的彙總（若有多標的）
    if len(targets) > 1:
        print(f"\n{'='*50}")
        print("  多標的最佳參數彙總")
        print(f"{'='*50}")
        rows = []
        for code, df in all_results.items():
            if df.empty:
                continue
            b = df.iloc[0].to_dict()
            b["code"] = code
            rows.append(b)
        if rows:
            summary_df = pd.DataFrame(rows).set_index("code")
            print(summary_df[["stop_loss","trailing_start","trailing_pullback",
                               "rsi_overbought","夏普","最大回撤%","勝率%"]].to_string())

    api.logout()


if __name__ == "__main__":
    main()
