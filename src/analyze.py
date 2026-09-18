"""LLM 结构化分析 + 抓取层 → 契约 report dict 的装配/降级。

- run_llm(): OpenAI 兼容协议，强约束"仅依据给定事实、缺数据写 N/A、不得虚构"。
- assemble_report(): 把抓取层原始数据（+可选 LLM 输出）映射成契约 report dict。
  无 LLM_API_KEY 或解析失败时走"数据速览版"，保证任何区块不空白。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# 常量：固定栏目名 / 配色映射
# ---------------------------------------------------------------------------
PRODS = ["石油", "天然气", "煤炭", "电力"]
PROD_KEYS = ["oil", "gas", "coal", "power"]

NEWS_CATEGORIES = ["地缘政治", "宏观金融", "供需库存", "天气季节", "中国政策", "替代能源"]

FACTOR_NAMES = ["地缘政治与制裁", "宏观金融", "供需库存",
                "天气季节", "中国政策与电力规则", "替代能源与基础设施"]
RADAR_NAMES = ["地缘政治", "宏观金融", "供需库存", "天气季节", "中国政策", "替代能源"]

# dir(up/down/flat) -> 展示符号与类
DIR_ARROW = {"up": "▲", "down": "▼", "flat": "■"}
DIR_LABEL = {"up": "看多", "down": "看空", "flat": "中性"}
CHG_CLS = {"up": "up", "down": "down", "flat": "flat", "na": "na"}


def _norm_dir(v: str | None) -> str:
    if not v:
        return "flat"
    v = str(v).strip()
    if v.startswith("▲") or v in ("up", "多", "bull", "long"):
        return "up"
    if v.startswith("▼") or v in ("down", "空", "bear", "short"):
        return "down"
    return "flat"


def _dir_label(d: str, strength: str = "") -> str:
    arrow = DIR_ARROW.get(d, "■")
    base = DIR_LABEL.get(d, "中性")
    if strength and d != "flat":
        return f"{arrow} {base}"
    if d == "flat":
        return f"{arrow} {base}"
    return f"{arrow} {base}"


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是能源电力市场事件影响推演引擎。严格规则：
1. 仅依据用户提供的【抓取事实】（标题/时间/来源/原始价格）推演，价格、链接、时间一律使用原文，禁止虚构任何价格/链接/数字。
2. 缺数据一律写 "N/A"，不要编造。
3. 只做定性结构化：每个事件给出对 石油oil/天然气gas/煤炭coal/电力power 四品种的方向(▲/▼/■)、强度、时滞、置信度、是否priced_in、一句传导逻辑。
4. 输出必须是且仅是一个 JSON 对象，不要任何 markdown 代码块或解释。
JSON 结构：
{
 "core_points":[{"tag":"石油|地缘|煤炭|天然气|宏观|政策","text":"一句话","net":{"oil":{"dir":"up|down|flat","label":"▲弱多"},"gas":{...},"coal":{...},"power":{...}}}],
 "events":[{"title":"...","time":"...","source":"...","url":["https://..."],"nature":"确认|突发|数据公布","impacts":{"oil":{"dir":"▲/▼/■","strength":"弱/中/强","lag":"...","confidence":"...","priced_in":"是/否/部分","logic":"..."},"gas":{...},"coal":{...},"power":{...}},"chain":"传导链：...","analogy":"历史类比：...","trigger":"强化触发：...","falsify":"证伪：..."}],
 "heatmap_events_short":["EV1 简称",...],
 "factor_board":[{"name":"地缘政治与制裁","chip":"偏多 · 强","pos":"left|right|center","width":"41%","desc":"..."}],
 "radar":[2.5,-1.3,0.2,-0.8,0.6,-0.2],
 "summary_rows":[{"event":"EVx ...","oil":{"txt":"▼中强","cls":"up|down|flat"},"gas":{...},"coal":{...},"power":{...},"note":"..."}],
 "scenarios":[{"name":"石油 · Brent","current":"当前 $97.92","rows":[{"label":"基准 50%","label_cls":"n|b|s","range":"...","range_cls":"up|down|","trigger":"..."}]}],
 "catalysts":[{"date":"9/18–19","body":"...","sub":"..."}],
 "main_risks":["...","..."],
 "source_status":[{"chip":"已更新|沿用上期|未抓到","chip_cls":"bull|neu|amb","content":"...","data":"..."}]
}"""


