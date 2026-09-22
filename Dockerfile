FROM python:3.12-slim

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/app/data/replay.db \
    PORT=8000

EXPOSE 8000

# 启动时由 FastAPI lifespan 自动完成数据库初始化
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
