#!/bin/sh
# 容器启动入口：完成数据库初始化（建表/迁移）后启动服务。
set -e

echo "[entrypoint] initializing database at ${DB_PATH:-/data/replay.db} ..."
python -m app.initdb

echo "[entrypoint] starting uvicorn on ${HOST:-0.0.0.0}:${PORT:-8000} ..."
exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
