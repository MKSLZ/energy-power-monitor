"""LLM 结构化分析 + 抓取层 → 契约 report dict 的装配/降级。

- run_llm(): OpenAI 兼容协议，强约束"仅依据给定事实、缺数据写 N/A、不得虚构"。
- assemble_report(): 把抓取层原始数据（+可选 LLM 输出）映射成契约 report dict。
  无 LLM_API_KEY 或解析失败时走"数据速览版"，保证任何区块不空白。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# 常量：固定四品种（内部英文键恒为 oil/gas/coal/power，展示名国内化）
# ---------------------------------------------------------------------------
# 内部键 -> 国内展示名。LLM impacts / net / summary_rows 一律按英文键取，再映射到中文名。
KEY2NAME = {"oil": "国内原油", "gas": "国内天然气", "coal": "国内煤炭", "power": "国内电力"}
PROD_KEYS = ["oil", "gas", "coal", "power"]
PRODS = [KEY2NAME[k] for k in PROD_KEYS]  # 热力图四列 / 事件卡品种标签 / 行情分组的展示名

# 口径定义（同时写进 SYSTEM_PROMPT）：
#   国内原油(oil) = INE 上海原油 SC 主力 + 成品油/化工成本；Brent/WTI 仅作外盘驱动，
#                  经进口到岸平价 / USDCNY / 运费传导到 SC。
#   国内天然气(gas) = SHPGX LNG 出厂与接收站现货、LNG 槽批、管道气门站价；
#                  JKM/TTF 仅经进口成本 / 汇率 / 海运费 / 冬储 / 城燃需求传导。
#   国内煤炭(coal) = 秦皇岛 Q5500 动力煤、港口/坑口、焦煤焦炭、电厂日耗与港口库存、
#                  进口煤内外价差、保供限产安监。
#   国内电力(power) = 广东及南方区域现货日前/实时为主 + 其他现货省；
#                  核心“煤价→度电燃料成本→现货电价”，叠加来水水电/风光出力/
#                  气温负荷/工业需求/容量电价；EPEX/EEX 仅国际参照。

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
SYSTEM_PROMPT = """你是【国内】能源电力市场事件影响推演引擎，分析结论聚焦国内四品种。严格规则：
0. 口径（务必遵守）：
   - 国内原油(oil)=INE 上海原油 SC 主力 + 成品油/化工成本；Brent/WTI 仅作外盘驱动，经进口到岸平价/USDCNY/运费传导到 SC。
   - 国内天然气(gas)=SHPGX LNG 出厂与接收站现货、LNG 槽批、管道气门站价；JKM/TTF 仅经进口成本/汇率/海运费/冬储/城燃需求传导。
   - 国内煤炭(coal)=秦皇岛 Q5500 动力煤、港口/坑口、焦煤焦炭、电厂日耗与港口库存、进口煤内外价差、保供限产安监。
   - 国内电力(power)=广东及南方区域现货日前/实时为主+其他现货省；核心“煤价→度电燃料成本→现货电价”，叠加来水水电/风光出力/气温负荷/工业需求/容量电价；EPEX/EEX 仅国际参照。
1. 仅依据用户提供的【抓取事实】（标题/时间/来源/原始价格）推演，价格、链接、时间一律使用原文，禁止虚构任何价格/链接/数字。
2. 缺数据一律写 "N/A"，不要编造。严格区分事实/报道/推测。
3. 每个事件对【国内四品种】给出：方向(▲/▼/■)、强度(弱/中/强)、时滞、置信度、是否priced_in(是/否/部分)、一句“对国内品种”的传导逻辑。impacts 的键固定为 oil/gas/coal/power，禁止用中文品种名做键。
4. 传导必须落到国内，示例范式：
   - 中东冲突→Brent 溢价→进口到岸成本+汇率→SC↑；
   - 暖冬高库存→JKM/TTF↓→中国 LNG 进口成本↓→SHPGX/门站↓；
   - OPEC 减产→油↑→油气替代→LNG 与煤电经济性变化；
   - 来水偏枯/高温→火电日耗↑→Q5500↑→广东现货电价↑；
   - 保供增产/安监放松→煤价↓→电价成本↓。
