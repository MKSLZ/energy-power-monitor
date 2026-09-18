#!/usr/bin/env python3
"""能源电力市场监控日报 —— 主流水线。

串联：抓取 → (可选 LLM) → 读历史 → 装配 report dict → Jinja2 渲染 → 写静态站。

用法：
  python3 src/main.py --mock          # 离线，加载 data/sample_feed.json 直接渲染
  python3 src/main.py                 # 在线：联网抓取 + 可选 LLM
  python3 src/main.py --out site --data data
环境变量：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（缺省走数据速览版）；EIA_API_KEY（可选）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:  # 报告口径恒为北京时间（UTC+8），不受运行机/Runner 时区影响
    from zoneinfo import ZoneInfo
    BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    BEIJING = timezone(timedelta(hours=8))

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))  # src/ 在 path 上

from jinja2 import Environment, FileSystemLoader  # noqa: E402

import analyze  # noqa: E402
import fetchers  # noqa: E402
import eventstore  # noqa: E402
from fetchers.base import select_events  # noqa: E402

HISTORY_KEEP = 30


def log(msg: str) -> None:
    print(f"[main] {msg}", flush=True)


def load_history(path: Path) -> list[dict]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def render(report: dict, templates_dir: Path, out_file: Path) -> None:
    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)
    tpl = env.get_template("report.html.j2")
    html = tpl.render(**report)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")


def run_mock(data_dir: Path, out_dir: Path) -> Path:
    sample = data_dir / "sample_feed.json"
    log(f"离线 mock 模式，加载 {sample}")
    report = json.loads(sample.read_text(encoding="utf-8"))
    templates_dir = REPO_ROOT / "templates"
    out_file = out_dir / "index.html"
    render(report, templates_dir, out_file)
    log(f"渲染完成（mock）→ {out_file}")
    return out_file


def run_online(data_dir: Path, out_dir: Path) -> Path:
    log("开始抓取事件与行情…")
    fetched = fetchers.fetch_all()
    n_events, n_quotes = len(fetched["events"]), len(fetched["quotes"])
    log(f"抓取完成：事件 {n_events} 条，行情 {n_quotes} 条，源状态 {len(fetched['source_status'])} 条")

    now = datetime.now(BEIJING)
    today_s = now.strftime("%Y-%m-%d")
    now_s = now.strftime("%Y-%m-%d %H:%M")
    history_path = data_dir / "history.json"
    history = load_history(history_path)

    # ===== 按日事件库（append-only）：只对当日“新抓取”事件调推演，全天只增不删 =====
    state = eventstore.load_day(data_dir, today_s)
    new_raw = eventstore.split_new_raw(state, fetched["events"])
    candidates = select_events(new_raw, cap=6, per_source=4) if new_raw else []
    log(f"当日事件库已有 {len(state['events'])} 条；本档新抓取 {len(new_raw)} 条，送推演 {len(candidates)} 条。")

    added = 0
    if candidates:
        log("仅对本档【新增】事件调用 LLM 推演（历史已抓事件不重复推演、不删除）…")
        llm_new = analyze.run_llm({**fetched, "events": candidates})
        if llm_new and llm_new.get("events"):
            good_new = [e for e in llm_new["events"] if not analyze._is_junk_event(e)]
            merged, added = eventstore.merge_events(state["events"], good_new)
            state["events"] = merged
            eventstore.mark_seen(state, candidates)   # 已处理原始事件不再重复送推演
            eventstore.update_brief(state, llm_new)   # 简报段刷新为最近一次
            state["updated_at"] = now_s
            eventstore.save_day(data_dir, state)
            log(f"已追加 {added} 条新事件；当日累积 {len(state['events'])} 条。")
        else:
            log("新增事件推演失败/为空：沿用当日已累积事件与既有推演，不覆盖。")
    else:
        log("本档无新增事件：沿用当日累积事件与最近一次整体推演。")

    day_events = state["events"]
    if day_events:
        llm = dict(state.get("brief") or {})
        llm["events"] = day_events
        llm["summary_rows"] = eventstore.rebuild_summary_rows(day_events)  # 速查矩阵覆盖全天累积事件
        ai_note = None
    else:
        llm = None
        ai_note = "本期AI影响推演不可用/未配置Key（当日暂无已推演事件）"
        log("无 LLM 返回且当日事件库为空，走【数据速览版】。")

    # 行情兜底摘要（国内原油 INE SC 优先，回退 Brent），仅在没有 AI 事件时用于快照
    sc = (analyze._find_quote(fetched["quotes"], "INE", "SC", "上海原油")
          or analyze._find_quote(fetched["quotes"], "布伦特", "Brent"))
    price_line = (f"{sc['name']} {sc['price']:g}（{sc['day_chg']:+.2f}%）"
                  if sc and sc["price"] is not None and sc["day_chg"] is not None
                  else "本期行情见价格区")

    # 快照基于当日累积事件：AI 版写入四品种净方向与头号事件，速览版退回行情一句话
    if day_events:
        cev = analyze._llm_events_to_contract(day_events)
        ai_dir = analyze.snapshot_direction(cev)
        top0 = re.sub(r"^EV\d+ · ", "", cev[0]["title"]) if cev else price_line
        top_event = f"{top0[:30]}（当日{len(day_events)}事件/本档+{added}）"
        snap = analyze.build_snapshot(fetched["quotes"], now, top_event, ai_dir=ai_dir)
    else:
        snap = analyze.build_snapshot(fetched["quotes"], now, price_line)

    # 新快照 unshift 到历史头，保留近 30 条
    snapshots = [snap] + [s for s in history if s.get("time") != snap["time"]]
    snapshots = snapshots[:HISTORY_KEEP]

    report = analyze.assemble_report(fetched, llm, snapshots, now, ai_note=ai_note)
    # 供模板展示“当日累积 / 本档新增”；累积数以实际渲染（剔除全中性/伪事件后）的卡片数为准
    report["day_date"] = today_s
    report["day_events_total"] = len(report.get("events", []))
    report["day_events_new"] = added
    eventstore.prune_old(data_dir, today_s)


    templates_dir = REPO_ROOT / "templates"
    out_file = out_dir / "index.html"
    render(report, templates_dir, out_file)
    log(f"渲染完成（在线）→ {out_file}")

    # 写回历史
    save_json(history_path, snapshots)
    log(f"history.json 已更新（{len(snapshots)} 条）")
    return out_file


def main() -> int:
    ap = argparse.ArgumentParser(description="能源电力市场监控日报流水线")
    ap.add_argument("--mock", action="store_true", help="离线模式，直接渲染 data/sample_feed.json")
    ap.add_argument("--out", default="site", help="输出目录（默认 site）")
    ap.add_argument("--data", default="data", help="数据目录（默认 data）")
    args = ap.parse_args()

    out_dir = (REPO_ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out)
    data_dir = (REPO_ROOT / args.data).resolve() if not Path(args.data).is_absolute() else Path(args.data)

    log(f"输出目录 {out_dir}；数据目录 {data_dir}")
    if args.mock:
        out_file = run_mock(data_dir, out_dir)
    else:
        out_file = run_online(data_dir, out_dir)

    log(f"最终输出：{out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