def run_llm(fetched: dict) -> dict | None:
    """调用 OpenAI 兼容接口。无 key / 失败 / 解析失败 -> None（main 走速览版）。"""
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    if not base or not key or not model:
        return None

    # 把抓取事实喂给 LLM（事件标题/来源/时间/链接，行情名与原始值）
    facts = {
        "events": [
            {"time": e["time"], "source": e["source"], "title": e["title"],
             "url": e["url"], "category": e["category"]}
            for e in fetched.get("events", [])[:60]
        ],
        "quotes": [
            {"name": q["name"], "price": q["price"], "unit": q["unit"],
             "day_chg": q["day_chg"], "src_time": q["src_time"], "status": q["status"]}
            for q in fetched.get("quotes", [])
        ],
    }
    user = "【抓取事实】如下，请按系统给定 JSON 结构输出（价格/链接/时间必须原样，缺则 N/A）：\n" + json.dumps(facts, ensure_ascii=False)

    import requests
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                            {"role": "user", "content": user}],
               "temperature": 0.2, "response_format": {"type": "json_object"}}

    for attempt in range(2):  # 解析失败重试 1 次
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=60)
            if r.status_code != 200:
                return None
            content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            if not m:
                continue
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            if attempt == 1:
                return None
    return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _fmt_price(price, unit: str) -> str:
    if price is None:
        return "N/A"
    try:
        return f"{float(price):g} {unit}".strip()
    except (ValueError, TypeError):
        return str(price)


def _chg_cell(v) -> tuple[str, str]:
    """day_chg(百分比数值) -> (展示串, cls)。"""
    if v is None:
        return "N/A", "na"
    try:
        f = float(v)
    except (ValueError, TypeError):
        return str(v), "na"
    s = f"{f:+.2f}%"
    return s, "up" if f > 0 else ("down" if f < 0 else "flat")


def _domain(url: str) -> str:
    m = re.sub(r"^https?://", "", url or "")
    return m.split("/")[0] if m else "source"


def _find_quote(quotes: list[dict], *keywords: str) -> dict | None:
    for q in quotes:
        if any(k in q["name"] for k in keywords):
            return q
    return None


# ---------------------------------------------------------------------------
# 装配：行情分组表 / 图表
# ---------------------------------------------------------------------------
def build_price_groups(quotes: list[dict]) -> list[dict]:
    groups = [
        ("石油", ["Brent", "WTI", "SC", "纽约原油", "布伦特"]),
        ("天然气·LNG", ["TTF", "JKM", "HH", "天然气", "LNG", "SHPGX"]),
        ("煤炭", ["秦港", "Q5500", "纽卡斯尔", "动力煤", "焦煤"]),
        ("电力（中国现货）", ["广东", "EPEX", "山东", "日前", "现货"]),
    ]
    out = []
    for gname, kws in groups:
        rows = []
        for q in quotes:
            if not any(k in q["name"] for k in kws):
                continue
            day_txt, day_cls = _chg_cell(q["day_chg"])
            week_txt, week_cls = _chg_cell(q["week_chg"])
            link = {"text": _domain(q["url"]), "url": q["url"]} if q["url"] else None
            rows.append({
                "contract": q["name"],
                "price": _fmt_price(q["price"], q["unit"]),
                "price_cls": "num",
                "day": day_txt if q["status"] == "ok" else "N/A",
                "day_cls": day_cls if q["status"] == "ok" else "na",
                "week": week_txt,
                "week_cls": week_cls,
                "src_time": q["src_time"],
                "link": link,
            })
        if not rows:
            rows.append({"contract": gname + " 代表性品种", "price": "N/A", "price_cls": "na",
                         "day": "N/A", "day_cls": "na", "week": "N/A", "week_cls": "na",
                         "src_time": "本期未抓到", "link": None})
        out.append({"name": gname, "rows": rows})
    return out


def build_chart_change(quotes: list[dict]) -> dict:
    labels = ["Brent亚盘", "WTI亚盘", "TTF", "HH", "秦港Q5500", "纽煤Dec"]
    want = [["布伦特", "Brent"], ["纽约原油", "WTI"], ["TTF"], ["HH", "天然气"], ["秦港", "Q5500"], ["纽卡斯尔", "纽煤"]]
    day, week = [], []
    for kws in want:
        q = _find_quote(quotes, *kws)
        if q and q["status"] == "ok" and q["day_chg"] is not None:
            day.append(q["day_chg"])
        else:
            day.append(None)
        week.append(q["week_chg"] if q else None)
    return {"labels": labels, "day": day, "week": week}


