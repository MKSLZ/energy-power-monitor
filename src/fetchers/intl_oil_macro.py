"""国际油气 / 宏观 / 地缘：Brent/WTI/HH 行情 + 路透/新华社/OPEC/IEA/EIA 新闻。"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

from .base import (http_get, http_get_json, make_event, make_quote,
                   parse_rss, pick_first)

_BEIJING = timezone(timedelta(hours=8))

# 英文标题命中任一才收（能源专业源也统一过滤，拦住偶发无关稿）
_EN_HIT = (
    "oil", "crude", "brent", "wti", "opec", "natural gas", "lng", "gas", "coal",
    "power", "electricity", "energy", "refinery", "pipeline", "tanker", "barrel",
    "iran", "iraq", "saudi", "israel", "gaza", "yemen", "houthi", "syria", "qatar",
    "russia", "ukraine", "red sea", "hormuz", "suez", "strait", "sanction",
    "embargo", "tariff", "export", "inventory", "stockpile", "eia", "iea",
    "fed ", "dollar", "inflation", "rate cut", "rate hike", "middle east",
)
_GEO_HIT = ("iran", "iraq", "saudi", "israel", "gaza", "yemen", "houthi", "syria",
            "qatar", "russia", "ukraine", "red sea", "hormuz", "suez", "strait",
            "sanction", "embargo", "missile", "airstrike", "drone", "ceasefire",
            "opec", "tanker", "middle east", "tariff", "export curb", "pipeline")
_MAC_HIT = ("fed ", "dollar", "inflation", "rate cut", "rate hike", "treasury",
            "powell", "cpi", "pmi", "recession", "imf", "tariff")


def _en_keep(t: str) -> bool:
    return any(k in (t or "").lower() for k in _EN_HIT)


def _en_category(t: str) -> str:
    tl = (t or "").lower()
    if any(k in tl for k in _GEO_HIT):
        return "地缘政治与制裁"
    if any(k in tl for k in _MAC_HIT):
        return "宏观金融"
    return "供需库存"


def _rss2(url: str, limit: int = 14, timeout: int = 20) -> list[dict]:
    """同 parse_rss，但可放宽超时（部分官方源跨境 >10s）。返回键 link/published。"""
    import feedparser
    txt = http_get(url, timeout=timeout)
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


def _cn_keep(t: str) -> bool:
    from .china_flash_news import CN_KEEP
    return any(k in t for k in CN_KEEP)


def _is_geo_cn(t: str) -> bool:
    return any(k in t for k in ("中东", "伊朗", "沙特", "以色列", "俄", "乌克兰", "红海",
                                "胡塞", "OPEC", "欧佩克", "制裁", "袭击", "停火", "海峡",
                                "油轮", "断供", "禁运", "加沙", "卡塔尔", "伊拉克", "叙利亚"))


def _is_macro_cn(t: str) -> bool:
    return any(k in t for k in ("美联储", "加息", "降息", "美元", "通胀", "关税", "央行",
                                "CPI", "PMI", "衰退", "非农", "利率"))



# 腾讯全球行情（实测可用）。符号 → 标准名。
TENCENT_SYMBOLS = {
    "hf_OIL": ("Brent 原油", "$/桶"),
    "hf_CL": ("WTI 原油", "$/桶"),
    "hf_NG": ("NYMEX 天然气(HH)", "$/MMBtu"),
    "hf_GC": ("COMEX 黄金", "$/oz"),
    "hf_SI": ("COMEX 白银", "$/oz"),
}


def _parse_tencent(symbol: str, raw: str):
    """解析腾讯 v_xxx="..." 行。返回 dict 或 None。"""
    m = re.search(r'v_%s="([^"]*)"' % re.escape(symbol), raw)
    if not m:
        return None
    parts = m.group(1).split(",")
    # 实测字段序：0现价 1涨跌幅% 2买 3卖 4高 5低 6时间 7昨结 8今开 ... 12日期 13名称
    try:
        price = float(parts[0])
        day_chg = float(parts[1])
        hi, lo = parts[4], parts[5]
        tstr, pdate = parts[6], parts[12]
        name = parts[13] if len(parts) > 13 else symbol
        return {"price": price, "day_chg": day_chg, "high": hi, "low": lo,
                "time": f"{pdate} {tstr}", "name": name}
    except (ValueError, IndexError):
        return None


def fetch_quotes() -> list[dict]:
    """Brent/WTI/HH/gold/silver 实时行情。"""
    symbols = ",".join(TENCENT_SYMBOLS.keys())
    url = f"https://qt.gtimg.cn/q={symbols}"
    txt = http_get(url, headers={"Referer": "https://gu.qq.com/"})
    quotes: list[dict] = []
    if not txt:
        for sym, (nm, unit) in TENCENT_SYMBOLS.items():
            quotes.append(make_quote(nm, None, unit, None, None, "N/A", url, status="error"))
        return quotes

    for sym, (nm, unit) in TENCENT_SYMBOLS.items():
        d = _parse_tencent(sym, txt)
        if d:
            quotes.append(make_quote(nm, d["price"], unit, d["day_chg"], None,
                                      d["time"], url, status="ok"))
        else:
            quotes.append(make_quote(nm, None, unit, None, None, "N/A", url, status="error"))
    return quotes


def fetch_news() -> list[dict]:
    """国际油气/宏观/地缘：OilPrice、CNBC 能源、EIA 官方 RSS + 华尔街见闻全球快讯。
    全部为云端网络实测可达源；任一失败独立降级，不影响其他源。"""
    events: list[dict] = []
    eid = 0

    rss_feeds = [
        ("https://oilprice.com/rss/main", "OilPrice"),
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "CNBC 能源"),
        ("https://www.eia.gov/rss/todayinenergy.xml", "EIA 官方"),
    ]
    for rss_url, src in rss_feeds:
        try:
            items = _rss2(rss_url, limit=14)
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            title = it["title"]
            if not title or not _en_keep(title + " " + it["summary"]):
                continue
            eid += 1
            events.append(make_event(
                f"INTL{eid:02d}", it["published"] or "—", src, title,
                re.sub(r"<[^>]+>", "", it["summary"])[:240],
                it["link"] or "—", _en_category(title + " " + it["summary"])))

    # 华尔街见闻·全球快讯（JSON；中东/俄乌/OPEC/美联储等中文实时电报）
    try:
        j = http_get_json(
            "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&client=pc&limit=50",
            timeout=15)
        items = (j or {}).get("data", {}).get("items", [])
    except Exception:  # noqa: BLE001
        items = []
    for x in items:
        body = re.sub(r"<[^>]+>", "", x.get("content_text") or "")
        title = (x.get("title") or body or "").strip()
        if not title:
            continue
        if not (_en_keep(title) or _cn_keep(title + body)):
            continue
        when = "—"
        try:
            when = datetime.fromtimestamp(int(x.get("display_time")), tz=_BEIJING).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            pass
        eid += 1
        cat = ("地缘政治与制裁" if _is_geo_cn(title + body)
               else ("宏观金融" if _is_macro_cn(title + body) else "供需库存"))
        events.append(make_event(
            f"INTL{eid:02d}", when, "华尔街见闻", title[:80],
            body[:240], x.get("uri") or "—", cat))

    return events


def fetch_status(fetched_events: list[dict], quotes: list[dict]) -> list[dict]:
    rows = []
    ok = [q["name"] for q in quotes if q["status"] == "ok"]
    bad = [q["name"] for q in quotes if q["status"] != "ok"]
    if ok:
        rows.append({
            "chip": "已更新", "chip_cls": "bull",
            "content": "腾讯全球行情（Brent/WTI/纽约天然气/贵金属）",
            "data": "；".join(f'{q["name"]} {q["price"]}' for q in quotes if q["status"] == "ok") or "—",
        })
    if bad:
        rows.append({
            "chip": "未抓到", "chip_cls": "amb",
            "content": "；".join(bad),
            "data": "行情接口失败，按 N/A 处理",
        })
    if fetched_events:
        rows.append({
            "chip": "已更新", "chip_cls": "neu",
            "content": "地缘/能源新闻：OilPrice、CNBC 能源、EIA、华尔街见闻全球快讯",
            "data": f"{len(fetched_events)} 条",
        })
    else:
        rows.append({
            "chip": "未抓到", "chip_cls": "amb",
            "content": "地缘/能源新闻（OilPrice/CNBC/EIA/华尔街见闻）",
            "data": "全部源不可达，按 N/A",
        })
    return rows
