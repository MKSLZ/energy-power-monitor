"""中国煤与电：秦港/CCTD 煤价、广东电力现货、上海石油天然气交易中心 SHPGX。"""
from __future__ import annotations

import re

from .base import http_get, html_links, make_event, make_quote


def fetch_quotes() -> list[dict]:
    """秦港 Q5500、SHPGX LNG、广东日前。门户多反爬，抓不到即 N/A。"""
    quotes: list[dict] = []

    # 秦港 Q5500：CCTD / 秦皇岛煤炭网，反爬则 N/A
    qinhuangdao = None
    qhd_urls = ["https://www.cctd.com.cn/", "http://www.cqcoal.com/"]
    for u in qhd_urls:
        txt = http_get(u, timeout=10)
        if not txt:
            continue
        m = re.search(r"(Q5500?[^0-9]{0,8})([0-9]{3,4})\s*元", txt)
        if m:
            try:
                qinhuangdao = float(m.group(2))
                break
            except ValueError:
                qinhuangdao = None
    quotes.append(make_quote("秦港 Q5500 平仓价", qinhuangdao, "元/吨", None, None,
                             "日度（CCTD/秦煤网反爬则 N/A）",
                             "https://www.cctd.com.cn/",
                             status="ok" if qinhuangdao is not None else "error"))

    # SHPGX 出厂价
    shpgx = None
    txt = http_get("http://www.shpgx.com/", timeout=10)
    if txt:
        m = re.search(r"([0-9]{4,5})\s*元/吨", txt)
        if m:
            try:
                shpgx = float(m.group(1))
            except ValueError:
                shpgx = None
    quotes.append(make_quote("中国 LNG 出厂指数 SHPGX", shpgx, "元/吨", None, None,
                             "日度", "http://www.shpgx.com/",
                             status="ok" if shpgx is not None else "error"))

    # 广东日前现货：门户滞后，必须标数据日期；价 N/A（公告滞后）
    quotes.append(make_quote("广东日前现货均价", None, "元/MWh", None, None,
                             "门户约滞后 3–5 个工作日（必标数据日期，价 N/A）",
                             "https://pm.gd.csg.cn/portal/", status="error"))
    return quotes


def fetch_news() -> list[dict]:
    events: list[dict] = []
    eid = 0
    for url, src in [
        ("https://www.cctd.com.cn/", "CCTD"),
        ("http://www.cqcoal.com/", "秦皇岛煤炭网"),
        ("https://pm.gd.csg.cn/portal/", "广东电力交易中心"),
        ("http://www.shpgx.com/", "上海石油天然气交易中心"),
    ]:
        try:
            links = html_links(url, limit=12)
        except Exception:  # noqa: BLE001
            links = []
        for lk in links:
            eid += 1
            events.append(make_event(
                f"CNC{eid:02d}", "—", src, lk["title"], "", lk["url"], "中国煤与电"))
    return events


def fetch_status(quotes: list[dict], n_news: int) -> list[dict]:
    rows = []
    ok = [q for q in quotes if q["status"] == "ok"]
    bad = [q for q in quotes if q["status"] != "ok"]
    if ok:
        rows.append({
            "chip": "已更新", "chip_cls": "bull",
            "content": "；".join(q["name"] for q in ok),
            "data": "；".join(f'{q["name"]} {q["price"]}{q["unit"]}' for q in ok),
        })
    if bad:
        rows.append({
            "chip": "沿用上期", "chip_cls": "neu",
            "content": "；".join(q["name"] for q in bad),
            "data": "门户反爬/滞后，标数据日期 + N/A",
        })
    return rows
