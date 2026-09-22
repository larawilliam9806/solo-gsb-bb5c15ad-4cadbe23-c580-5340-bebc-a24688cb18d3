"""回放引擎与 JSON API 的测试：乱序、撤销、任意时刻、差异、导出。"""
from __future__ import annotations

import os
import tempfile

import pytest

# 在导入 app 之前指定临时数据库
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name
os.environ["SEED_ON_STARTUP"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.replay import diff_states, parse_ts, replay  # noqa: E402


def ev(eid, seq, ts, kind, key, title="", data=None, revoked=0):
    return {"id": eid, "seq": seq, "ts": ts, "kind": kind, "step_key": key,
            "title": title, "data": data or {}, "revoked": revoked}


T0 = "2026-01-01T00:00:00Z"


def t(sec):
    return f"2026-01-01T00:00:{sec:02d}Z"


# ---------------- 引擎层 ----------------

def test_out_of_order_events_replay_by_timestamp():
    # 追加顺序与时间戳故意不一致（晚到事件）
    events = [
        ev(1, 1, t(20), "add", "b", "B"),
        ev(2, 2, t(10), "add", "a", "A"),
        ev(3, 3, t(30), "add", "c", "C"),
    ]
    r = replay(events)
    assert [s["step_key"] for s in r.state] == ["a", "b", "c"]
    assert [e["step_key"] for e in r.applied] == ["a", "b", "c"]


def test_same_timestamp_breaks_by_seq():
    events = [
        ev(1, 2, t(5), "add", "x", "second"),
        ev(2, 1, t(5), "add", "x", "first"),
    ]
    r = replay(events)
    assert r.state[0]["title"] == "second"  # seq 更大的后生效（重复 add 覆盖）


def test_revoke_step_via_remove_event():
    events = [
        ev(1, 1, t(0), "add", "a", "A"),
        ev(2, 2, t(5), "remove", "a"),
        ev(3, 3, t(10), "add", "b", "B"),
    ]
    r = replay(events)
    assert [s["step_key"] for s in r.state] == ["b"]


def test_revoked_event_is_ignored():
    events = [
        ev(1, 1, t(0), "add", "a", "A"),
        ev(2, 2, t(5), "remove", "a", revoked=1),  # 事件被撤销（软删除）
    ]
    r = replay(events)
    assert [s["step_key"] for s in r.state] == ["a"]
    assert r.ignored[0]["reason"] == "revoked"


def test_update_merges_data():
    events = [
        ev(1, 1, t(0), "add", "a", "A", {"x": 1, "y": 2}),
        ev(2, 2, t(5), "update", "a", "A2", {"y": 3, "z": 4}),
    ]
    r = replay(events)
    assert r.state[0]["title"] == "A2"
    assert r.state[0]["data"] == {"x": 1, "y": 3, "z": 4}


def test_update_missing_target_ignored():
    r = replay([ev(1, 1, t(0), "update", "ghost", title="g")])
    assert r.state == []
    assert r.ignored[0]["reason"] == "target-missing"


def test_replay_at_arbitrary_moment():
    events = [
        ev(1, 1, t(0), "add", "a", "A"),
        ev(2, 2, t(10), "add", "b", "B"),
        ev(3, 3, t(20), "remove", "a"),
    ]
    at_15 = replay(events, at="2026-01-01T00:00:15Z")
    assert [s["step_key"] for s in at_15.state] == ["a", "b"]
    assert len(at_15.skipped) == 1  # remove 在 15s 之后

    at_5 = replay(events, at="2026-01-01T00:00:05Z")
    assert [s["step_key"] for s in at_5.state] == ["a"]


def test_diff_states():
    left = [{"step_key": "a", "title": "A", "data": {"v": 1}},
            {"step_key": "g", "title": "G", "data": {}}]
    right = [{"step_key": "a", "title": "A2", "data": {"v": 2}},
             {"step_key": "b", "title": "B", "data": {}}]
    d = diff_states(left, right)
    assert [s["step_key"] for s in d["added"]] == ["b"]
    assert [s["step_key"] for s in d["removed"]] == ["g"]
    assert [c["step_key"] for c in d["changed"]] == ["a"]
    assert d["summary"] == {"added": 1, "removed": 1, "changed": 1, "unchanged": 0}


def test_parse_ts_naive_treated_as_utc():
    assert parse_ts("2026-01-01T00:00:00") == parse_ts("2026-01-01T00:00:00Z")


# ---------------- API 层 ----------------

@pytest.fixture(scope="module")
def client():
    db.init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def pid(client):
    r = client.post("/api/processes", json={"name": "API测试流程", "version": "2.3.1",
                                            "note": "test"})
    assert r.status_code == 201
    return r.json()["id"]


def test_duplicate_name_version_conflict(client, pid):
    r = client.post("/api/processes", json={"name": "API测试流程", "version": "2.3.1"})
    assert r.status_code == 409


def test_append_out_of_order_and_replay_api(client, pid):
    def add(sec, kind, key, **kw):
        return client.post(f"/api/processes/{pid}/events",
                           json={"ts": t(sec), "kind": kind, "step_key": key, **kw})

    assert add(20, "add", "b", title="B", data={"k": 1}).status_code == 201  # seq1
    assert add(10, "add", "a", title="A").status_code == 201                 # seq2 晚到
    assert add(30, "update", "b", data={"k": 2, "extra": True}).status_code == 201
    revoked = add(40, "add", "c", title="C").json()

    r = client.get(f"/api/processes/{pid}/replay")
    data = r.json()
    assert [s["step_key"] for s in data["state"]] == ["a", "b", "c"]
    assert data["state"][1]["data"] == {"k": 2, "extra": True}

    # 任意时刻：15s 时只有 a
    r = client.get(f"/api/processes/{pid}/replay", params={"at": t(15)})
    assert [s["step_key"] for s in r.json()["state"]] == ["a"]
    assert r.json()["counts"]["skipped"] == 3

    # 撤销 c 事件：撤销后重放结果不再含 c
    r = client.patch(f"/api/processes/{pid}/events/{revoked['id']}", json={"revoked": True})
    assert r.status_code == 200 and r.json()["revoked"] is True

    r = client.get(f"/api/processes/{pid}/replay")
    data = r.json()
    assert [s["step_key"] for s in data["state"]] == ["a", "b"]
    assert data["ignored"][0]["reason"] == "revoked"

    # 差异：15s -> 最新
    r = client.get(f"/api/processes/{pid}/diff", params={"from": t(15)})
    d = r.json()
    assert [s["step_key"] for s in d["added"]] == ["b"]


def test_invalid_timestamp_returns_400(client, pid):
    r = client.post(f"/api/processes/{pid}/events",
                    json={"kind": "add", "step_key": "x", "ts": "not-a-time"})
    assert r.status_code == 400
    r = client.get(f"/api/processes/{pid}/replay", params={"at": "nope"})
    assert r.status_code == 400


def test_export_json_and_csv(client, pid):
    rj = client.get(f"/api/processes/{pid}/export.json")
    assert rj.status_code == 200
    assert "state" in rj.json()
    assert "attachment" in rj.headers.get("content-disposition", "")

    rc = client.get(f"/api/processes/{pid}/export.csv")
    assert rc.status_code == 200
    assert "text/csv" in rc.headers["content-type"]
    assert "step_key" in rc.text


def test_event_and_process_lifecycle_404(client):
    assert client.get("/api/processes/999999").status_code == 404
    r = client.post("/api/processes", json={"name": "待删除", "version": "0.1"})
    p = r.json()["id"]
    e = client.post(f"/api/processes/{p}/events",
                    json={"kind": "add", "step_key": "x", "title": "X"}).json()
    assert client.delete(f"/api/processes/{p}/events/{e['id']}").status_code == 204
    assert client.delete(f"/api/processes/{p}").status_code == 204
    assert client.get(f"/api/processes/{p}").status_code == 404
