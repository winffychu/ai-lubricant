#!/usr/bin/env bash
# upgrade_compose.sh —— 把已部署的 compose 实例升级到指定版本。
#
# 只做四件事（**绝不做镜像构建**，镜像一律来自 GitHub Actions 的发布产物）：
#   1. docker pull <镜像>:<VERSION>
#   2. docker tag  重新打成 compose 认的本地共享 tag（ai-lubricant:${TAG:-local}）
#   3. docker compose up -d --force-recreate   ← 关键：不加 --force-recreate 不会重建容器
#   4. 核验容器内 AI_LUBRICANT_VERSION 是否等于 $VERSION，不等则退出非 0 并打印回滚命令
#
# 背景见 docs/DEPLOY.md「升级（已部署实例换新版本）」：
# compose 判断是否重建看服务配置哈希，不比对本地镜像 ID；只 retag 不强制重建，
# 容器会继续跑旧镜像。
#
# 用法（在 docker-compose.yml 所在目录执行）：
#   bash script/upgrade_compose.sh v260917.1
#   IMAGE=ghcr.io/winffychu/ai-lubricant bash script/upgrade_compose.sh v260917.1
# 环境变量：
#   IMAGE    镜像仓库地址（默认 ghcr.io/wuxin-gh/ai-lubricant）
#   TAG      compose 使用的本地共享 tag 名（默认 local，对应 ai-lubricant:local）
#   CONTAINER 数据服务容器名（默认 ai-lubricant）
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/wuxin-gh/ai-lubricant}"
TAG="${TAG:-local}"
CONTAINER="${CONTAINER:-ai-lubricant}"

VERSION="${1:-}"
if [ -z "${VERSION}" ]; then
  echo "用法: bash script/upgrade_compose.sh <VERSION>（如 v260917.1）" >&2
  exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "✗ 未检测到 docker compose（v2）。请在 docker-compose.yml 所在目录、且有 compose v2 的机器上运行。" >&2
  exit 2
fi

REF="${IMAGE}:${VERSION}"
LOCAL_REF="ai-lubricant:${TAG}"

echo "=== 1/4 拉取 ${REF} ==="
docker pull "${REF}"

echo "=== 2/4 重新打成 ${LOCAL_REF} ==="
docker tag "${REF}" "${LOCAL_REF}"

echo "=== 3/4 强制重建容器（--force-recreate 不可省）==="
docker compose up -d --force-recreate

echo "=== 4/4 核验版本 ==="
actual="$(docker exec "${CONTAINER}" printenv AI_LUBRICANT_VERSION 2>/dev/null || true)"
echo "  期望: ${VERSION}"
echo "  实际: ${actual:-<空>}"
if [ "${actual}" != "${VERSION}" ]; then
  echo "✗ 升级失败：容器内 AI_LUBRICANT_VERSION 与目标版本不一致。" >&2
  echo "  回滚到上一个版本：" >&2
  echo "    docker tag ${IMAGE}:<上一个版本> ${LOCAL_REF} && docker compose up -d --force-recreate" >&2
  exit 1
fi
echo "✓ 升级完成：${LOCAL_REF} = ${VERSION}"
