# Data Quality Loop — FastAPI 接入层镜像
# 对齐电商系统 Docker 标准: python:3.12-slim + 阿里云镜像 + requirements.txt + uvicorn

FROM python:3.12-slim

# 国内镜像源（加速依赖下载）
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set global.trusted-host mirrors.aliyun.com

# 健康检查用 curl
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖先装（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码（数据目录 data/ 用 volume 持久化，不 COPY）
COPY src ./src
COPY configs ./configs
COPY scripts ./scripts
COPY eval ./eval
COPY skills ./skills

# 数据目录：被质检数仓 + SQLite 审计（挂载卷持久化）
RUN mkdir -p /app/data

# 让 `python -m data_quality_loop...` / uvicorn 能找到包
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8500

# 启动 FastAPI（api/app.py 内部已把 ROOT 与 src 加进 sys.path）
CMD ["uvicorn", "data_quality_loop.api.app:app", "--host", "0.0.0.0", "--port", "8500"]
