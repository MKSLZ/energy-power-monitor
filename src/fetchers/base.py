"""共享 HTTP / 解析工具。

设计原则（硬约束）：
- 每个源独立 try/except，任何失败都降级为 None / N/A，绝不让整轮崩。
- 统一 UA、超时 10s、最多 1 次重试。
- 抓到的 url 原样透传，抓不到就标 N/A，禁止编数。
"""
from __future__ import annotations

import re
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


def http_get_bytes(url: str, *, headers: dict | None = None,
                   timeout: int = TIMEOUT) -> bytes | None:
    """GET 原始字节（交给 BeautifulSoup 自动嗅探 GBK/UTF-8，避免乱码）。失败 None。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    for attempt in range(RETRY + 1):
        try:
            r = get_session().get(url, headers=h, timeout=timeout, allow_redirects=True)
            if r.status_code == 200:
                return r.content
        except Exception:  # noqa: BLE001
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


# ---------------------------------------------------------------------------
# 新闻标题有效性过滤：HTML 首页里大量 <a> 是导航/菜单/栏目/乱码，必须清掉，
# 否则会把 "Net-zero / Governance / English / 煤炭供需 / ç¤ç»­..." 当成事件。
# 黑名单只做“整条等值”，不做子串，避免误杀含“价格/公告”的真实长标题。
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[\u4e00-\u9fff]")
_MOJIBAKE = re.compile(r"[ÃÂâãäåçèéêëìíîïðñòóôõöøùúûüýþÿ]")
_PUNCT_SPACE = re.compile(r"[\s\-_·•|—–:：，,。.！!？?、；;“”\"'‘’（）()【】\[\]《》<>/\\#~*]")

_NAV_EXACT = {
    "home", "about", "aboutus", "contact", "contactus", "login", "logout", "signin",
    "signout", "signup", "register", "subscribe", "newsletter", "menu", "search",
    "more", "readmore", "english", "sitemap", "faq", "help", "careers", "jobs",
    "privacy", "privacypolicy", "cookies", "cookiestatement", "terms", "termsofuse",
    "imprint", "impressum", "accessibility", "dataprotection", "members", "allmembers",
    "meetourteam", "ourdna", "governance", "board", "gieboard", "excom",
    "gteexcom", "gseexcom", "gleexcom", "netzero", "publications", "statistics",
    "maps", "tools", "press", "media", "events", "library", "skip", "skipcontent",
    "cookie", "settings", "legal", "glossary", "methodology", "news", "latest",
    "首页", "主页", "官网", "官方网站", "登录", "登陆", "注册", "登录注册", "退出",
    "关于", "关于我们", "联系", "联系我们", "网站地图", "加入收藏", "设为首页",
    "免责声明", "隐私", "隐私政策", "版权", "版权所有", "中文版", "英文版", "简体",
    "繁体", "繁體", "客户端", "下载", "app下载", "微信", "微博", "搜索", "更多",
    "返回", "返回首页", "会员", "新闻中心", "资讯中心", "信息公开", "信息公开目录",
    "通知公告", "公告", "政策法规", "行业动态", "市场分析", "数据中心", "数据",
    "价格", "行情", "指数", "报告", "专题", "视频", "图片", "直播", "会展", "会议",
    "培训", "煤炭供需", "煤炭运输", "煤炭价格", "电煤", "焦炭", "钢材", "化工",
    "水泥", "供需", "运输", "港口", "坑口", "产品", "产品中心", "解决方案", "服务",
    "业务", "机构", "领导", "职能", "司局", "直属单位", "地方", "国际", "合作",
    "党建", "人事", "招聘", "公示", "征求意见", "互动", "咨询", "投诉", "网站声明",
    "台风", "台风网", "天气预报", "中心概况",
}


def norm_title(t: str) -> str:
    return _PUNCT_SPACE.sub("", (t or "")).lower()


def looks_like_news(title: str) -> bool:
    """判断 <a> 文本是否像一条真实新闻标题，而非导航/栏目/乱码/站名。"""
    t = " ".join((title or "").split())
    if not t:
        return False
    n = norm_title(t)
    if "\ufffd" in t:
        return False
    n_cjk = len(_CJK.findall(t))
    if n_cjk == 0 and len(_MOJIBAKE.findall(t)) >= 2:
        return False
    if n in _NAV_EXACT:
        return False
    if any(k in t for k in ("欢迎访问", "欢迎光临", "版权所有", "ICP备", "ICP证",
                            "京公网安备", "地址：", "邮编：", "电话：")):
        return False
    if "官网" in t and n_cjk <= 12:
        return False
    if n_cjk >= 4:
        return n_cjk >= 8
    words = [w for w in re.split(r"\s+", t) if re.search(r"[A-Za-z]", w)]
    letters = sum(len(re.findall(r"[A-Za-z]", w)) for w in words)
    return len(words) >= 4 and letters >= 24


# 能源价格相关性：真实标题里仍有与油气煤电无关的内容（个股、房车、氦气、纪检声明、
# 水电站大坝注册复函模板、月度名单、下载宣传页…）。含强相关词一律保留；否则命中
# 高确定性无关词/模板才剔除，宁漏勿滥。
_REL_KWS = (
    "油", "原油", "石油", "天然气", "lng", "燃气", "气价", "煤", "焦煤", "焦炭",
    "电", "电价", "电网", "发电", "用电", "供电", "电源", "核电", "水电", "火电",
    "opec", "库存", "减产", "增产", "日耗", "保供", "来水", "蓄水", "高温", "寒潮",
    "冷暖", "台风", "负荷", "管道", "关税", "美联储", "加息", "降息", "美元", "通胀",
    "cpi", "ppi", "伊朗", "俄", "制裁", "红海", "霍尔木兹", "航运", "油船", "运价",
    "储气", "采暖", "供暖", "风光", "风电", "光伏", "新能源", "储能", "碳价", "碳市场",
    "容量电价", "现货", "油气", "能化", "大宗", "商品", "宏观", "经济", "甲烷",
    "biomethane", "bio-lng", "biolng", "gas", "oil", "coal", "power", "energy",
    "brent", "wti", "ttf", "jkm", "refinery", "lng船", "接收站", "门站",
    # 天气 / 水文 / 负荷（电力相关）
    "气象", "天气", "台风", "高温", "寒潮", "热浪", "暴雨", "降水", "降雨", "雨雪",
    "来水", "蓄水", "枯水", "丰水", "水电", "水利", "水文", "水量", "水库", "流域",
    "流量", "调水", "长江", "黄河", "干旱", "洪涝", "渍涝", "山洪", "度日", "气旋",
    "预警", "预报", "落区", "汛",
    # 英文气链补充
    "methane", "storage", "injection", "pipeline", "refinery", "opec+",
)
# 标题里常自带来源机构名（含“天然气/煤/电”等字），判相关性前先剥除，避免假阳性
_SOURCE_NAMES = (
    "上海石油天然气交易中心", "中国煤炭市场网", "秦皇岛煤炭网", "秦皇岛港股份有限公司",
    "秦皇岛港", "国家发展和改革委员会", "国家发展改革委", "国家能源局", "中国气象局",
    "中央气象台", "长江水利委员会", "长江水利委", "长江委", "中国电力企业联合会",
    "中电联", "证券时报", "新浪财经", "财联社", "上海石油天然气", "天然气交易中心",
    "中国煤炭", "煤炭市场网", "CCTD", "GIE", "Nord Pool", "EPEX", "Nordnet",
)
_IRREL_KWS = (
    "涨停", "跌停", "封板", "减持", "增持", "回购", "个股", "龙头", "房车", "烟花爆竹",
    "氦气", "纪检", "监督举报", "推荐信", "电信诈骗", "严正声明", "科普", "五一", "劳动节",
    "金砖", "基础研究座谈会", "先进制造业", "电缆",
)
_HARD_DROP = (
    "大坝安全注册", "注册登记证", "新增建档立卡", "首台（套）", "首台(套)",
    "许可证注销", "承装（修", "承装(修",
)
_HARD_DROP_EN = ("download or order", "click here", "register now", "subscribe here")


def energy_relevant(title: str) -> bool:
    """剥掉来源机构名后走白名单：必须命中油/气/煤/电/天气水文等强相关词；
    高确定性无关词（个股/诈骗/声明/氦气等）即便含相关词也剔除。"""
    t = title or ""
    if t.startswith(("习近平", "李强")):
        return False
    tl = t.lower()
    if any(k in t for k in _HARD_DROP) or any(k in tl for k in _HARD_DROP_EN):
        return False
    body = t
    for nm in _SOURCE_NAMES:  # 先长后短，去掉自带“天然气/煤/电”的机构名
        body = body.replace(nm, "")
    b = body.lower()
    if any(k in body for k in _IRREL_KWS) or any(k in b for k in _IRREL_KWS):
        return False
    return any(k in b for k in _REL_KWS)


def event_relevance_score(title: str) -> int:
    """粗略相关度打分，用于喂 LLM 前排序（分越高越靠前）。"""
    tl = (title or "").lower()
    score = 0
    strong = ("原油", "石油", "天然气", "lng", "煤", "焦煤", "电价", "电力", "现货",
              "opec", "brent", "wti", "ttf", "油价", "气价", "保供", "库存", "减产",
              "日耗", "管道气", "接收站", "门站", "广东", "南方区域", "来水", "高温",
              "伊朗", "制裁", "霍尔木兹", "红海", "油船", "储气", "采暖")
    for k in strong:
        if k in tl:
            score += 2
    for k in ("电", "气", "油", "能源", "发电", "新能源", "碳", "关税", "美元",
              "美联储", "加息", "航运", "运价", "负荷", "台风", "寒潮"):
        if k in tl:
            score += 1
    return score


def select_events(events: list[dict], cap: int = 30, per_source: int = 8) -> list[dict]:
    """每源限量（防单源刷屏）+ 相关度稳定排序，取前 cap 条喂 LLM。"""
    cnt: dict[str, int] = {}
    picked = []
    for e in events:  # 保持原序做每源限量
        s = e.get("source", "")
        if cnt.get(s, 0) >= per_source:
            continue
        cnt[s] = cnt.get(s, 0) + 1
        picked.append(e)
    picked.sort(key=lambda e: event_relevance_score(e.get("title", "")), reverse=True)
    return picked[:cap]


def clean_events(events: list[dict]) -> list[dict]:
    """跨源统一清洗（新闻性 + 能源相关性）+ 去重（按规范化标题），保持原顺序。"""
    seen: set[str] = set()
    out: list[dict] = []
    for e in events:
        title = (e.get("title") or "").strip()
        if not (looks_like_news(title) and energy_relevant(title)):
            continue
        key = norm_title(title)
        if key in seen:
            continue
        seen.add(key)
        e = dict(e)
        e["title"] = title
        out.append(e)
    return out


def html_links(url: str, *, encoding: str | None = None, limit: int = 30) -> list[dict]:
    """抓 HTML 列表页抽 <a>。用原始 bytes 让 BeautifulSoup 自动嗅探编码（修 GBK 乱码），
    并用 looks_like_news/energy_relevant 过滤导航/栏目/乱码/无关内容。失败 []。"""
    from bs4 import BeautifulSoup
    if encoding:
        soup_arg = http_get(url, encoding=encoding)
    else:
        soup_arg = http_get_bytes(url)
    if not soup_arg:
        return []
    try:
        soup = BeautifulSoup(soup_arg, "lxml")
    except Exception:  # noqa: BLE001
        return []
    out = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        if not (looks_like_news(text) and energy_relevant(text)):
            continue
        href = a["href"].strip()
        if href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(url, href)
        if not href.startswith("http"):
            continue
        key = norm_title(text)
        if key in seen:
            continue
        seen.add(key)
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
