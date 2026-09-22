"""演示数据：覆盖乱序到达与事件撤销两种典型重放场景。"""
from __future__ import annotations

from datetime import timedelta

from . import db
from .replay import now_iso, parse_ts

SEED_NAME = "粒子加速实验（演示）"
SEED_VERSION = "1.0.0"


def seed_demo(conn) -> dict:
    """幂等创建演示流程；已存在同名校验版本时抛 LookupError。"""
    if db.find_process(conn, SEED_NAME, SEED_VERSION) is not None:
        raise LookupError("demo process already exists")

    created = now_iso()
    pid = db.create_process(conn, SEED_NAME, SEED_VERSION,
                            "追加顺序与时间戳故意乱序，且含一条被撤销的事件", created)

    base = parse_ts(created)

    def t(seconds: float) -> str:
        return (base + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")

    # 注意：ts 与追加顺序刻意不一致 —— 展示乱序重放
    db.append_event(conn, pid, t(0), "add", "inject", "粒子注入",
                    {"voltage_kv": 12.5, "beam": "proton"}, created)
    db.append_event(conn, pid, t(20), "add", "accelerate", "一级加速",
                    {"gradient": 32}, created)
    db.append_event(conn, pid, t(10), "add", "focus", "束流聚焦",  # 晚到：ts 位于前两条之间
                    {"magnet": "F1"}, created)
    db.append_event(conn, pid, t(20), "update", "accelerate", "一级加速",  # 同刻事件，按 seq
                    {"gradient": 35.2}, created)
    db.append_event(conn, pid, t(40), "add", "calibrate", "探测器校准",
                    {"detector": "D0"}, created, revoked=True)  # 被撤销，不参与重放
    db.append_event(conn, pid, t(30), "remove", "focus", "束流聚焦", {}, created)  # 撤销某步骤
    db.append_event(conn, pid, t(50), "add", "collide", "对撞采集",
                    {"luminosity": 1.8e34}, created)

    conn.commit()
    return {"id": pid, "name": SEED_NAME, "version": SEED_VERSION}
