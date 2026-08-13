# ── Stage 1: 基础系统依赖 ──
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Stage 2: Python 依赖 ──
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright 浏览器及系统依赖（Chromium，用于 RPA）
RUN playwright install-deps chromium 2>/dev/null || true
RUN playwright install chromium 2>/dev/null || true

# ── Stage 3: 运行时 ──
FROM deps AS runtime

# 安装 Docker CLI（代码执行沙箱通过宿主机 Docker socket 通信）
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

COPY src/ ./src/

# ⚠️ 不要 COPY .env 到镜像 — 密钥应通过 docker-compose env_file 或 K8s Secrets 注入
# 构建时如需默认值，通过 ARG 传入（docker build --build-arg LLM_API_KEY=xxx）

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["python", "src/main.py"]