5. 历史类比优先国内：2021 全国电荒拉闸限电、2021 煤价暴涨与电价浮动、2022 俄乌致进口能源/LNG 成本抬升、OPEC 意外减产、极端高温限电拉动动力煤。
6. 输出必须是且仅是一个 JSON 对象，不要任何 markdown 代码块或解释。
JSON 结构（与现有契约一致，仅 impacts 键固定 oil/gas/coal/power）：
{
 "core_points":[{"tag":"国内原油|地缘|国内煤炭|国内天然气|宏观|政策","text":"一句话","net":{"oil":{"dir":"up|down|flat","label":"▲弱多"},"gas":{...},"coal":{...},"power":{...}}}],
 "events":[{"title":"...","time":"...","source":"...","url":["https://..."],"nature":"确认|突发|数据公布","impacts":{"oil":{"dir":"▲/▼/■","strength":"弱/中/强","lag":"...","confidence":"...","priced_in":"是/否/部分","logic":"..."},"gas":{...},"coal":{...},"power":{...}},"chain":"传导链：...","analogy":"历史类比（优先国内）：...","trigger":"强化触发：...","falsify":"证伪：..."}],
 "heatmap_events_short":["EV1 简称",...],
 "factor_board":[{"name":"地缘政治与制裁","chip":"偏多 · 强","pos":"left|right|center","width":"41%","desc":"..."}],
 "radar":[2.5,-1.3,0.2,-0.8,0.6,-0.2],
 "summary_rows":[{"event":"EVx ...","oil":{"txt":"▼中强","cls":"up|down|flat"},"gas":{...},"coal":{...},"power":{...},"note":"..."}],
 "scenarios":[{"name":"国内原油 · INE SC","current":"当前 728 元/桶","rows":[{"label":"基准 50%","label_cls":"n|b|s","range":"...","range_cls":"up|down|","trigger":"..."}]}],
 "catalysts":[{"date":"9/18–19","body":"...","sub":"..."}],
 "main_risks":["...","..."],
 "source_status":[{"chip":"已更新|沿用上期|未抓到","chip_cls":"bull|neu|amb","content":"...","data":"..."}]
}"""


_RULES = """你是【国内】能源电力市场事件影响推演引擎，结论只针对国内四品种：
- oil 国内原油 = INE 上海原油 SC 主力（Brent/WTI 仅经进口到岸平价 / USDCNY / 运费传导到 SC）；
- gas 国内天然气 = SHPGX LNG、接收站现货、LNG 槽批、管道气门站价（JKM/TTF 仅经进口成本 / 汇率 / 海运费传导）；
- coal 国内煤炭 = 秦皇岛 Q5500 动力煤、港口/坑口、焦煤、电厂日耗与库存、进口煤价差、保供限产安监；
- power 国内电力 = 广东及南方现货日前/实时为主，核心“煤价→度电燃料成本→现货电价”，叠加来水/风光/气温负荷/容量电价。
铁律：
1. 只依据给定【抓取事实】，价格/链接/时间原样使用；缺数据写 "N/A"，禁止编造；区分事实/报道/推测。
2. 只输出一个合法 JSON 对象，不带 markdown 或解释，括号配平。
3. dir 只能是 "up"/"down"/"flat"；strength 只能是 "弱"/"中"/"强"；priced_in 只能是 "是"/"否"/"部分"。
4. impacts 必须且只有 oil/gas/coal/power 四个键。
5. 中文措辞务必精炼：logic≤40字，chain/analogy/trigger/falsify 各不超过30字。
6. 历史类比优先：2021 全国电荒拉闸限电、2021 煤价暴涨与电价浮动、2022 俄乌推高进口能源/LNG 成本、OPEC 意外减产、极端高温限电拉动动力煤。
7. 【事件选择优先级】抓取事实中只要存在地缘政治/制裁/海峡通道/OPEC 减产增产/美联储与美元/重大能源政策/突发断供，必须优先入选，并尽量让 6 条里覆盖这些政治与政策事件，不得只选行情涨跌类。
8. 【海外/政治事件必须传导到国内四品种】即使是中东、俄乌、美联储等海外事件，也要沿“地缘/宏观→外盘油气→进口到岸平价/汇率/海运费→国内油气→替代与预期→煤电燃料成本→电价”给出方向，禁止因“直接联动弱”而四品种一律 flat；确实无影响的品种才用 flat 并在 logic 简述原因。
9. 四个品种都要独立判断、方向要有分化：同一条事件下 oil/gas/coal/power 不应无条件同向或同 flat；用 strength/confidence 表达不确定性，而不是回避判断。
10. 【默认传导矩阵——除非有明确相反事实，否则照此给非 flat 方向，并按国内缓冲调强度】
   A. 中东冲突升级/原油供应中断或被威胁/海峡(霍尔木兹·红海·苏伊士)受阻/OPEC+超预期减产/制裁禁运出口管制：
      oil=up(强/中)；gas=up（油→气切换发电动气、JKM/TTF进口LNG到岸成本上行；即时~数日；中/弱）；
      coal=up（油气涨价→能源替代与冬储/进口煤价差预期；数日；弱/中）；
      power=up（气电与煤电度电燃料成本上行、替代发电趋紧；数日；弱/中，广东现货对成本更敏感可中）。
   B. 上述风险缓和/停火/复产/航道恢复/OPEC+增产：四品种反向（down），强度按缓和程度取中/弱。
   C. 美联储偏鹰加息/美元走强/全球风险偏好回落：oil、gas 金融属性 down（中）；coal down（弱，国内长协保供缓冲）；power 最钝，弱 down 或 flat。
   D. 中国保供增产/增进口/限价/煤电容量电价与长协落地：coal down（中/强）、power flat 或弱 down（成本被压、电价更稳）；oil/gas 视措辞弱或 flat。
   E. 来水偏枯/持续高温寒潮/风光出力骤降：coal up（中/强）、power up（强/中）、gas up（弱/中，调峰）、oil 弱或 flat；反之来水丰沛/暖冬则反向。
   F. EIA/API 原油或天然气超预期去库：对应 oil/gas up（弱/中），煤电弱联动；超预期累库反向。
   要求：每条政治/政策事件，oil 与 gas 至少其一为非 flat 时，coal、power 也要据上表给出非 flat 的弱/中方向（除非确有长协限价等明确绝缘理由，此时 confidence=低并在 logic 写明）；严禁把“未直接受影响/价格未受直接影响”当作 coal、power 一律 flat 的默认理由。"""


def _llm_env():
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "")
    return (base, key, model) if (base and key and model) else ("", "", "")


def _repair_truncated(s: str):
    """best-effort 修复被 max_tokens 截断的 JSON：截到最后一个完整闭合元素再补齐外层括号。"""
    last_complete = -1
    instr = esc = False
    depth = 0
    for i, ch in enumerate(s):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            last_complete = i
    if last_complete < 0:
        return None
    t = s[: last_complete + 1].rstrip().rstrip(",").rstrip()
    st: list[str] = []
    instr = esc = False
    for ch in t:
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch in "{[":
            st.append(ch)
        elif ch in "}]" and st:
            st.pop()
    cand = t + "".join("}" if c == "{" else "]" for c in reversed(st))
    try:
        return json.loads(cand)
    except Exception:  # noqa: BLE001
        return None


def _loose_json_loads(content: str):
    try:
        return json.loads(content)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{", content)
    if not m:
        return None
    s = content[m.start():]
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return _repair_truncated(s)


def _chat_json(system: str, user: str, *, max_tokens: int = 4096,
               retries: int = 3, total_timeout: int = 240):
    """流式分段：规避跨境长链路整体读超时，较小请求体也降低 4096 截断概率。
    返回 (obj, finish_reason_or_err)。"""
    base, key, model = _llm_env()
    if not base:
        return None, "no_key"
    import requests
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}],
               "temperature": 0.2, "max_tokens": max_tokens, "stream": True,
               "response_format": {"type": "json_object"}}
    last = "未知"
    for attempt in range(retries):
        finish = None
        try:
            r = requests.post(url, headers=headers, json=payload, stream=True, timeout=(15, 60))
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                r.close()
            elif r.status_code != 200:
                print(f"[analyze] LLM 4xx 放弃：{r.status_code} {r.text[:160]}", flush=True)
                return None, f"http_{r.status_code}"
            else:
                parts, deadline = [], time.time() + total_timeout
                for raw in r.iter_lines():
                    if time.time() > deadline:
                        raise TimeoutError("流式接收超过总时长")
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ch0 = json.loads(data)["choices"][0]
                    except Exception:  # noqa: BLE001
                        continue
                    if ch0.get("finish_reason"):
                        finish = ch0["finish_reason"]
                    piece = (ch0.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                r.close()
                obj = _loose_json_loads("".join(parts))
                if isinstance(obj, dict):
                    return obj, (finish or "stop")
                last = f"JSON解析失败(finish={finish})"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}:{str(e)[:100]}"
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    print(f"[analyze] LLM 段落失败：{last}", flush=True)
    return None, last


def _facts(fetched: dict, n: int = 30) -> dict:
    try:
        from fetchers.base import select_events
        ev_sel = select_events(fetched.get("events", []), cap=n)
    except Exception:  # noqa: BLE001 - 极端情况下退回简单截断
        ev_sel = fetched.get("events", [])[:n]
    return {
        "events": [
            {"time": e["time"], "source": e["source"], "title": e["title"],
             "url": e["url"], "category": e["category"]}
            for e in ev_sel
        ],
        "quotes": [
            {"name": q["name"], "price": q["price"], "unit": q["unit"],
             "day_chg": q["day_chg"], "src_time": q["src_time"], "status": q["status"]}
            for q in fetched.get("quotes", [])
        ],
    }


def run_llm(fetched: dict) -> dict | None:
    """两段式（事件段优先、简报段尽力而为）+ 流式 + 截断修复。
    无 key / 事件段失败 -> None（main 走速览版）；简报段失败仅该段代码兜底。"""
    base, _, _ = _llm_env()
    if not base:
        return None

    fact_s = json.dumps(_facts(fetched), ensure_ascii=False)

    # ---- 第 1 段：事件影响（核心，必须成功；只让模型输出 6 事件精简结构）----
    ev_user = (
        "【抓取事实】如下。请挑选对【国内油/气/煤/电】最重要的 6 个真实事件：优先纳入地缘政治/制裁/海峡与管道/OPEC 减产增产/美联储美元/中国能源电力政策/突发断供袭击类事件，其次才是行情涨跌与周度数据；从影响最大到最小排列。"
        "title 必须是该事件本身的事实简述（不要复述本指令、不要出现“排序/仅输出”等字样），且仅输出：\n"
        '{"events":[{"title":"...","time":"...","source":"...","url":["https://..."],'
        '"nature":"突发|确认|数据公布|预期|辟谣",'
        '"impacts":{"oil":{"dir":"up|down|flat","strength":"弱|中|强","lag":"即时|数日|数周",'
        '"confidence":"高|中|低","priced_in":"是|否|部分","logic":"≤40字，落到国内"},'
        '"gas":{...},"coal":{...},"power":{...}},'
        '"chain":"≤30字","analogy":"≤30字","trigger":"≤30字","falsify":"≤30字"}]}\n'
        "价格/链接/时间原样，缺则 N/A。抓取事实：\n" + fact_s)
    # 事件段内容达标策略（glm-4-flash 偶发 finish=stop 却只给 1~3 条）：
    # 有 ≥1 条真实有效事件就走 AI 推演版（远胜整块降级速览）；取两轮中条数较多的一次。
    # 仅当首轮 0~1 条时才追加一次“补足”请求；若为 max_tokens 截断（finish!=stop），
    # _chat_json 已做截断挽救，有几条算几条，立即采用。
    best = None
    best_n = 0
    finish = None
    ev_ask = ev_user
    for _attempt in range(2):
        ev_obj, finish = _chat_json(_RULES, ev_ask, max_tokens=4096)
        cand = ev_obj.get("events") if isinstance(ev_obj, dict) else None
        valid = [e for e in cand if not _is_junk_event(e)] if isinstance(cand, list) else []
        if valid and len(valid) > best_n:
            best, best_n = cand, len(valid)
        if best_n >= 6 or finish != "stop" or best_n >= 2:
            break
        if _attempt == 0:
            print(f"[analyze] 事件段首轮仅 {best_n} 条有效事件，要求补足后重试一次…", flush=True)
            ev_ask = (ev_user + "\n注意：上一次给出的事件过少。"
                                "请务必给出 6 个互不相同、对国内油/气/煤/电影响最重要的真实事件，不要只给 1 个。")
    events = best
    if not isinstance(events, list) or not events:
        print(f"[analyze] 事件段不可用（{finish}），整轮降级数据速览版。", flush=True)
        return None
    print(f"[analyze] 事件段采用 {best_n} 条有效事件（finish={finish}）。", flush=True)
    out: dict[str, Any] = {"events": events}

    # ---- 第 2 段：简报（要点/因子/雷达/速查/情景/催化剂/风险；失败则代码兜底）----
    br_user = (
        "基于同样【抓取事实】，仅输出以下精炼 JSON（措辞简短）：\n"
        '{"core_points":[{"tag":"国内原油|地缘|国内煤炭|国内天然气|宏观|政策","text":"一句话",'
        '"net":{"oil":{"dir":"up|down|flat","label":"▲弱多"},"gas":{...},"coal":{...},"power":{...}}}],'
        '"factor_board":[{"name":"地缘政治与制裁|宏观金融|供需库存|天气季节|中国政策与电力规则|替代能源与基础设施",'
        '"chip":"偏多 · 强|偏空 · 中|中性 · 弱","pos":"left|right|center","width":"41%","desc":"≤24字"}],'
        '"radar":[2.5,-1.3,0.2,-0.8,0.6,-0.2],'
        '"summary_rows":[{"event":"EV简称","oil":{"txt":"▼中","cls":"down"},'
        '"gas":{"txt":"■","cls":"flat"},"coal":{...},"power":{...},"note":"≤16字"}],'
        '"scenarios":[{"name":"国内原油 · INE SC","current":"当前价","rows":['
        '{"label":"基准 50%","label_cls":"n","range":"一句话","range_cls":"","trigger":"≤20字"},'
        '{"label":"上行 25%","label_cls":"b","range":"...","range_cls":"up","trigger":"..."},'
        '{"label":"下行 25%","label_cls":"s","range":"...","range_cls":"down","trigger":"..."}]}],'
        '"catalysts":[{"date":"9/18-19","body":"未来1-3天数据/会议/天气","sub":"≤12字"}],'
        '"main_risks":["一句话风险","..."]}\n'
        "数量：4 条 core_points、6 条 factor_board、6 条 summary_rows、4 个 scenarios、3 条 catalysts、4 条 main_risks。\n"
        "抓取事实：\n" + fact_s)
    br_obj, _ = _chat_json(_RULES, br_user, max_tokens=4096)
    if isinstance(br_obj, dict):
        for k in ("core_points", "factor_board", "radar", "summary_rows", "scenarios",
                  "catalysts", "main_risks", "source_status", "chart_brent",
                  "heatmap_events_short"):
            if k in br_obj:
                out[k] = br_obj[k]
    print(f"[analyze] 完成：事件 {len(events)} 条（事件段finish={finish}），"
          f"简报段 {'有' if isinstance(br_obj, dict) else '无→兜底'}。", flush=True)
    return out


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
    # 国内四组在前；Brent/WTI/HH/TTF/JKM/纽煤/EPEX 等外盘合并为末尾「外盘驱动参照」。
    # 采用“首次匹配归属”：每条 quote 只进一个组，避免国内外重复出现。
    domestic = [
        ("国内原油（INE SC）", ["INE", "SC", "上海原油"]),
        ("国内天然气（LNG·管道气）", ["SHPGX", "LNG", "管道气", "门站"]),
        ("国内煤炭（动力煤·焦煤）", ["秦港", "Q5500", "焦煤", "JM", "坑口", "港口"]),
        ("国内电力（国内现货）", ["广东", "山东", "现货"]),
    ]
    external_name = "外盘驱动参照"

    assigned: list[int | None] = [None] * len(quotes)
    for gi, (_, kws) in enumerate(domestic):
        for qi, q in enumerate(quotes):
            if assigned[qi] is not None:
                continue
            if any(k in q["name"] for k in kws):
                assigned[qi] = gi
    # 未归入国内四组的，全部落入末尾外盘参照组
    ext_idx = len(domestic)

    def _row(q: dict) -> dict:
        day_txt, day_cls = _chg_cell(q["day_chg"])
        week_txt, week_cls = _chg_cell(q["week_chg"])
        link = {"text": _domain(q["url"]), "url": q["url"]} if q["url"] else None
        return {
            "contract": q["name"],
            "price": _fmt_price(q["price"], q["unit"]),
            "price_cls": "num",
            "day": day_txt if q["status"] == "ok" else "N/A",
            "day_cls": day_cls if q["status"] == "ok" else "na",
            "week": week_txt,
            "week_cls": week_cls,
            "src_time": q["src_time"],
            "link": link,
        }

    groups = [(gname, []) for gname, _ in domestic] + [(external_name, [])]
    for qi, q in enumerate(quotes):
        gi = assigned[qi] if assigned[qi] is not None else ext_idx
        groups[gi][1].append(_row(q))

    out = []
    for gname, rows in groups:
        if not rows:
            rows.append({"contract": gname + " 代表性品种", "price": "N/A", "price_cls": "na",
                         "day": "N/A", "day_cls": "na", "week": "N/A", "week_cls": "na",
                         "src_time": "本期未抓到", "link": None})
        out.append({"name": gname, "rows": rows})
    return out


def build_chart_change(quotes: list[dict]) -> dict:
    # 国内口径优先：INE SC / SHPGX LNG / 秦港Q5500 / 广东日前；外盘项排后。
    labels = ["INE SC", "SHPGX LNG", "秦港Q5500", "广东日前", "Brent", "TTF"]
    want = [["INE", "SC", "上海原油"], ["SHPGX", "LNG"], ["秦港", "Q5500"], ["广东", "日前"],
            ["布伦特", "Brent"], ["TTF"]]
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
def _pick(quotes: list[dict], *keywords: str) -> dict | None:
    """首个“命中关键词且 price 非空”的 quote（跳过无值的同名外盘/占位行）。"""
    for q in quotes:
        if q.get("price") is None:
            continue
        if any(k in q["name"] for k in keywords):
            return q
    return None


def snapshot_direction(events: list[dict]) -> str:
    """由契约事件（已过滤）按强度汇总四品种净方向，输出如「油▲ 气▼ 煤■ 电▲」。"""
    names = ["油", "气", "煤", "电"]
    magmap = {"强": 3, "中": 2, "弱": 1}
    scores = [0, 0, 0, 0]
    for e in events:
        for i, imp in enumerate(e["impacts"][:4]):
            mag = magmap.get(str(imp.get("strength")), 2)
            scores[i] += {"up": mag, "down": -mag, "flat": 0}.get(imp["dir"], 0)
    return " ".join(
        f"{names[i]}{'▲' if scores[i] > 0 else ('▼' if scores[i] < 0 else '■')}"
        for i in range(4))


def build_snapshot(quotes: list[dict], dt: datetime, event_line: str, ai_dir: str | None = None) -> dict:
    def chg_up(v):
        if v is None:
            return None
        try:
            f = float(v)
        except (ValueError, TypeError):
            return None
        return True if f > 0 else (False if f < 0 else None)

    # 国内口径优先：oil 优先 INE SC（回退 Brent）；gas 优先 SHPGX（回退 TTF/HH）；
    # coal=秦港Q5500；power=广东日前。
    oil = _pick(quotes, "INE", "SC", "上海原油") or _pick(quotes, "布伦特", "Brent")
    gas = (_pick(quotes, "SHPGX", "LNG") or _pick(quotes, "TTF")
           or _pick(quotes, "HH", "天然气"))
    coal = _pick(quotes, "秦港", "Q5500")
    power = _pick(quotes, "广东", "日前")

    def txt(q):
        return _fmt_price(q["price"], q["unit"]) if q else "N/A"

    def pct(q):
        return f"{q['day_chg']:+.2f}%" if q and q["day_chg"] is not None else "N/A"

    return {
        "time": dt.strftime("%Y-%m-%d %H:%M"),
        "oil": txt(oil), "oil_chg": pct(oil), "oil_up": chg_up(oil["day_chg"] if oil else None),
        "gas": txt(gas), "gas_chg": pct(gas), "gas_up": chg_up(gas["day_chg"] if gas else None),
        "coal": txt(coal), "coal_chg": pct(coal), "coal_up": chg_up(coal["day_chg"] if coal else None),
        "power": txt(power), "power_chg": pct(power), "power_up": chg_up(power["day_chg"] if power else None),
        "dir": ai_dir or "速览版：以实际涨跌为准",
        "event": event_line or "本期快照",
    }


# ---------------------------------------------------------------------------
# LLM 输出 -> 契约 events / heatmap / factors ...
# ---------------------------------------------------------------------------
_JUNK_TITLE_HINTS = (
    "按重要性排序", "最重要的", "仅输出", "抓取事实", "示例", "placeholder",
    "your_", "事件标题", "标题…", "title", "xxx", "...", "…",
)


def _is_junk_event(ev: dict) -> bool:
    """剔除模型把指令/JSON 模板示例误当成的伪事件，以及完全无方向信息的条目。"""
    if not isinstance(ev, dict):
        return True
    t = str(ev.get("title", "")).strip()
    if not t or set(t) <= {"·", " ", "-", ".", "…"}:
        return True
    low = t.lower()
    if any(h.lower() in low for h in _JUNK_TITLE_HINTS):
        return True
    imps = ev.get("impacts") or {}
    has_dir = any(_norm_dir((imps.get(k) or {}).get("dir")) != "flat" for k in PROD_KEYS)
    return not has_dir


def _llm_events_to_contract(llm_events: list[dict]) -> list[dict]:
    # 先剔除指令残留/占位伪事件，过滤后重新连续编号 EV1..EVn
    cleaned = [e for e in llm_events if not _is_junk_event(e)]
    # 极端情况下若全被过滤（如全部中性），退回原始事件，避免整轮空白
    llm_events = cleaned or [e for e in llm_events if isinstance(e, dict)]
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
    _strip_ev = re.compile(r'^EV\d+ · ')
    names = [f"{e['id']} {_strip_ev.sub('', e['title'])[:12]}" for e in events]
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
        "subtitle": "Energy & Power Market Daily Monitor · 事件驱动 · 覆盖 国内原油/国内天然气/国内煤炭/国内电力",
        "date": date_s, "cutoff": cutoff_s, "window": window_s,
        "next_update": "约每 3 小时",
        "badges": badges, "ai_note": ai_note,
    }

    has_llm = isinstance(llm, dict) and bool(llm)

    # ---- core_points ----
    if has_llm and llm.get("core_points"):
        core_points = llm["core_points"]
    else:
        sc = _pick(quotes, "INE", "SC", "上海原油") or _pick(quotes, "布伦特", "Brent")
        core_points = [{
            "tag": "行情速览",
            "text": f"国内原油 INE SC {_fmt_price(sc['price'], sc['unit']) if sc else 'N/A'}；"
                    f"本期共抓取 {len(raw_events)} 条事件、{len(quotes)} 条行情。",
            "net": {p: {"dir": "flat", "label": "■"} for p in PROD_KEYS},
        }]

    # ---- events / heatmap ----
    if has_llm and llm.get("events"):
        events = _llm_events_to_contract(llm["events"])
        # 纵轴简称统一由代码从“已去 EV 前缀”的干净标题生成，避免 “EV1 EV1 ·” 重复
        _strip_ev_local = re.compile(r"^EV\d+ · ")
        short = [f"{e['id']} " + _strip_ev_local.sub("", e["title"])[:12] for e in events]
        # 用 impacts 方向+强度折算热力值（强±3/中±2/弱±1），颜色深浅即强度
        _mag = {"强": 3, "中": 2, "弱": 1}
        matrix = []
        logic = []
        for e in events:
            row, lrow = [], []
            for imp in e["impacts"]:
                mag = _mag.get(str(imp.get("strength")), 2)
                row.append({"up": mag, "down": -mag, "flat": 0}[imp["dir"]])
                lrow.append(str(imp.get("logic") or "—")[:18])
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
        def _scn(name, kws, unit, trig):
            cur = _fmt_price((_pick(quotes, *kws) or {}).get("price"), unit)
            return {"name": name, "current": cur,
                    "rows": [{"label": "基准 50%", "label_cls": "n", "range": "N/A", "range_cls": "", "trigger": trig},
                             {"label": "乐观 25%", "label_cls": "b", "range": "N/A", "range_cls": "up", "trigger": "—"},
                             {"label": "悲观 25%", "label_cls": "s", "range": "N/A", "range_cls": "down", "trigger": "—"}]}
        scenarios = [
            _scn("国内原油 · INE SC", ("INE", "SC", "上海原油"), "元/桶", "速览版未做情景推演"),
            _scn("国内天然气 · SHPGX LNG", ("SHPGX", "LNG"), "元/吨", "速览版未做情景推演"),
            _scn("国内煤炭 · 秦港Q5500", ("秦港", "Q5500"), "元/吨", "速览版未做情景推演"),
            _scn("国内电力 · 广东日前", ("广东", "日前"), "元/MWh", "速览版未做情景推演"),
        ]

    # ---- catalysts / main_risks ----
    catalysts = (llm.get("catalysts") if has_llm else None) or [
        {"date": "持续", "body": "油气煤电主要事件与政策跟踪。", "sub": "速览版"}]
    main_risks = (llm.get("main_risks") if has_llm else None) or [
        "速览版：未配置 LLM_API_KEY，影响推演不可用；价格以抓取层原始数据为准。",
        "盘中不同数据源口径可能存在差异；政策与突发事件可能导致快速偏离。"]

    # ---- sources_rows / appendix（静态巡检入口）----
    sources_rows = [
        {"name": "新浪国内期货（INE / 大商所连续主力）", "monitors": "沪原油 SC、大连焦煤 JM", "freq": "实时",
         "links": [{"text": "SC0", "url": "https://finance.sina.com.cn/futures/quotes/SC0.shtml"},
                   {"text": "JM0", "url": "https://finance.sina.com.cn/futures/quotes/JM0.shtml"}]},
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
