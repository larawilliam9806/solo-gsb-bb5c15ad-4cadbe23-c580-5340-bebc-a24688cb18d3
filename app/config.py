"""运行配置，全部通过环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# SQLite 数据库文件路径
DB_PATH = Path(os.getenv("DB_PATH", "/data/replay.db"))

# uvicorn 监听配置
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# 启动时是否自动写入一份演示数据流程
SEED_ON_STARTUP = _bool("SEED_ON_STARTUP", False)

# 允许上传的 step data JSON 大小（字节），仅用于基本防护
MAX_DATA_BYTES = int(os.getenv("MAX_DATA_BYTES", str(64 * 1024)))


def ensure_dirs() -> None:
    """确保数据库目录存在。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
