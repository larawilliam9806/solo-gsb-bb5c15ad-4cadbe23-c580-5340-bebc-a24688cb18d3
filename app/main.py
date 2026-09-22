"""FastAPI 应用：流程管理、事件追加、回放 / 差异 / 导出 API 与 Web 界面。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import replay as engine
from .database import get_conn, init_db
from .models import EventCreate, FlowCreate, now_iso, parse_time

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时完成数据库初始化
    yield


app = FastAPI(title="实验流程事件回放台", version="1.0.0", lifespan=lifespan)


# ---------- 工具 ----------

def _row_to_event(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def _get_flow_or_404(flow_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    return dict(row)


def _load_events(flow_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE flow_id = ? ORDER BY id", (flow_id,)
        ).fetchall()
    return [_row_to_event(r) for r in rows]


# ---------- 流程 API ----------

@app.post("/api/flows", status_code=201)
def create_flow(body: FlowCreate):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO flows (name, version, description, created_at) VALUES (?, ?, ?, ?)",
            (body.name, body.version, body.description, now_iso()),
        )
        flow_id = cur.lastrowid
    return _get_flow_or_404(flow_id)


@app.get("/api/flows")
def list_flows():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT f.*, COUNT(e.id) AS event_count
            FROM flows f LEFT JOIN events e ON e.flow_id = f.id
            GROUP BY f.id ORDER BY f.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/flows/{flow_id}")
def get_flow(flow_id: int):
    return _get_flow_or_404(flow_id)


@app.delete("/api/flows/{flow_id}", status_code=204)
def delete_flow(flow_id: int):
    _get_flow_or_404(flow_id)
    with get_conn() as conn:
        conn.execute("DELETE FROM flows WHERE id = ?", (flow_id,))


# ---------- 事件 API ----------

@app.post("/api/flows/{flow_id}/events", status_code=201)
def append_event(flow_id: int, body: EventCreate):
    _get_flow_or_404(flow_id)
    if body.event_type == "undone" and body.target_event_id is None:
        raise HTTPException(status_code=422, detail="undone 事件必须提供 target_event_id")
    if body.target_event_id is not None:
        with get_conn() as conn:
            target = conn.execute(
                "SELECT id FROM events WHERE id = ? AND flow_id = ?",
                (body.target_event_id, flow_id),
            ).fetchone()
        if target is None:
            raise HTTPException(status_code=422, detail="target_event_id 不存在于该流程")

    event_time = body.event_time or now_iso()
    # 归一化为带时区的 ISO 字符串，保证排序一致
    event_time = parse_time(event_time).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO events
               (flow_id, seq, event_type, step_id, payload, event_time, received_at, target_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                flow_id,
                body.seq,
                body.event_type,
                body.step_id,
                json.dumps(body.payload, ensure_ascii=False),
                event_time,
                now_iso(),
                body.target_event_id,
            ),
        )
        event_id = cur.lastrowid
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_event(row)


@app.get("/api/flows/{flow_id}/events")
def list_events(flow_id: int, order: str = Query("received", pattern="^(received|replay)$")):
    """order=received 按到达顺序；order=replay 按回放顺序。"""
    _get_flow_or_404(flow_id)
    events = _load_events(flow_id)
    if order == "replay":
        events = engine.order_events(events)
    return events


# ---------- 回放 / 差异 / 导出 API ----------

@app.get("/api/flows/{flow_id}/replay")
def replay_flow(flow_id: int, at: str | None = None):
    """返回 at 时刻（缺省为最终）的重放状态。"""
    _get_flow_or_404(flow_id)
    events = _load_events(flow_id)
    if at is not None:
        try:
            parse_time(at)
        except ValueError:
            raise HTTPException(status_code=422, detail="at 时间格式非法")
    return engine.replay(events, at=at)


@app.get("/api/flows/{flow_id}/diff")
def diff_flow(flow_id: int, from_: str = Query(..., alias="from"), to: str = Query(...)):
    """比较 from / to 两个时刻的状态差异。"""
    _get_flow_or_404(flow_id)
    for name, value in (("from", from_), ("to", to)):
        try:
            parse_time(value)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{name} 时间格式非法")
    events = _load_events(flow_id)
    a = engine.replay(events, at=from_)
    b = engine.replay(events, at=to)
    return {"from": from_, "to": to, **engine.diff_states(a, b)}


@app.get("/api/flows/{flow_id}/export")
def export_flow(flow_id: int):
    """导出完整回放结果（回放顺序事件、最终状态、逐时刻快照）为 JSON 文件。"""
    flow = _get_flow_or_404(flow_id)
    events = _load_events(flow_id)
    result = {
        "flow": flow,
        "events_received_order": events,
        "events_replay_order": engine.order_events(events),
        "final_state": engine.replay(events),
        "snapshots": engine.build_snapshots(events),
    }
    filename = f"flow-{flow_id}-replay.json"
    return JSONResponse(
        content=result,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- Web 界面 ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