# ---------------------------------------------------------------------------
# 装配：新闻六分类
# ---------------------------------------------------------------------------
def _classify(title: str, category: str) -> str:
    if category == "天气季节":
        return "天气季节"
    if any(k in title for k in ["伊朗", "俄罗斯", "制裁", "霍尔木兹", "海峡", "袭击", "战争", "导弹", "以色列", "乌克兰", "袭击"]):
        return "地缘政治"
    if any(k in title for k in ["加息", "美联储", "央行", "CPI", "PPI", "DXY", "美元", "降息", "利率", "美股", "宏观", "GDP"]):
        return "宏观金融"
    if any(k in title for k in ["库存", "产量", "累库", "去库", "出口", "进口", "OPEC", "EIA", "IEA", "供需", "开工", "日耗"]):
        return "供需库存"
    if any(k in title for k in ["能源局", "发改委", "政策", "通知", "电价", "市场化", "碳", "容量电价", "核准", "规划", "现货"]):
        return "中国政策"
    if any(k in title for k in ["水电", "核电", "光伏", "风电", "储能", "特高压", "替代", "新能源", "来水", "蓄水"]):
        return "替代能源"
    if any(k in title for k in ["天气", "台风", "高温", "寒潮", "降水", "来水"]):
        return "天气季节"
    return "供需库存"


def build_news_categories(events: list[dict]) -> list[dict]:
    buckets = {c: [] for c in NEWS_CATEGORIES}
    for e in events:
        cat = _classify(e["title"], e.get("category", ""))
        tags = []
        for k, cls in [("油", "bull"), ("气", "bull"), ("煤", "bear"), ("电", "neu")]:
            if k in e["title"]:
                tags.append({"label": k, "cls": cls})
        buckets[cat].append({
            "time": e["time"] or "—",
            "source": e["source"],
            "text": e["title"],
            "links": [{"text": e["source"], "url": e["url"]}] if e["url"] else [],
            "tags": tags or [{"label": "关注", "cls": "neu"}],
        })
    out = []
    for c in NEWS_CATEGORIES:
        items = buckets[c][:8]
        out.append({"name": c, "count": len(buckets[c]), "items": items})
    return out


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def build_snapshot(quotes: list[dict], dt: datetime, event_line: str) -> dict:
    def chg_up(v):
        if v is None:
            return None
        try:
            f = float(v)
        except (ValueError, TypeError):
            return None
        return True if f > 0 else (False if f < 0 else None)

    brent = _find_quote(quotes, "布伦特", "Brent")
    ttf = _find_quote(quotes, "TTF")
    hh = _find_quote(quotes, "HH", "天然气")
    coal = _find_quote(quotes, "秦港", "Q5500")
    power = _find_quote(quotes, "广东", "日前")

    oil_txt = f"${brent['price']:g}" if brent and brent["price"] is not None else "N/A"
    gas_q = ttf if (ttf and ttf["price"] is not None) else hh
    gas_txt = (f"{gas_q['price']:g} €/MWh" if gas_q and "TTF" in gas_q["name"]
               else (f"${gas_q['price']:g}/MMBtu" if gas_q and gas_q["price"] is not None else "N/A"))
    coal_txt = f"{coal['price']:g} 元/吨" if coal and coal["price"] is not None else "N/A"
    power_txt = f"{power['price']:g} 元/MWh" if power and power["price"] is not None else "N/A"

    def pct(q):
        return f"{q['day_chg']:+.2f}%" if q and q["day_chg"] is not None else "N/A"

    return {
        "time": dt.strftime("%Y-%m-%d %H:%M"),
        "oil": oil_txt, "oil_chg": pct(brent), "oil_up": chg_up(brent["day_chg"] if brent else None),
        "gas": gas_txt, "gas_chg": pct(gas_q), "gas_up": chg_up(gas_q["day_chg"] if gas_q else None),
        "coal": coal_txt, "coal_chg": pct(coal), "coal_up": chg_up(coal["day_chg"] if coal else None),
        "power": power_txt, "power_chg": pct(power), "power_up": chg_up(power["day_chg"] if power else None),
        "dir": "速览版：以实际涨跌为准",
        "event": event_line or "本期快照",
    }


