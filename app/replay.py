"""纯函数式回放引擎。

事件规则
--------
每个步骤事件属于某个流程，包含：
    seq        追加顺序（同流程内自增，乱序到达时的次序打破依据）
    ts         步骤时间戳（UTC ISO8601，可晚到 / 乱序）
    kind       ``add`` 新增步骤，``update`` 更新已存在步骤，``remove`` 撤销步骤
    step_key   步骤业务键（同流程内唯一）
    title      步骤标题
    data       任意 JSON 对象
    revoked    事件本身是否被撤销（软删除）；撤销后的事件不参与重放

重放规则
--------
1. 仅取 revoked = 0 的事件；
2. 按 (时间戳 ts, 追加顺序 seq) 升序排列 —— 时间戳相同则按追加顺序，
   因此"晚到的乱序事件"落库后重放位置仍由 ts 决定；
3. 依次折叠：add 写入步骤、update 覆盖标题并浅合并 data、remove 删除步骤；
4. update/remove 作用于当时不存在的步骤时记为异常事件，不影响其他事件；
5. 给定 at（时刻）时，只折叠 ts <= at 的事件，得到"任意时刻"的状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


def now_iso() -> str:
    """当前 UTC 时间，精确到微秒的 ISO8601 字符串（Z 结尾）。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    """解析 ISO8601 时间戳；裸时间视为 UTC；非法值抛 ValueError。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_ts(value: str | None) -> str:
    """把用户传入的时间戳归一化为 UTC ISO8601；为空则取当前时间。"""
    if value is None or not value.strip():
        return now_iso()
    return parse_ts(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _event_sort_key(ev: Mapping[str, Any]) -> tuple[datetime, int]:
    return parse_ts(str(ev["ts"])), int(ev["seq"])


def _ordered(mapping: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """按步骤首次插入顺序输出（dict 保序），并补上 step_key。"""
    result = []
    for key, step in mapping.items():
        result.append({"step_key": key, **step})
    return result


@dataclass
class AppliedEvent:
    seq: int
    id: int
    ts: str
    kind: str
    step_key: str
    title: str
    effect: str  # applied | noop-target-missing


@dataclass
class ReplayResult:
    at: str | None
    state: list[dict[str, Any]]
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    ignored: list[dict[str, Any]] = field(default_factory=list)
    count_total: int = 0
    count_considered: int = 0


def replay(events: Iterable[Mapping[str, Any]], at: str | None = None) -> ReplayResult:
    """对一组事件执行重放。

    events 元素至少包含 id/seq/ts/kind/step_key/title/data/revoked，
    可以是 sqlite Row 或普通 dict。at 为 UTC ISO8601；None 表示重放到最后。
    """
    cutoff = parse_ts(at) if at else None
    at_norm = cutoff.isoformat(timespec="microseconds").replace("+00:00", "Z") if cutoff else None

    ordered_events = sorted(events, key=_event_sort_key)

    state: dict[str, dict[str, Any]] = {}
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []   # 因 at 截断而跳过
    ignored: list[dict[str, Any]] = []   # 被撤销 或 目标缺失
    considered = 0

    for ev in ordered_events:
        ev_ts = parse_ts(str(ev["ts"]))
        base = {
            "id": int(ev["id"]),
            "seq": int(ev["seq"]),
            "ts": ev["ts"],
            "kind": ev["kind"],
            "step_key": ev["step_key"],
            "title": ev["title"],
        }
        if cutoff is not None and ev_ts > cutoff:
            skipped.append(base)
            continue
        if int(ev["revoked"]):
            ignored.append({**base, "reason": "revoked"})
            continue

        considered += 1
        kind = ev["kind"]
        key = ev["step_key"]
        effect = "applied"

        if kind == "add":
            # 重复 add 视为幂等覆盖（data 整体替换）
            state[key] = {"title": ev["title"], "data": dict(ev["data"] or {}),
                          "added_seq": int(ev["seq"]), "updated_seq": int(ev["seq"])}
        elif kind == "update":
            if key not in state:
                ignored.append({**base, "reason": "target-missing"})
                continue
            merged = dict(state[key]["data"])
            merged.update(ev["data"] or {})
            state[key] = {**state[key], "title": ev["title"] or state[key]["title"],
                          "data": merged, "updated_seq": int(ev["seq"])}
        elif kind == "remove":
            if key not in state:
                ignored.append({**base, "reason": "target-missing"})
                continue
            del state[key]
        else:
            ignored.append({**base, "reason": f"unknown-kind:{kind}"})
            continue

        applied.append({**base, "effect": effect})

    return ReplayResult(
        at=at_norm,
        state=_ordered(state),
        applied=applied,
        skipped=skipped,
        ignored=ignored,
        count_total=len(ordered_events),
        count_considered=considered,
    )


def diff_states(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    """对比两个时刻的状态快照，输出 added / removed / changed / unchanged。"""
    lm = {s["step_key"]: s for s in left}
    rm = {s["step_key"]: s for s in right}

    added, removed, changed, unchanged = [], [], [], []
    for key in rm.keys() - lm.keys():
        added.append(rm[key])
    for key in lm.keys() - rm.keys():
        removed.append(lm[key])
    for key in lm.keys() & rm.keys():
        if lm[key].get("title") != rm[key].get("title") or lm[key].get("data") != rm[key].get("data"):
            changed.append({"step_key": key, "before": lm[key], "after": rm[key]})
        else:
            unchanged.append(rm[key])

    sort_key = lambda s: s["step_key"]
    return {
        "added": sorted(added, key=sort_key),
        "removed": sorted(removed, key=sort_key),
        "changed": sorted(changed, key=lambda c: c["step_key"]),
        "unchanged": sorted(unchanged, key=sort_key),
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(unchanged),
        },
    }
