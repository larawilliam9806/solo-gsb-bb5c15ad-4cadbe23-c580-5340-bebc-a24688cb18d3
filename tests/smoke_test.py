"""API 冒烟测试：乱序重放、撤销、时刻状态、差异、导出。"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main() -> None:
    with TestClient(app) as c:
        # 创建带版本号的流程
        r = c.post("/api/flows", json={"name": "蛋白纯化", "version": "v1.0"})
        assert r.status_code == 201, r.text
        flow = r.json()
        fid = flow["id"]
        assert flow["version"] == "v1.0"

        # 乱序追加：t2 先到，t1 后到
        e2 = c.post(f"/api/flows/{fid}/events", json={
            "step_id": "s1", "event_type": "completed",
            "event_time": "2026-09-22T10:05:00Z", "seq": 2}).json()
        e1 = c.post(f"/api/flows/{fid}/events", json={
            "step_id": "s1", "event_type": "started",
            "event_time": "2026-09-22T10:00:00Z", "seq": 1}).json()
        e3 = c.post(f"/api/flows/{fid}/events", json={
            "step_id": "s2", "event_type": "started",
            "event_time": "2026-09-22T10:10:00Z", "seq": 3,
            "payload": {"temp": 80}}).json()

        # 回放顺序应按业务时间排序，与到达顺序相反
        replay_order = c.get(f"/api/flows/{fid}/events?order=replay").json()
        assert [e["id"] for e in replay_order] == [e1["id"], e2["id"], e3["id"]]

        # 时刻状态：10:03 时 s1 为 running，s2 不存在
        st = c.get(f"/api/flows/{fid}/replay", params={"at": "2026-09-22T10:03:00Z"}).json()
        assert len(st["steps"]) == 1 and st["steps"][0]["status"] == "running"

        # 撤销 e2（completed），最终状态 s1 回到 running
        undo = c.post(f"/api/flows/{fid}/events", json={
            "step_id": "s1", "event_type": "undone",
            "event_time": "2026-09-22T10:20:00Z",
            "target_event_id": e2["id"]})
        assert undo.status_code == 201, undo.text
        final = c.get(f"/api/flows/{fid}/replay").json()
        s1 = next(s for s in final["steps"] if s["step_id"] == "s1")
        assert s1["status"] == "running", final
        assert final["cancelled_event_ids"] == [e2["id"]]

        # 差异：10:03 -> 10:15，s2 新增
        d = c.get(f"/api/flows/{fid}/diff", params={
            "from": "2026-09-22T10:03:00Z", "to": "2026-09-22T10:15:00Z"}).json()
        assert [s["step_id"] for s in d["added"]] == ["s2"]
        assert any(ch["step_id"] == "s1" and ch["status"]["to"] == "completed"
                   for ch in d["changed"])  # 10:15 时 e2 尚未被撤销

        # 导出
        exp = c.get(f"/api/flows/{fid}/export")
        assert exp.status_code == 200
        assert "attachment" in exp.headers["content-disposition"]
        body = exp.json()
        assert body["flow"]["id"] == fid
        assert len(body["snapshots"]) >= 3
        assert body["final_state"]["cancelled_event_ids"] == [e2["id"]]

        # 校验：undone 缺 target -> 422；坏时间 -> 422
        bad = c.post(f"/api/flows/{fid}/events", json={
            "step_id": "x", "event_type": "undone"})
        assert bad.status_code == 422
        bad2 = c.post(f"/api/flows/{fid}/events", json={
            "step_id": "x", "event_type": "started", "event_time": "not-a-time"})
        assert bad2.status_code == 422

        # Web 界面
        assert c.get("/").status_code == 200

    print("全部冒烟测试通过 ✔")


if __name__ == "__main__":
    main()
