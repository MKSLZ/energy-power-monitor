"""国际油气 / 宏观 / 地缘：Brent/WTI/HH 行情 + 路透/新华社/OPEC/IEA/EIA 新闻。"""
from __future__ import annotations

import re

from .base import (http_get, http_get_json, make_event, make_quote,
                   parse_rss, pick_first)

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
    """国际油气/宏观/地缘新闻：路透能源 RSS、新华社能源、OilPrice、OPEC/IEA 公告。"""
    events: list[dict] = []
    eid = 0

    # 注：feeds.reuters.com / reuters.com/.../rss 已实测不可达（连接超时），已剔除，
    # 避免每个死链白白消耗 ~11s。保留实测可达的候选；抓不到即降级 N/A。
    rss_candidates = [
        ("https://www.energyintel.com/rss", "Energy Intel"),
        ("https://www.oilprice.com/rss/article.xml", "OilPrice"),
        ("http://www.xinhuanet.com/energy/", "新华社能源"),
    ]
    for rss_url, src in rss_candidates:
        try:
            items = parse_rss(rss_url, limit=12)
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            if not it["title"]:
                continue
            eid += 1
            events.append(make_event(
                f"INTL{eid:02d}", it["published"] or "—", src,
                it["title"], re.sub(r"<[^>]+>", "", it["summary"])[:240],
                it["url"], "国际油气/宏观地缘"))

    # EIA：无 EIA_API_KEY 则标 N/A（不在此编数）
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
            "content": "国际油气/宏观/地缘新闻（路透/新华社/OilPrice RSS）",
            "data": f"{len(fetched_events)} 条",
        })
    else:
        rows.append({
            "chip": "未抓到", "chip_cls": "amb",
            "content": "国际新闻 RSS（路透/新华社/OilPrice）",
            "data": "RSS 不可达，按 N/A",
        })
    return rows
