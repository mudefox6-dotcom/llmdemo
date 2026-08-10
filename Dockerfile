FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 软件源可在构建时替换：默认走官方源保证在任何环境都能构建，
# 部署到国内服务器时用 --build-arg 注入就近镜像（见 docker-compose.prod.yml）。
# 实测腾讯云内网镜像比 deb.debian.org 快 10 倍（902 KB/s vs 84 KB/s）。
ARG APT_MIRROR=""
ARG PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"

# Debian 13(trixie) 用 deb822 格式的 debian.sources，旧版本用 sources.list，两个都替换
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip -i "$PIP_INDEX" \
    && pip install -r requirements.txt -i "$PIP_INDEX"

COPY . .

EXPOSE 8000

# 健康检查用 Python 而非 curl，这样镜像里不必额外装 curl
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
