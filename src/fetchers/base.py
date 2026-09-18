"""共享 HTTP / 解析工具。

设计原则（硬约束）：
- 每个源独立 try/except，任何失败都降级为 None / N/A，绝不让整轮崩。
- 统一 UA、超时 10s、最多 1 次重试。
- 抓到的 url 原样透传，抓不到就标 N/A，禁止编数。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 10
RETRY = 1  # 额外重试次数（共最多 2 次尝试）

_session: requests.Session | None = None


def get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        _session = s
    return _session


def http_get(url: str, *, headers: dict | None = None, encoding: str | None = None,
             timeout: int = TIMEOUT) -> str | None:
    """GET 文本。失败返回 None（调用方降级）。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last_exc: Exception | None = None
    for attempt in range(RETRY + 1):
        try:
            r = get_session().get(url, headers=h, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                if encoding:
                    r.encoding = encoding
                return r.text
            last_exc = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001 - 网络层任意错误都降级
            last_exc = e
        if attempt < RETRY:
            time.sleep(0.6)
    return None


def http_get_json(url: str, *, headers: dict | None = None,
                  timeout: int = TIMEOUT) -> Any:
    """GET JSON。失败返回 None。"""
    txt = http_get(url, headers=headers, timeout=timeout)
    if not txt:
        return None
    try:
        import json
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return None


def parse_rss(url: str, limit: int = 30) -> list[dict]:
    """解析 RSS/Atom，返回 [{title, summary, link, published}]。

    关键：先用带超时的 http_get 取文本，再喂给 feedparser.parse(content)，
    避免 feedparser 内置 urllib 无超时挂死在不可达源上。失败 []。
    """
    import feedparser
    txt = http_get(url)
    if not txt:
        return []
    try:
        d = feedparser.parse(txt)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in d.entries[:limit]:
        out.append({
            "title": (getattr(e, "title", "") or "").strip(),
            "summary": (getattr(e, "summary", "") or getattr(e, "description", "") or "").strip(),
            "link": (getattr(e, "link", "") or "").strip(),
            "published": (getattr(e, "published", "") or getattr(e, "updated", "") or "").strip(),
        })
    return out


def html_links(url: str, *, encoding: str | None = None, limit: int = 30) -> list[dict]:
    """抓一个 HTML 列表页，抽出 <a> 文本与 href。失败 []。"""
    from bs4 import BeautifulSoup
    txt = http_get(url, encoding=encoding)
    if not txt:
        return []
    try:
        soup = BeautifulSoup(txt, "lxml")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in soup.find_all("a", href=True)[: limit * 3]:
        text = " ".join(a.get_text(" ", strip=True).split())
        href = a["href"]
        if not text or len(text) < 6:
            continue
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        if not href.startswith("http"):
            continue
        out.append({"title": text, "url": href})
        if len(out) >= limit:
            break
    return out


def make_quote(name: str, price, unit: str, day_chg, week_chg,
               src_time: str, url: str, status: str = "ok") -> dict:
    """统一行情结构。price/day_chg 抓不到用 None。"""
    return {
        "name": name,
        "price": price,
        "unit": unit,
        "day_chg": day_chg,
        "week_chg": week_chg,
        "src_time": src_time,
        "url": url,
        "status": status,
    }


def make_event(eid: str, time: str, source: str, title: str, summary: str,
               url: str, category: str, raw_price: str | None = None) -> dict:
    return {
        "id": eid,
        "time": time,
        "source": source,
        "title": title,
        "summary": summary,
        "url": url,
        "category": category,
        "raw_price": raw_price,
    }


def pick_first(candidates: Iterable[str], fn, *args) -> tuple[Any, str | None]:
    """对一组候选 URL 依次尝试 fn(url,*args)，返回首个非空结果与命中的 url。"""
    for url in candidates:
        try:
            res = fn(url, *args)
        except Exception:  # noqa: BLE001
            res = None
        if res:
            return res, url
    return None, None
