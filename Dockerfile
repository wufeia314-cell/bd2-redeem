# BD2 礼包码自动兑换系统 - 生产镜像
FROM python:3.11-slim

WORKDIR /app

# 系统依赖（httpx/uvicorn 无需编译，保持精简）
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 数据库目录必须存在且可写（get_conn 也会自动 makedirs，这里显式建好更稳）
RUN mkdir -p /app/data

ENV BD2_HOST=0.0.0.0 \
    BD2_PORT=8000 \
    BD2_DB_PATH=/app/data/bd2.db \
    BD2_ADMIN_TOKEN=change-me-admin-token \
    BD2_FETCH_ENABLED=1 \
    BD2_FETCH_INTERVAL_MIN=30

EXPOSE 8000

CMD ["python", "backend/run.py"]
