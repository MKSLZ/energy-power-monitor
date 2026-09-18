"""天气与水文：中央气象台/台风路径、长江来水。拿不到就精确定性描述 + N/A。"""
from __future__ import annotations

from .base import html_links, make_event


def fetch_news() -> list[dict]:
    events: list[dict] = []
    eid = 0
    sources = [
        ("http://www.nmc.cn/publish/typhoon/warning.html", "中央气象台·台风"),
        ("http://www.cma.gov.cn/", "中国气象局"),
        ("http://www.cjw.gov.cn/", "长江水利委"),
        ("https://cjhy.mot.gov.cn/fw/cxfw/swgg/", "长江航务局"),
    ]
    for url, src in sources:
        try:
            links = html_links(url, limit=10)
        except Exception:  # noqa: BLE001
            links = []
        for lk in links:
            eid += 1
            events.append(make_event(
                f"WX{eid:02d}", "—", src, lk["title"], "", lk["url"], "天气季节"))
    # 兜底：拿不到逐日来水，给一句定性（不编数字）
    if not events:
        events.append(make_event(
            "WX01", "—", "天气/水文（定性）",
            "台风与来水逐日数据暂未抓到",
            "按定性描述处理：来水偏枯/偏丰、台风路径需以中央气象台与长江水利委原文为准，本项数值 N/A。",
            "http://www.nmc.cn/", "天气季节"))
    return events


def fetch_status(n: int) -> list[dict]:
    return [{
        "chip": "沿用上期" if n else "未抓到",
        "chip_cls": "neu" if n else "amb",
        "content": "中央气象台/台风、长江来水",
        "data": f"{n} 条；逐日数值拿不到则精确定性 + N/A",
    }]
