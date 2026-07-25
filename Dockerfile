FROM docker.m.daocloud.io/node:22-alpine AS frontend-build
WORKDIR /build/frontend
RUN npm config set registry https://registry.npmmirror.com
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM docker.m.daocloud.io/python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8081 \
    DATABASE_URL=sqlite:////data/app.sqlite3 \
    WORKSPACE_ROOT=/data/workspaces \
    RUNTIME_DIR=/data

RUN set -eux; \
    sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \
    sed -i 's|https://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i 's|https://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md AGENTS.md ./
COPY app ./app
RUN python -m pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

COPY --from=frontend-build /build/frontend/dist ./frontend/dist

RUN mkdir -p /data/workspaces /data/logs /data/memory /data/skills
VOLUME ["/data"]
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
