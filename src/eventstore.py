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

from fetchers.base import norm_title, select_events  # noqa: E402

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
                st.setdefault("pending", [])
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


# ---------------------------------------------------------------------------
# 待分析事件队列（pending）：保证当日所有重要事件最终都会被分析。
# 新抓到的相关事件先进队列；每档取前若干条分批送模型；模型作答（含判定无影响）
# 的出队并记入 seen，漏答的 attempts+1 留下档补做，超过上限才放弃（避免坏条目永久堵队列）。
# ---------------------------------------------------------------------------
def ingest_pending(state: dict, raw_events: list[dict], *,
                   cap: int = 40, per_source: int = 10) -> int:
    """把本期新抓取、且此前从未处理（未上卡/未判无影响/不在队）的相关事件排入待分析队列。"""
    ordered = select_events(raw_events, cap=cap, per_source=per_source) if raw_events else []
    termin = set(state.get("seen_raw_keys", []))
    termin |= {norm_title(str(e.get("title", "")))
               for e in state.get("events", []) if isinstance(e, dict)}
    have = {p.get("key") for p in state.get("pending", []) if isinstance(p, dict)}
    pend = state.setdefault("pending", [])
    added = 0
    for it in ordered:
        k = norm_title(str(it.get("title", "")))
        if not k or k in termin or k in have:
            continue
        pend.append({"key": k, "item": it, "attempts": 0})
        have.add(k)
        added += 1
    return added


def take_batch(state: dict, size: int = 5) -> list[dict]:
    """取待分析队列最前 size 条的原始事件（不出队，结算时再定去留）。"""
    return [p["item"] for p in state.get("pending", [])[:size] if isinstance(p, dict)]


def settle_batch(state: dict, batch: list[dict], decided_idx: list[int],
                 max_attempts: int = 3) -> tuple[int, int]:
    """结算一批：模型作答的条目（含判无影响）出队并入 seen；漏答的 attempts+1，
    达到上限则放弃出队（防止坏条目永久堵塞）。返回 (已作答出队数, 放弃数)。"""
    pend = state.get("pending", [])
    decided_keys = set()
    for i in decided_idx:
        if isinstance(i, int) and 0 <= i < len(batch):
            decided_keys.add(norm_title(str(batch[i].get("title", ""))))
    batch_keys = {norm_title(str(it.get("title", ""))) for it in batch}
    seen = set(state.get("seen_raw_keys", []))
    new_pend: list[dict] = []
    finished = gave_up = 0
    for p in pend:
        k = p.get("key")
        if k in batch_keys:
            if k in decided_keys:
                seen.add(k)
                finished += 1
                continue
            p["attempts"] = int(p.get("attempts", 0)) + 1
            if p["attempts"] >= max_attempts:
                seen.add(k)
                gave_up += 1
                continue
        new_pend.append(p)
    state["pending"] = new_pend
    state["seen_raw_keys"] = sorted(seen)
    return finished, gave_up
