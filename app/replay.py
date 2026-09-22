"""回放引擎：将乱序到达、含撤销的事件流按规则重放为确定性状态。

回放规则
--------
1. 排序：所有事件按 (event_time, seq, id) 升序得到唯一的“回放顺序”，
   与事件到达（写入数据库）的先后无关。
2. 撤销：event_type=undone 的事件取消其 target_event_id 指向的事件，
   仅当目标事件在回放顺序中位于该 undo 之前时生效；被取消的事件不产生任何效果。
3. 应用：按回放顺序依次应用未被取消的事件，构建步骤状态：
   - started   -> status = running
   - completed -> status = completed
   - failed    -> status = failed
   - updated   -> 仅合并 payload 到步骤数据
   所有非 undo 事件的 payload 都会合并进步骤数据。
4. 时刻状态：回放 at 时刻的状态 = 仅让 event_time <= at 的事件参与上述流程。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from .models import parse_time


def order_events(events: list[dict]) -> list[dict]:
    """按 (event_time, seq, id) 排序，返回回放顺序的事件列表。"""
    return sorted(
        events,
        key=lambda e: (
            parse_time(e["event_time"]),
            e["seq"] if e["seq"] is not None else 0,
            e["id"],
        ),
    )


def replay(events: list[dict], at: Optional[str] = None) -> dict[str, Any]:
    """重放事件流，返回 at 时刻（缺省为最终）的状态。"""
    ordered = order_events(events)
    if at is not None:
        at_dt = parse_time(at)
        ordered = [e for e in ordered if parse_time(e["event_time"]) <= at_dt]

    # 第一遍：确定被取消的事件 id（undo 必须排在目标之后才生效）
    cancelled: set[int] = set()
    seen: set[int] = set()
    for e in ordered:
        if e["event_type"] == "undone" and e.get("target_event_id") in seen:
            cancelled.add(e["target_event_id"])
        seen.add(e["id"])

    # 第二遍：按序应用
    steps: dict[str, dict] = {}
    step_order: list[str] = []
    applied: list[dict] = []
    for e in ordered:
        if e["id"] in cancelled:
            continue
        applied.append(e)
        if e["event_type"] == "undone":
            continue  # undo 本身不直接改变步骤，只取消目标事件
        sid = e["step_id"]
        step = steps.get(sid)
        if step is None:
            step = {"step_id": sid, "status": "pending", "data": {}, "last_event_id": None}
            steps[sid] = step
            step_order.append(sid)
        if e["event_type"] == "started":
            step["status"] = "running"
        elif e["event_type"] == "completed":
            step["status"] = "completed"
        elif e["event_type"] == "failed":
            step["status"] = "failed"
        # updated 不改状态；所有类型的 payload 均合并
        payload = e.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        step["data"].update(payload)
        step["last_event_id"] = e["id"]

    return {
        "at": at,
        "steps": [steps[sid] for sid in step_order],
        "applied_event_ids": [e["id"] for e in applied],
        "cancelled_event_ids": sorted(cancelled),
        "event_count": len(ordered),
    }


def diff_states(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """比较两个时刻的状态，返回新增 / 消失 / 变化的步骤。"""
    sa = {s["step_id"]: s for s in a["steps"]}
    sb = {s["step_id"]: s for s in b["steps"]}

    added = [sb[sid] for sid in sb.keys() - sa.keys()]
    removed = [sa[sid] for sid in sa.keys() - sb.keys()]
    changed = []
    for sid in sa.keys() & sb.keys():
        fa, fb = sa[sid], sb[sid]
        delta = {}
        if fa["status"] != fb["status"]:
            delta["status"] = {"from": fa["status"], "to": fb["status"]}
        if fa["data"] != fb["data"]:
            keys = fa["data"].keys() | fb["data"].keys()
            data_delta = {
                k: {"from": fa["data"].get(k), "to": fb["data"].get(k)}
                for k in sorted(keys)
                if fa["data"].get(k) != fb["data"].get(k)
            }
            if data_delta:
                delta["data"] = data_delta
        if delta:
            changed.append({"step_id": sid, **delta})

    return {"added": added, "removed": removed, "changed": changed}


def build_snapshots(events: list[dict]) -> list[dict[str, Any]]:
    """对每个事件时间点生成一份状态快照，用于导出完整回放过程。"""
    ordered = order_events(events)
    times = sorted({e["event_time"] for e in ordered}, key=parse_time)
    return [{"at": t, **replay(events, at=t)} for t in times]
