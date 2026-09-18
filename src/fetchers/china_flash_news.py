"""国内快讯与政策：财联社、证券时报、新浪财经、国家能源局、发改委、中电联。

按 油/气/煤/电 关键词过滤。每个源独立 try/except。
"""
from __future__ import annotations

import re

from .base import html_links, make_event

# 关键词过滤：命中任一即收录
KEYWORDS = [
    "油", "原油", "Brent", "WTI", "SC", "成品油", "OPEC", "EIA",
    "气", "天然气", "LNG", "TTF", "储气",
    "煤", "动力煤", "焦煤", "秦港", "坑口",
    "电", "电力", "电价", "现货", "风电", "光伏", "核电", "水电", "来水",
    "储能", "碳", "能源", "电网",
]


def _keep(title: str) -> bool:
    return any(k in title for k in KEYWORDS)


def fetch_news() -> list[dict]:
    events: list[dict] = []
    eid = 0

    sources = [
        ("https://www.cls.cn/telegraph", "财联社", None),
        ("https://www.stcn.com/", "证券时报", None),
        ("https://finance.sina.com.cn/", "新浪财经", None),
        ("http://www.nea.gov.cn/", "国家能源局", None),
        ("https://www.ndrc.gov.cn/", "发改委", None),
        ("https://www.cec.org.cn/", "中电联", None),
    ]
    for url, src, _enc in sources:
        try:
            links = html_links(url, limit=25)
        except Exception:  # noqa: BLE001
            links = []
        for lk in links:
            if not _keep(lk["title"]):
                continue
            eid += 1
            events.append(make_event(
                f"FL{eid:02d}", "—", src, lk["title"], "", lk["url"], "国内快讯政策"))
    return events


def fetch_status(n_kept: int) -> list[dict]:
    if n_kept > 0:
        return [{
            "chip": "已更新", "chip_cls": "neu",
            "content": "财联社/证券时报/新浪 + 能源局/发改委/中电联（按油气煤电过滤）",
            "data": f"过滤后 {n_kept} 条",
        }]
    return [{
        "chip": "未抓到", "chip_cls": "amb",
        "content": "国内快讯/政策源",
        "data": "门户不可达，按 N/A",
    }]
