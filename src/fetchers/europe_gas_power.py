"""欧洲天然气与电力：GIE AGSI 储气、EPEX/Nord Pool 电价、TTF/JKM 候选行情。"""
from __future__ import annotations

import os
import re

from .base import (http_get, http_get_json, html_links, make_event, make_quote,
                   parse_rss)


def fetch_quotes() -> list[dict]:
    """TTF / JKM / HH / EPEX 德国日前。多候选，抓不到即 N/A。"""
    quotes: list[dict] = []

    # TTF：腾讯无 TTF，候选 investing/barchart RSS；失败降级
    ttf_urls = [
        "https://www.barchart.com/futures/quotes/TTFF2/overview",
        "https://www.investing.com/commodity/dutch-ttf-gas-futures",
    ]
    ttf_val = None
    for u in ttf_urls:
        txt = http_get(u, headers={"Referer": "https://www.google.com/"})
        if txt:
            m = re.search(r"([0-9]{2,3}\.[0-9]{1,2})\s*€", txt)
            if m:
                try:
                    ttf_val = float(m.group(1))
                    break
                except ValueError:
                    ttf_val = None
    quotes.append(make_quote("欧洲 TTF", ttf_val, "€/MWh", None, None,
                             "盘中（源抓取失败则 N/A）", ttf_urls[0],
                             status="ok" if ttf_val is not None else "error"))

    # JKM：Platts 指示价无公开稳定接口，候选 RSS，失败 N/A
    jkm_val = None
    quotes.append(make_quote("亚洲 JKM", jkm_val, "$/MMBtu", None, None,
                             "指示价（无公开实时源 → N/A）",
                             "https://www.spglobal.com/commodityinsights/en",
                             status="error" if jkm_val is None else "ok"))

    # EPEX 德国日前：门户无稳定公开报价 API，抓首页链接作为证据，价 N/A
    epex_url = "https://www.epexspot.com/en/market-data"
    quotes.append(make_quote("EPEX 德国日前", None, "€/MWh", None, None,
                             "门户出清约 18:00 后更新（价 N/A）", epex_url, status="error"))
    return quotes


def fetch_agsi() -> dict | None:
    """GIE AGSI 欧洲储气率。无 key 时 data 为空 → 返回结构化描述，不编数。"""
    api = "https://agsi.gie.eu/api?country=EU"
    data = http_get_json(api)
    if not data:
        return None
    gas_day = data.get("gas_day", "—")
    rows = data.get("data") or []
    if rows:
        latest = rows[0] if isinstance(rows, list) else {}
        full = latest.get("full", latest.get("gasInStorage", "—"))
        return {"gas_day": gas_day, "full": full, "via": "AGSI API", "url": "https://agsi.gie.eu/"}
    # API 可达但无 key 返回空 data
    return {"gas_day": gas_day, "full": "N/A（无 API key，仅 gas_day 元数据）",
            "via": "AGSI API（受限）", "url": "https://agsi.gie.eu/"}


def fetch_news() -> list[dict]:
    events: list[dict] = []
    eid = 0
    # GIE 首页公告 / ICIS 类
    for url, src in [("https://www.gie.eu/", "GIE"),
                     ("https://www.nordpoolgroup.com/en/news/", "Nord Pool")]:
        try:
            links = html_links(url, limit=10)
        except Exception:  # noqa: BLE001
            links = []
        for lk in links:
            eid += 1
            events.append(make_event(
                f"EU{eid:02d}", "—", src, lk["title"], "", lk["url"], "欧洲气与电"))
    return events


def fetch_status(agsi: dict | None, quotes: list[dict], n_news: int) -> list[dict]:
    rows = []
    if agsi and agsi.get("full") not in (None, "—"):
        rows.append({
            "chip": "已更新" if str(agsi["full"]).replace(".", "").isdigit() else "沿用上期",
            "chip_cls": "bull" if str(agsi["full"]).replace(".", "").isdigit() else "neu",
            "content": "GIE AGSI 欧洲储气率",
            "data": f'{agsi.get("gas_day")} 满库度 {agsi.get("full")}',
        })
    else:
        rows.append({
            "chip": "未抓到", "chip_cls": "amb",
            "content": "AGSI 逐日值（无 EIA 类公开 key）",
            "data": "gas_day 元数据可达，满库度 N/A",
        })
    bad = [q["name"] for q in quotes if q["status"] != "ok"]
    if bad:
        rows.append({
            "chip": "沿用上期", "chip_cls": "neu",
            "content": "TTF/JKM/EPEX 实时价",
            "data": "；".join(bad) + "（门户滞后/无公开接口，标 N/A）",
        })
    return rows
