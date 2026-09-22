"""FastAPI 入口：JSON API + 静态 Web 界面。"""
from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .config import MAX_DATA_BYTES, SEED_ON_STARTUP
from .replay import diff_states, normalize_ts, now_iso, parse_ts, replay
from .schemas import EventCreate, EventRevoke, ProcessCreate
from .seed import seed_demo

STATIC_DIR = "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if SEED_ON_STARTUP:
        conn = db.connect()
        try:
            seed_demo(conn)
        except LookupError:
            pass
        finally:
            conn.close()
    yield


app = FastAPI(
    title="实验流程事件回放台",
    description="追加带时间戳的步骤事件，在乱序/撤销后按规则重放任意时刻状态并导出结果。",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------- 辅助 ----------------

def _get_process_or_404(conn, pid: int):
    row = db.get_process(conn, pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"流程 {pid} 不存在")
    return row


def _replay_payload(conn, pid: int, at: str | None) -> dict:
    events = db.list_events(conn, pid, include_revoked=True)
    result = replay(events, at=at)
    return {
        "process_id": pid,
        "at": result.at,
        "state": result.state,
        "applied": result.applied,
        "skipped": result.skipped,
        "ignored": result.ignored,
        "counts": {
            "total": result.count_total,
            "considered": result.count_considered,
            "applied": len(result.applied),
            "skipped": len(result.skipped),
            "ignored": len(result.ignored),
        },
    }


def _validate_data_size(data: dict) -> None:
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > MAX_DATA_BYTES:
        raise HTTPException(status_code=413, detail=f"data 超过大小限制 {MAX_DATA_BYTES} 字节")


# ---------------- 基础 ----------------

@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "time": now_iso()}


@app.post("/api/seed", tags=["meta"], status_code=201)
def create_demo() -> dict:
    """一键写入演示流程（含乱序与撤销事件）。"""
    conn = db.connect()
    try:
        try:
            proc = seed_demo(conn)
        except LookupError:
            raise HTTPException(status_code=409, detail="演示流程已存在")
        return proc
    finally:
        conn.close()


# ---------------- 流程 ----------------

@app.get("/api/processes", tags=["processes"])
def api_list_processes() -> list[dict]:
    conn = db.connect()
    try:
        return db.list_processes(conn)
    finally:
        conn.close()


@app.post("/api/processes", tags=["processes"], status_code=201)
def api_create_process(body: ProcessCreate) -> dict:
    conn = db.connect()
    try:
        if db.find_process(conn, body.name, body.version) is not None:
            raise HTTPException(status_code=409, detail="同名同版本流程已存在")
        pid = db.create_process(conn, body.name.strip(), body.version.strip(),
                                body.note, now_iso())
        conn.commit()
        row = db.get_process(conn, pid)
        return db.process_to_dict(row)
    finally:
        conn.close()


@app.get("/api/processes/{pid}", tags=["processes"])
def api_get_process(pid: int) -> dict:
    conn = db.connect()
    try:
        row = _get_process_or_404(conn, pid)
        return {**db.process_to_dict(row), "events": db.list_events(conn, pid)}
    finally:
        conn.close()


@app.delete("/api/processes/{pid}", tags=["processes"], status_code=204)
def api_delete_process(pid: int) -> Response:
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        db.delete_process(conn, pid)
        conn.commit()
        return Response(status_code=204)
    finally:
        conn.close()


# ---------------- 事件 ----------------

@app.post("/api/processes/{pid}/events", tags=["events"], status_code=201)
def api_append_event(pid: int, body: EventCreate) -> dict:
    _validate_data_size(body.data)
    try:
        ts = normalize_ts(body.ts)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="ts 不是合法的 ISO8601 时间戳")
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        ev = db.append_event(conn, pid, ts, body.kind, body.step_key.strip(),
                             body.title, body.data, now_iso())
        conn.commit()
        return ev
    finally:
        conn.close()


@app.patch("/api/processes/{pid}/events/{event_id}", tags=["events"])
def api_revoke_event(pid: int, event_id: int, body: EventRevoke) -> dict:
    """撤销 / 取消撤销事件（软删除）。撤销后的事件不参与重放。"""
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        row = db.get_event(conn, pid, event_id)
        if row is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        db.set_event_revoked(conn, event_id, body.revoked)
        conn.commit()
        row = db.get_event(conn, pid, event_id)
        return db.event_to_dict(row)
    finally:
        conn.close()


@app.delete("/api/processes/{pid}/events/{event_id}", tags=["events"], status_code=204)
def api_delete_event(pid: int, event_id: int) -> Response:
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        if db.get_event(conn, pid, event_id) is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        db.delete_event(conn, event_id)
        conn.commit()
        return Response(status_code=204)
    finally:
        conn.close()


# ---------------- 重放 / 差异 / 导出 ----------------

def _parse_at(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return parse_ts(raw).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"at 不是合法的 ISO8601 时间戳: {raw}")


@app.get("/api/processes/{pid}/replay", tags=["replay"])
def api_replay(pid: int, at: str | None = Query(default=None, description="重放到此刻（含），缺省为最新")) -> dict:
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        return _replay_payload(conn, pid, _parse_at(at))
    finally:
        conn.close()


@app.get("/api/processes/{pid}/diff", tags=["replay"])
def api_diff(pid: int,
             from_at: str | None = Query(default=None, alias="from"),
             to_at: str | None = Query(default=None, alias="to")) -> dict:
    """对比两个时刻的状态；from 缺省为流程起点（空状态），to 缺省为最新。"""
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        events = db.list_events(conn, pid, include_revoked=True)
        left = replay(events, at=_parse_at(from_at)).state
        right = replay(events, at=_parse_at(to_at)).state
        return {
            "process_id": pid,
            "from_at": _parse_at(from_at),
            "to_at": _parse_at(to_at),
            **diff_states(left, right),
        }
    finally:
        conn.close()


def _export_name(pid: int, suffix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"process-{pid}-replay-{stamp}.{suffix}"


@app.get("/api/processes/{pid}/export.json", tags=["export"])
def export_json(pid: int, at: str | None = Query(default=None)) -> Response:
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        payload = _replay_payload(conn, pid, _parse_at(at))
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{_export_name(pid, "json")}"'},
        )
    finally:
        conn.close()


@app.get("/api/processes/{pid}/export.csv", tags=["export"])
def export_csv(pid: int, at: str | None = Query(default=None)) -> Response:
    conn = db.connect()
    try:
        _get_process_or_404(conn, pid)
        payload = _replay_payload(conn, pid, _parse_at(at))
        buf = io.StringIO()
        buf.write("﻿")  # BOM，便于 Excel 正确识别 UTF-8
        writer = csv.writer(buf)
        writer.writerow(["order", "step_key", "title", "data_json"])
        for idx, step in enumerate(payload["state"], start=1):
            writer.writerow([idx, step["step_key"], step["title"],
                             json.dumps(step["data"], ensure_ascii=False)])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{_export_name(pid, "csv")}"'},
        )
    finally:
        conn.close()


# ---------------- 静态页面 ----------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(f"{STATIC_DIR}/index.html")