# ---------------------------------------------------------------------------
# LLM 输出 -> 契约 events / heatmap / factors ...
# ---------------------------------------------------------------------------
def _llm_events_to_contract(llm_events: list[dict]) -> list[dict]:
    out = []
    for i, ev in enumerate(llm_events, 1):
        impacts = []
        for key, prod in zip(PROD_KEYS, PRODS):
            imp = (ev.get("impacts") or {}).get(key) or {}
            d = _norm_dir(imp.get("dir"))
            impacts.append({
                "prod": prod,
                "dir": d,
                "dir_label": _dir_label(d, imp.get("strength", "")),
                "strength": imp.get("strength", "—"),
                "lag": imp.get("lag", "—"),
                "confidence": imp.get("confidence", "—"),
                "priced_in": imp.get("priced_in", "—"),
                "logic": imp.get("logic", "—"),
            })
        urls = ev.get("url") or []
        links = [{"text": ev.get("source", "来源"), "url": u} for u in urls if u]
        nature = ev.get("nature", "")
        chips = [nature] if nature else []
        out.append({
            "id": f"EV{i}",
            "title": f"EV{i} · {ev.get('title', '—')}",
            "time": ev.get("time", "—"),
            "source": ev.get("source", "—"),
            "chips": chips,
            "links": links[:3],
            "impacts": impacts,
            "chain": ev.get("chain", "—"),
            "analogy": ev.get("analogy", "—"),
            "trigger": ev.get("trigger", "—"),
            "falsify": ev.get("falsify", "—"),
        })
    return out


def _degraded_events(raw_events: list[dict]) -> list[dict]:
    """无 LLM：用原始抓取事件造精简事件卡（只填石油/中性行）。"""
    out = []
    for i, e in enumerate(raw_events[:8], 1):
        impacts = []
        for prod in PRODS:
            impacts.append({"prod": prod, "dir": "flat", "dir_label": "■ 中性",
                            "strength": "—", "lag": "—", "confidence": "—",
                            "priced_in": "—", "logic": "（速览版：未做 LLM 推演）"})
        out.append({
            "id": f"EV{i}", "title": f"EV{i} · {e['title']}",
            "time": e["time"], "source": e["source"],
            "chips": ["数据速览版"],
            "links": [{"text": e["source"], "url": e["url"]}] if e["url"] else [],
            "impacts": impacts,
            "chain": e.get("summary", "—") or "（速览版：仅列事件，未推演传导链）",
            "analogy": "—", "trigger": "—", "falsify": "—",
        })
    if not out:
        out.append({"id": "EV1", "title": "EV1 · 本期未抓取到事件", "time": "—",
                    "source": "—", "chips": ["速览版"], "links": [],
                    "impacts": [{"prod": p, "dir": "flat", "dir_label": "■ 中性",
                                 "strength": "—", "lag": "—", "confidence": "—",
                                 "priced_in": "—", "logic": "—"} for p in PRODS],
                    "chain": "—", "analogy": "—", "trigger": "—", "falsify": "—"})
    return out


def _degraded_heatmap(events: list[dict]) -> dict:
    names = [f"{e['id']} {re.sub(r'^EV\\d+ · ', '', e['title'])[:12]}" for e in events]
    n = len(events)
    return {
        "events": names, "prods": PRODS,
        "matrix": [[0, 0, 0, 0] for _ in range(n)],
        "logic": [["—", "—", "—", "—"] for _ in range(n)],
    }


