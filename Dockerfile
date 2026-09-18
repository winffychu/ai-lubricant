# syntax=docker/dockerfile:1.7

# Docker Hub 基础镜像前缀，供国内部署走加速器（compose 经 build.args 注入，
# 取值来自 MIRROR_MODE=cn 的 DOCKER_REGISTRY_PREFIX）。默认空 = 直连 Docker Hub。
ARG DOCKER_REGISTRY_PREFIX=
FROM ${DOCKER_REGISTRY_PREFIX}python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 复制依赖文件
COPY requirements.txt .

# 安装依赖（默认走清华镜像；海外构建可 --build-arg PIP_INDEX_URL=https://pypi.org/simple）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt -i "${PIP_INDEX_URL}"

# 复制应用代码
COPY . .

# 版本号烧进镜像：compose 用 TAG 传（--build-arg APP_VERSION=$TAG），
# /api/v1/server/config 的 current_version 与升级卡片的「当前运行版本」都读它。
# 不烧的话恒为 dev——升级完成后无法回答「现在跑的是哪版」。
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# 暴露端口：8001 数据服务 / 8003 节点控制面 / 8004 Tunnel Runtime
# （同一镜像承载三个入口，由 docker-compose 按服务分配 command）
EXPOSE 8001 8003 8004

# 启动命令
CMD ["python", "main.py"]
