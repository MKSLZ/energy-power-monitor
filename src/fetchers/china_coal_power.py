"""中国煤与电：秦港/CCTD 煤价、广东电力现货、上海石油天然气交易中心 SHPGX。
另含 INE 上海原油 SC 主力、大连焦煤主力 JM0（新浪国内期货，必须带 Referer）。"""
from __future__ import annotations

import re

from .base import http_get, html_links, make_event, make_quote

_SINA_REFERER = "https://finance.sina.com.cn"


def _fetch_sina_futures(symbol: str, name: str, unit: str) -> dict:
    """新浪国内期货连续主力 nf_XXX（如 nf_SC0 / nf_JM0）。

    硬要求：必须带 Referer: https://finance.sina.com.cn；响应为 GBK。
    实测逗号串字段：0=名称 7=最新价 9=昨结算 17=日期。
    防御式解析：任一字段缺失/非法即降级 status="error"、price=None，绝不拖垮整轮。
    （无强反爬公开候选时，新浪为主用源；腾讯等仅作后续可扩展备选。）
    """
    api = f"https://hq.sinajs.cn/list={symbol}"
    page = f"https://finance.sina.com.cn/futures/quotes/{symbol[3:]}.shtml"  # 人类可读行情页
    txt = http_get(api, headers={"Referer": _SINA_REFERER}, encoding="gbk", timeout=10)
    if not txt:
        return make_quote(name, None, unit, None, None, "新浪行情接口无响应", page, status="error")
    m = re.search(r'hq_str_%s="([^"]*)"' % re.escape(symbol), txt)
    if not m:
        return make_quote(name, None, unit, None, None, "新浪行情串未匹配", page, status="error")
    parts = m.group(1).split(",")
    try:
        price = float(parts[7])
        prev = float(parts[9])
        if price <= 0 or prev <= 0:
            raise ValueError("bad price")
        day_chg = round((price - prev) / prev * 100, 2)
        date = parts[17] if len(parts) > 17 else "—"
        return make_quote(name, price, unit, day_chg, None,
                          f"新浪期货 {date}", page, status="ok")
    except (ValueError, IndexError):
        return make_quote(name, None, unit, None, None, "新浪期货字段解析失败", page, status="error")


def fetch_quotes() -> list[dict]:
    """INE SC 主力、大连焦煤 JM0、秦港 Q5500、SHPGX LNG、广东日前。门户多反爬，抓不到即 N/A。"""
    quotes: list[dict] = []

    # INE 上海原油 SC 连续主力（国内原油口径主行情）
    quotes.append(_fetch_sina_futures("nf_SC0", "INE SC 主力", "元/桶"))

    # 大连商品交易所 焦煤连续主力（国内煤炭口径之一）
    quotes.append(_fetch_sina_futures("nf_JM0", "大连焦煤主力 JM0", "元/吨"))

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