# ---------------------------------------------------------------------------
# 总装配
# ---------------------------------------------------------------------------
def assemble_report(fetched: dict, llm: dict | None, history: list[dict],
                    now: datetime, *, ai_note: str | None = None) -> dict:
    quotes = fetched.get("quotes", [])
    raw_events = fetched.get("events", [])
    fetched_status = fetched.get("source_status", [])

    date_s = now.strftime("%Y-%m-%d")
    cutoff_s = now.strftime("%Y-%m-%d %H:%M")
    window_s = f"{(now.replace(hour=15, minute=30)).strftime('%m-%d %H:%M')} — {now.strftime('%m-%d %H:%M')}"

    # ---- meta ----
    badges = [{"text": "本期为自动化流水线生成", "cls": "neutral"}]
    if ai_note:
        badges.append({"text": "数据速览版（无 AI 推演）", "cls": "warn"})
    meta = {
        "title": "能源电力市场监控日报",
        "subtitle": "Energy & Power Market Daily Monitor · 事件驱动 · 覆盖 石油/天然气LNG/煤炭/电力",
        "date": date_s, "cutoff": cutoff_s, "window": window_s,
        "next_update": "约每 3 小时",
        "badges": badges, "ai_note": ai_note,
    }

    has_llm = isinstance(llm, dict) and bool(llm)

    # ---- core_points ----
    if has_llm and llm.get("core_points"):
        core_points = llm["core_points"]
    else:
        brent = _find_quote(quotes, "布伦特", "Brent")
        core_points = [{
            "tag": "行情速览",
            "text": f"Brent {_fmt_price(brent['price'], brent['unit']) if brent else 'N/A'}；"
                    f"本期共抓取 {len(raw_events)} 条事件、{len(quotes)} 条行情。",
            "net": {p: {"dir": "flat", "label": "■"} for p in PROD_KEYS},
        }]

    # ---- events / heatmap ----
    if has_llm and llm.get("events"):
        events = _llm_events_to_contract(llm["events"])
        short = llm.get("heatmap_events_short") or [f"{e['id']} {e['title'][:10]}" for e in events]
        # 若 LLM 没给 matrix，用 events 的 impacts 方向折算
        matrix = []
        logic = []
        for e in events:
            row, lrow = [], []
            for imp in e["impacts"]:
                row.append({"up": 2, "down": -2, "flat": 0}[imp["dir"]])
                lrow.append(imp["logic"][:18])
            matrix.append(row)
            logic.append(lrow)
        heatmap = {"events": short[: len(events)], "prods": PRODS,
                   "matrix": matrix, "logic": logic}
    else:
        events = _degraded_events(raw_events)
        heatmap = _degraded_heatmap(events)

    # ---- source_status：抓取层 + LLM 追加 ----
    source_status = list(fetched_status)
    if has_llm and llm.get("source_status"):
        source_status.extend(llm["source_status"])

    # ---- price groups / charts ----
    price_groups = build_price_groups(quotes)
    chart_change = build_chart_change(quotes)
    if has_llm and llm.get("chart_brent"):
        chart_brent = llm["chart_brent"]
    else:
        chart_brent = {"labels": ["上期", "本期"], "data": [None, None],
                       "ymin": None, "ymax": None, "marks": []}

    # ---- snapshots（history 已是新快照在前）----
    snapshots = history

    # ---- news categories ----
    news_categories = build_news_categories(raw_events)

    # ---- factors / radar ----
    if has_llm and llm.get("factor_board"):
        factors = []
        for i, f in enumerate(llm["factor_board"][:6]):
            factors.append({
                "name": f.get("name", FACTOR_NAMES[i] if i < len(FACTOR_NAMES) else f"因子{i+1}"),
                "chip": f.get("chip", "—"),
                "chip_cls": "bull" if "多" in f.get("chip", "") else ("bear" if "空" in f.get("chip", "") else "neu"),
                "desc": f.get("desc", "—"),
                "pos": f.get("pos", "center"),
                "width": f.get("width", "3%"),
                "color": f.get("color", "var(--muted)"),
            })
        radar = {"names": RADAR_NAMES,
                 "values": (llm.get("radar") or [0, 0, 0, 0, 0, 0])[:6]}
    else:
        factors = [{"name": n, "chip": "中性", "chip_cls": "neu", "desc": "速览版：未做因子量化",
                    "pos": "center", "width": "3%", "color": "var(--muted)"} for n in FACTOR_NAMES]
        radar = {"names": RADAR_NAMES, "values": [0, 0, 0, 0, 0, 0]}

    # ---- summary_rows ----
    if has_llm and llm.get("summary_rows"):
        summary_rows = llm["summary_rows"]
    else:
        summary_rows = [{"event": e["title"],
                         "oil": {"txt": "■", "cls": "flat"}, "gas": {"txt": "■", "cls": "flat"},
                         "coal": {"txt": "■", "cls": "flat"}, "power": {"txt": "■", "cls": "flat"},
                         "note": "速览版：未推演"} for e in events[:8]]

    # ---- scenarios ----
    if has_llm and llm.get("scenarios"):
        scenarios = llm["scenarios"]
    else:
        scenarios = [
            {"name": "石油 · Brent", "current": _fmt_price((_find_quote(quotes, "布伦特") or {}).get("price"), "$/桶"),
             "rows": [{"label": "基准 50%", "label_cls": "n", "range": "N/A", "range_cls": "", "trigger": "速览版未做情景推演"},
                      {"label": "乐观 25%", "label_cls": "b", "range": "N/A", "range_cls": "up", "trigger": "—"},
                      {"label": "悲观 25%", "label_cls": "s", "range": "N/A", "range_cls": "down", "trigger": "—"}]},
        ] * 4
        scenarios = scenarios  # noqa: keep 4 cards placeholder

    # ---- catalysts / main_risks ----
    catalysts = (llm.get("catalysts") if has_llm else None) or [
        {"date": "持续", "body": "油气煤电主要事件与政策跟踪。", "sub": "速览版"}]
    main_risks = (llm.get("main_risks") if has_llm else None) or [
        "速览版：未配置 LLM_API_KEY，影响推演不可用；价格以抓取层原始数据为准。",
        "盘中不同数据源口径可能存在差异；政策与突发事件可能导致快速偏离。"]

    # ---- sources_rows / appendix（静态巡检入口）----
    sources_rows = [
        {"name": "腾讯全球行情", "monitors": "Brent/WTI/HH/贵金属实时", "freq": "实时",
         "links": [{"text": "qt.gtimg.cn", "url": "https://qt.gtimg.cn/"}]},
        {"name": "新华社 / 路透", "monitors": "地缘与能源突发", "freq": "实时",
         "links": [{"text": "xinhuanet", "url": "http://www.xinhuanet.com/"},
                   {"text": "reuters", "url": "https://www.reuters.com/"}]},
        {"name": "财联社 / 证券时报", "monitors": "油气煤电快讯", "freq": "实时",
         "links": [{"text": "cls.cn", "url": "https://www.cls.cn/"},
                   {"text": "stcn", "url": "https://www.stcn.com/"}]},
        {"name": "CCTD / 秦皇岛煤炭网", "monitors": "煤价/港口库存/日耗", "freq": "日度",
         "links": [{"text": "cctd", "url": "https://www.cctd.com.cn/"},
                   {"text": "cqcoal", "url": "http://www.cqcoal.com/"}]},
        {"name": "GIE AGSI", "monitors": "欧洲储气充填率", "freq": "日度",
         "links": [{"text": "agsi.gie.eu", "url": "https://agsi.gie.eu/"}]},
        {"name": "EPEX / Nord Pool", "monitors": "欧洲电力现货", "freq": "日度",
         "links": [{"text": "epexspot", "url": "https://www.epexspot.com/"},
                   {"text": "nordpool", "url": "https://www.nordpoolgroup.com/"}]},
        {"name": "OPEC / IEA / EIA", "monitors": "产量政策/月报/库存", "freq": "周/月度",
         "links": [{"text": "opec", "url": "https://www.opec.org/"},
                   {"text": "iea", "url": "https://www.iea.org/"},
                   {"text": "eia", "url": "https://www.eia.gov/"}]},
        {"name": "国家能源局 / 发改委 / 中电联", "monitors": "电力政策/装机/用电", "freq": "月度/临时",
         "links": [{"text": "nea", "url": "http://www.nea.gov.cn/"},
                   {"text": "ndrc", "url": "https://www.ndrc.gov.cn/"},
                   {"text": "cec", "url": "https://www.cec.org.cn/"}]},
        {"name": "广东电力交易中心", "monitors": "广东现货出清价", "freq": "日度",
         "links": [{"text": "pm.gd.csg.cn", "url": "https://pm.gd.csg.cn/portal/"}]},
        {"name": "上海石油天然气交易中心", "monitors": "SHPGX 出厂价", "freq": "日度",
         "links": [{"text": "shpgx.com", "url": "http://www.shpgx.com/"}]},
    ]
    appendix = [
        "<b>交易所行情：</b>ICE Brent/TTF/纽煤、NYMEX WTI/HH、INE SC、EEX 德国电力、广东/山东电力交易中心",
        "<b>库存产量：</b>EIA 周度原油与天然气库存、API、IEA/OPEC 月报",
        "<b>欧洲气：</b>AGSI、ICIS/S&P 普氏 JKM、ARA 煤价",
        "<b>宏观央行：</b>美联储 FOMC、BLS CPI/PPI、DXY、VIX",
        "<b>国内能源价：</b>秦港 Q5500、BSPI、SHPGX",
        "<b>天气水文：</b>中央气象台、NOAA、长江水利委",
        "<b>免责声明：</b>本报告由监控系统自动汇总公开信息生成，仅供研究参考，不构成投资建议。",
    ]

    return {
        "meta": meta, "core_points": core_points, "source_status": source_status,
        "events": events, "heatmap": heatmap, "price_groups": price_groups,
        "chart_change": chart_change, "chart_brent": chart_brent, "snapshots": snapshots,
        "news_categories": news_categories, "factors": factors, "radar": radar,
        "summary_rows": summary_rows, "scenarios": scenarios, "catalysts": catalysts,
        "main_risks": main_risks, "sources_rows": sources_rows, "appendix": appendix,
    }
