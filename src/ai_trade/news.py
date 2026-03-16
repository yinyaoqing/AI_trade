"""
台股免費新聞聚合器

整合以下免費來源：
  1. 鉅亨網   RSS
  2. MoneyDJ  RSS
  3. Google News RSS（台股關鍵字）
  4. PTT Stock 板（官方 JSON API）
  5. 證交所重大訊息（TWSE Open Data）
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime
    summary: str = ""

    @property
    def digest(self) -> str:
        """用標題 hash 去重複"""
        return hashlib.md5(self.title.encode()).hexdigest()


# ---------------------------------------------------------------------------
# 各來源 fetcher
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    )
}
TIMEOUT = 10


def _parse_rss(url: str, source_name: str) -> list[NewsItem]:
    """通用 RSS 解析"""
    try:
        feed = feedparser.parse(url)
        items = []
        for e in feed.entries[:20]:
            pub = e.get("published_parsed") or e.get("updated_parsed")
            dt = (
                datetime(*pub[:6], tzinfo=timezone.utc)
                if pub
                else datetime.now(timezone.utc)
            )
            items.append(NewsItem(
                title=e.get("title", "").strip(),
                source=source_name,
                url=e.get("link", ""),
                published=dt,
                summary=BeautifulSoup(
                    e.get("summary", ""), "html.parser"
                ).get_text()[:200],
            ))
        return items
    except Exception as e:
        print(f"[新聞] {source_name} RSS 失敗: {e}")
        return []


def fetch_cnyes() -> list[NewsItem]:
    """鉅亨網 — 台股新聞 JSON API（回傳全市場新聞供 AI 分析大盤情緒）"""
    items = []
    try:
        category = "tw_stock_news"
        url = f"https://api.cnyes.com/media/api/v1/newslist/category/{category}?limit=20"
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("items", {}).get("data", [])
        for item in data:
            title = item.get("title", "").strip()
            summary = item.get("summary", "") or ""
            if not title:
                continue
            # 不過濾個股代碼，回傳全市場新聞供 AI 分析大盤情緒
            news_id = item.get("newsId", "")
            items.append(NewsItem(
                title=title,
                source="鉅亨網",
                url=f"https://news.cnyes.com/news/id/{news_id}",
                published=datetime.fromtimestamp(item.get("publishAt", 0), tz=timezone.utc),
                summary=summary[:200],
            ))
    except Exception as e:
        print(f"[新聞] 鉅亨網失敗: {e}")
    return items


def fetch_yahoo_tw(stock_code: str = "") -> list[NewsItem]:
    """Yahoo 奇摩股市 — RSS 新聞"""
    if stock_code:
        url = f"https://tw.stock.yahoo.com/rss?s={stock_code}"
    else:
        url = "https://tw.stock.yahoo.com/rss"
    return _parse_rss(url, "Yahoo股市")


def fetch_google_news(stock_code: str = "", keyword: str = "台股") -> list[NewsItem]:
    """Google News RSS — 依關鍵字搜尋"""
    query = stock_code if stock_code else keyword
    url = f"https://news.google.com/rss/search?q={query}+台股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    return _parse_rss(url, "Google News")


def fetch_ptt_stock() -> list[NewsItem]:
    """PTT Stock 板 — 透過 pushshift 非官方鏡像（官方常封鎖爬蟲）"""
    items = []
    # PTT 官方端點容易被 reset，改用 RSS 鏡像
    url = "https://www.ptt.cc/bbs/Stock/index.rss"
    try:
        resp = requests.get(
            url,
            headers={**HEADERS, "Cookie": "over18=1"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        for e in feed.entries[:20]:
            title = e.get("title", "").strip()
            if not title or "(已刪除)" in title:
                continue
            items.append(NewsItem(
                title=title,
                source="PTT Stock",
                url=e.get("link", ""),
                published=datetime.now(timezone.utc),
                summary=BeautifulSoup(e.get("summary", ""), "html.parser").get_text()[:200],
            ))
    except Exception as e:
        print(f"[新聞] PTT Stock 失敗: {e}")
    return items


def fetch_twse_announcements(stock_code: str = "") -> list[NewsItem]:
    """證交所重大訊息 — MOPS 公開資訊觀測站 RSS"""
    # MOPS 提供各類重大訊息 RSS
    rss_urls = [
        ("https://mops.twse.com.tw/mops/web/rss?step=1&TYPEK=sii", "上市重大訊息"),
        ("https://mops.twse.com.tw/mops/web/rss?step=1&TYPEK=otc", "上櫃重大訊息"),
    ]
    items = []
    for url, label in rss_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.encoding = "utf-8"
            feed = feedparser.parse(resp.text)
            for e in feed.entries[:20]:
                title = e.get("title", "").strip()
                if not title:
                    continue
                if stock_code and stock_code not in title and stock_code not in e.get("summary", ""):
                    continue
                pub = e.get("published_parsed") or e.get("updated_parsed")
                dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else datetime.now(timezone.utc)
                items.append(NewsItem(
                    title=title,
                    source=f"MOPS {label}",
                    url=e.get("link", "https://mops.twse.com.tw"),
                    published=dt,
                    summary=BeautifulSoup(e.get("summary", ""), "html.parser").get_text()[:200],
                ))
        except Exception as e:
            print(f"[新聞] MOPS {label} 失敗: {e}")
    return items


# ---------------------------------------------------------------------------
# 聚合器
# ---------------------------------------------------------------------------

class NewsAggregator:
    """整合所有來源，回傳去重後的最新新聞清單"""

    SOURCES = [
        fetch_cnyes,        # 鉅亨網 JSON API
        fetch_yahoo_tw,     # Yahoo 奇摩股市 RSS
        fetch_google_news,  # Google News RSS
    ]

    def __init__(self, stock_code: str = ""):
        self.stock_code = stock_code
        self._seen: set[str] = set()

    def fetch_all(self) -> list[NewsItem]:
        """從所有來源抓取，去重後依時間排序"""
        all_items: list[NewsItem] = []
        for source_fn in self.SOURCES:
            try:
                if source_fn == fetch_cnyes:
                    items = source_fn()
                elif source_fn == fetch_yahoo_tw:
                    items = source_fn(self.stock_code)
                elif source_fn == fetch_google_news:
                    items = source_fn(stock_code=self.stock_code)
                else:
                    items = source_fn()
                all_items.extend(items)
            except Exception as e:
                print(f"[新聞] {source_fn.__name__} 例外: {e}")

        # 去重 + 排序
        unique: list[NewsItem] = []
        for item in all_items:
            if item.digest not in self._seen and item.title:
                self._seen.add(item.digest)
                unique.append(item)

        unique.sort(key=lambda x: x.published, reverse=True)
        return unique

    def fetch_today(self, limit: int = 20) -> list[NewsItem]:
        """只回傳今日（台灣時間）的新聞，按時間排序"""
        from datetime import timezone, timedelta
        tz_tw = timezone(timedelta(hours=8))
        today_tw = datetime.now(tz_tw).date()
        items = [
            i for i in self.fetch_all()
            if i.published.astimezone(tz_tw).date() >= today_tw
        ]
        return items[:limit]

    def fetch_headlines(self, limit: int = 10) -> str:
        """回傳今日新聞標題字串，供 AI 分析使用"""
        items = self.fetch_today(limit)
        if not items:
            # 若今日無新聞（非交易日/深夜），回退到最新 N 則
            items = self.fetch_all()[:limit]
        if not items:
            return ""
        lines = [f"[{i.source}] {i.title}" for i in items]
        return "\n".join(lines)

    def format_telegram_digest(self, limit: int = 10) -> str:
        """回傳附連結的 Telegram 新聞摘要（Markdown 格式）"""
        from datetime import timezone, timedelta
        tz_tw = timezone(timedelta(hours=8))
        items = self.fetch_today(limit)
        if not items:
            items = self.fetch_all()[:limit]
        if not items:
            return "（目前無新聞）"
        lines = []
        for i in items:
            pub = i.published.astimezone(tz_tw).strftime("%H:%M")
            # Telegram MarkdownV2 需跳脫特殊字元，這裡用純文字模式
            lines.append(f"[{pub}] [{i.source}] {i.title}\n{i.url}")
        return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 快速測試
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    agg = NewsAggregator(stock_code="2330")
    print("=== 台積電相關新聞 (2330) ===")
    headlines = agg.fetch_headlines(limit=15)
    print(headlines if headlines else "（無新聞）")

    print("\n=== 各來源筆數 ===")
    for fn in NewsAggregator.SOURCES:
        t = time.time()
        try:
            if fn == fetch_cnyes:
                r = fn()
            elif fn == fetch_yahoo_tw:
                r = fn("2330")
            elif fn == fetch_google_news:
                r = fn(stock_code="2330")
            else:
                r = fn()
            print(f"  {fn.__name__:35s} {len(r):3d} 筆  ({time.time()-t:.1f}s)")
        except Exception as ex:
            print(f"  {fn.__name__:35s} 失敗: {ex}")
