#!/usr/bin/env python3
"""按日事件库（append-only）。

每 3 小时一档：只对当日【新抓取到】的事件调一次 LLM 推演，随后把已推演事件
追加进当天的事件库；同一事件不重复推演、历史已抓事件不删除。跨北京自然日
自动另起一篇（旧文件保留近 30 天）。

文件：data/events/YYYY-MM-DD.json
结构：{"date", "updated_at", "seen_raw_keys":[原始标题归一...],
       "events":[LLM 产出的已推演事件，原样累积...], "brief":{最近一次简报段字段}}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fetchers.base import norm_title  # noqa: E402

KEEP_DAYS = 30
_BRIEF_KEYS = ("core_points", "factor_board", "radar", "summary_rows",
               "scenarios", "catalysts", "main_risks", "source_status",
               "chart_brent", "heatmap_events_short")


def events_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "events"


def day_path(data_dir: Path, date_s: str) -> Path:
    return events_dir(data_dir) / f"{date_s}.json"


def load_day(data_dir: Path, date_s: str) -> dict:
    p = day_path(data_dir, date_s)
    if p.exists():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(st, dict) and st.get("date") == date_s:
                st.setdefault("seen_raw_keys", [])
                st.setdefault("events", [])
                st.setdefault("brief", {})
                return st
        except Exception:  # noqa: BLE001
            pass
    return {"date": date_s, "updated_at": "", "seen_raw_keys": [],
            "events": [], "brief": {}}


def save_day(data_dir: Path, state: dict) -> Path:
    d = events_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = day_path(data_dir, state["date"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # 原子替换
    return p


def prune_old(data_dir: Path, today_s: str, keep_days: int = KEEP_DAYS) -> None:
    d = events_dir(data_dir)
    if not d.exists():
        return
    cutoff = time.mktime(time.strptime(today_s, "%Y-%m-%d")) - keep_days * 86400
    for f in d.glob("*.json"):
        try:
            ds = f.stem
            if time.mktime(time.strptime(ds, "%Y-%m-%d")) < cutoff:
                f.unlink()
        except Exception:  # noqa: BLE001
            continue


def _dup_of(k: str, existing: set[str]) -> bool:
    """归一标题精确命中，或互为较长子串（同一事件模型两轮措辞略不同）即视为重复。"""
    if k in existing:
        return True
    if len(k) < 12:
        return False
    for ek in existing:
        if len(ek) >= 12 and (k in ek or ek in k):
            return True
    return False


def merge_events(old_events: list[dict], new_events: list[dict]) -> tuple[list[dict], int]:
    """把本轮新推演事件按标题去重后追加到当日累积序列，返回 (合并后, 真正新增数)。"""
    merged = list(old_events)
    existing = {norm_title(str(e.get("title", ""))) for e in merged if isinstance(e, dict)}
    added = 0
    for e in new_events:
        if not isinstance(e, dict):
            continue
        k = norm_title(str(e.get("title", "")))
        if not k or _dup_of(k, existing):
            continue
        merged.append(e)
        existing.add(k)
        added += 1
    return merged, added


def split_new_raw(state: dict, raw_events: list[dict]) -> list[dict]:
    """以【原始抓取标题】是否已在今日处理过，判定本档新增原始事件。"""
    seen = set(state.get("seen_raw_keys", []))
    out, local = [], set()
    for r in raw_events:
        if not isinstance(r, dict):
            continue
        k = norm_title(str(r.get("title", "")))
        if not k or k in seen or k in local:
            continue
        local.add(k)
        out.append(r)
    return out


def mark_seen(state: dict, raw_events: list[dict]) -> None:
    seen = set(state.get("seen_raw_keys", []))
    for r in raw_events:
        if isinstance(r, dict):
            seen.add(norm_title(str(r.get("title", ""))))
    state["seen_raw_keys"] = sorted(seen)


def update_brief(state: dict, llm: dict) -> None:
    brief = dict(state.get("brief") or {})
    for k in _BRIEF_KEYS:
        if k in llm:
            brief[k] = llm[k]
    state["brief"] = brief


def _dir_cell(d: str, strength: str) -> dict:
    d = (d or "flat").lower()
    s = strength or ""
    if d.startswith("up"):
        return {"txt": f"▲{s}", "cls": "up"}
    if d.startswith("down") or d.startswith("d"):
        return {"txt": f"▼{s}", "cls": "down"}
    return {"txt": "■", "cls": "flat"}


def rebuild_summary_rows(events: list[dict]) -> list[dict]:
    """用当日【全部累积事件】重建速查矩阵，保证与事件卡/热力图条数一致。"""
    rows = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        title = str(ev.get("title", "—"))
        imp = ev.get("impacts") or {}
        cells = {}
        for key in ("oil", "gas", "coal", "power"):
            one = imp.get(key) or {}
            cells[key] = _dir_cell(str(one.get("dir", "flat")), str(one.get("strength", "")))
        rows.append({
            "event": title[:14],
            "oil": cells["oil"], "gas": cells["gas"],
            "coal": cells["coal"], "power": cells["power"],
            "note": str(ev.get("chain", "") or "—")[:16],
        })
    return rows
