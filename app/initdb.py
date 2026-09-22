"""数据库初始化脚本：容器启动时执行（也可手动 python -m app.initdb）。"""
from __future__ import annotations

from . import db
from .config import DB_PATH
from .seed import seed_demo
from .config import SEED_ON_STARTUP


def main() -> None:
    db.init_db()
    print(f"[initdb] schema ready at {DB_PATH}")
    if SEED_ON_STARTUP:
        conn = db.connect()
        try:
            try:
                info = seed_demo(conn)
                print(f"[initdb] demo process ready: {info['name']} @{info['version']} (id={info['id']})")
            except LookupError:
                print("[initdb] demo process already exists, skipped")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
