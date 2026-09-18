"""国内快讯、地缘与政策：新华社政治、人民网、新浪财经 7×24、国家能源局、发改委。

按【油/气/煤/电 + 地缘政治 + 宏观金融 + 能源政策】关键词过滤。
每个源独立 try/except，失败降级为空，绝不让整轮崩。
"""
from __future__ import annotations

import re

from .base import html_links, http_get_json, make_event, parse_rss

# 命中任一即收录：能源四品种 + 地缘政治/制裁/通道 + 宏观金融 + 中国能源政策
CN_KEEP = [
    # 能源四品种
    "原油", "石油", "成品油", "Brent", "WTI", "SC原油", "油价",
    "天然气", "LNG", "液化", "管道气", "TTF", "储气", "气价", "燃气",
    "煤", "动力煤", "焦煤", "焦炭", "秦港", "坑口", "煤价",
    "电力", "电价", "现货", "风电", "光伏", "核电", "水电", "火电", "来水",
    "储能", "碳价", "碳市场", "能源", "电网", "发电", "用电", "负荷", "保供",
    # 地缘政治 / 制裁 / 海峡通道
    "中东", "伊朗", "伊拉克", "沙特", "以色列", "加沙", "也门", "胡塞", "叙利亚",
    "卡塔尔", "阿联酋", "科威特", "委内瑞拉", "俄罗斯", "乌克兰", "俄乌", "欧盟",
    "红海", "霍尔木兹", "苏伊士", "海峡", "波斯湾", "OPEC", "欧佩克", "减产", "增产",
    "制裁", "禁运", "出口管制", "断供", "封锁", "护航", "油轮", "袭击", "空袭",
    "导弹", "无人机", "停火", "停战", "冲突", "地缘", "关税",
    # 宏观金融
    "美联储", "加息", "降息", "美元", "通胀", "CPI", "PPI", "PMI", "央行",
    "货币政策", "财政", "衰退", "非农", "利率", "汇率", "人民币",
    # 中国能源/电力政策与主管部门
    "国家能源局", "发改委", "发展改革委", "中电联", "容量电价", "分时电价",
    "电力市场", "现货市场", "辅助服务", "煤电", "机组", "特高压", "接收站",
    "长协", "进口煤", "安监", "环保限产", "能耗",
]

# 纯政策/地缘源（新华社/人民网政治频道）需要命中的词，避免外交礼宾噪音
POLICY_HIT = CN_KEEP


def _keep(title: str) -> bool:
    return any(k in (title or "") for k in CN_KEEP)


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def fetch_news() -> list[dict]:
    events: list[dict] = []
    eid = 0

    # —— 说明：新华社/人民网 politics RSS 实测返回 2022–2025 旧稿（非当日），已弃用，
    # 防止历史旧闻污染推演。中文当日政治/政策以带时间戳的新浪7×24、华尔街见闻及主管部门官网为准。

    # —— 新浪财经 7×24 直播（JSON，实时电报，含油气煤电/地缘/宏观）——
    try:
        j = http_get_json(
            "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=50&zhibo_id=152&tag_id=0&type=0",
            timeout=15)
        lst = (((j or {}).get("result") or {}).get("data") or {}).get("feed", {}).get("list", [])
    except Exception:  # noqa: BLE001
        lst = []
    for x in lst:
        text = _strip_html(x.get("rich_text", ""))
        if not text or not _keep(text):
            continue
        title = text.split("】", 1)[-1][:80] if "】" in text else text[:80]
        eid += 1
        events.append(make_event(
            f"FL{eid:02d}", x.get("create_time") or "—", "新浪7×24",
            title, text[:240], x.get("docurl") or "—", "国内快讯/地缘宏观"))

    # —— 主管部门官网列表页（HTML 抽 <a>；实测可达，抓不到独立降级）——
    html_sources = [
        ("http://www.nea.gov.cn/xwzx/", "国家能源局"),
        ("https://www.ndrc.gov.cn/xwdt/xwfb/", "发改委"),
    ]
    for url, src in html_sources:
        try:
            links = html_links(url, limit=30)
        except Exception:  # noqa: BLE001
            links = []
        for lk in links:
            if not _keep(lk["title"]):
                continue
            eid += 1
            events.append(make_event(
                f"FL{eid:02d}", "—", src, lk["title"], "", lk["url"],
                "中国政策与电力市场规则"))

    return events


def fetch_status(n_kept: int) -> list[dict]:
    if n_kept > 0:
        return [{
            "chip": "已更新", "chip_cls": "neu",
            "content": "新浪7×24、华尔街见闻（能源·地缘·宏观实时电报）+ 能源局/发改委政策",
            "data": f"过滤后 {n_kept} 条",
        }]
    return [{
        "chip": "未抓到", "chip_cls": "amb",
        "content": "国内快讯/地缘/政策源（新浪7×24/华尔街见闻/能源局/发改委）",
        "data": "全部源不可达，按 N/A",
    }]
