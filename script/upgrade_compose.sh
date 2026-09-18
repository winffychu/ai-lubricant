#!/usr/bin/env bash
# upgrade_compose.sh —— 把已部署的 compose 实例升级到镜像仓库上的**最新发布**。
#
# 生产口径是 `latest`（见 docs/DOCKER_IMAGE.md 第二节：分支推送与 tag 推送都会移动它）。
# 只做四件事（**绝不做镜像构建**，镜像一律来自 GitHub Actions 的发布产物）：
#   1. docker pull <镜像>:<REF>          （REF 默认 latest）
#   2. docker tag  重新打成 compose 认的本地共享 tag（ai-lubricant:${TAG:-local}）
#   3. docker compose up -d --force-recreate   ← 关键：不加 --force-recreate 不会重建容器
#   4. 核验：容器确实跑在**刚拉下来的那个镜像**上，且健康检查通过
#
# 背景见 docs/DOCKER_IMAGE.md「升级已部署实例」：compose 判断是否重建看服务配置哈希，
# 不比对本地镜像 ID；只 retag 不强制重建，容器会继续跑旧镜像。
#
# 第 4 步为什么不比对版本号：`latest` 是**移动标签**，而镜像内 AI_LUBRICANT_VERSION 是
# 构建时写入的 `main-<7hex>`（分支推送）或 `vYYMMDD.N`（tag 推送）——它和字符串
# "latest" 永远不相等，拿它做等值断言必然误报失败。真正的不变量是
# 「运行容器的镜像 ID == 本地共享 tag 的镜像 ID」，这也正是「还是旧版本」故障的判据。
# 镜像内版本只作为信息打印，供人工确认当前跑的是哪次构建。
#
# 用法（在 docker-compose.yml 所在目录执行）：
#   bash script/upgrade_compose.sh                 # 升级到 latest（生产默认）
#   bash script/upgrade_compose.sh v260917.2       # 例外：临时钉住某个版本 tag
#   REF=sha256:<digest> bash script/upgrade_compose.sh   # 例外：按 digest 回滚
# 环境变量：
#   IMAGE     镜像仓库地址（默认 ghcr.io/wuxin-gh/ai-lubricant）
#   REF       目标 ref（默认 latest；位置参数优先）
#   TAG       compose 使用的本地共享 tag 名（默认 local，对应 ai-lubricant:local）
#   CONTAINER 数据服务容器名（默认 ai-lubricant）
#   WAIT      健康检查等待上限秒数（默认 120）
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/wuxin-gh/ai-lubricant}"
TAG="${TAG:-local}"
CONTAINER="${CONTAINER:-ai-lubricant}"
WAIT="${WAIT:-120}"

# 位置参数优先于环境变量；两者都没给则用生产口径 latest。
REF="${1:-${REF:-latest}}"

if ! docker compose version >/dev/null 2>&1; then
  echo "✗ 未检测到 docker compose（v2）。请在 docker-compose.yml 所在目录、且有 compose v2 的机器上运行。" >&2
  exit 2
fi

# digest 引用要用 `repo@sha256:...`，tag 引用用 `repo:tag`。
case "${REF}" in
  sha256:*) PULL_REF="${IMAGE}@${REF}" ;;
  *)        PULL_REF="${IMAGE}:${REF}" ;;
esac
LOCAL_REF="ai-lubricant:${TAG}"

echo "=== 1/4 拉取 ${PULL_REF} ==="
# 记录本地共享 tag 当前指向的镜像 ID，用于判断 latest 是否真的动了。
prev_id="$(docker image inspect --format '{{.Id}}' "${LOCAL_REF}" 2>/dev/null || true)"
docker pull "${PULL_REF}"
new_id="$(docker image inspect --format '{{.Id}}' "${PULL_REF}")"
echo "  镜像 ID: ${new_id:0:19}…"
if [ -n "${prev_id}" ] && [ "${prev_id}" = "${new_id}" ]; then
  echo "  ℹ ${LOCAL_REF} 已经是这份镜像（latest 自上次部署以来未移动）。"
  echo "    重建后容器仍运行同一镜像——若你期待的是「换了新版本」，先确认上游发布是否已跑完。"
fi

echo "=== 2/4 重新打成 ${LOCAL_REF} ==="
docker tag "${PULL_REF}" "${LOCAL_REF}"

echo "=== 3/4 强制重建容器（--force-recreate 不可省）==="
docker compose up -d --force-recreate

echo "=== 4/4 核验 ==="
# 4a. 运行容器必须就是刚拉下来的那份镜像 —— 这才是「升级成功」的硬判据。
run_id="$(docker inspect --format '{{.Image}}' "${CONTAINER}" 2>/dev/null || true)"
if [ -z "${run_id}" ]; then
  echo "✗ 未找到容器 ${CONTAINER}（compose 里的 container_name）。" >&2
  exit 1
fi
if [ "${run_id}" != "${new_id}" ]; then
  echo "  期望镜像: ${new_id}" >&2
  echo "  运行镜像: ${run_id}" >&2
  echo "✗ 升级失败：容器没有跑在刚拉取的镜像上（很可能容器未被重建）。" >&2
  echo "  回滚/重试：" >&2
  echo "    docker tag ${PULL_REF} ${LOCAL_REF} && docker compose up -d --force-recreate" >&2
  exit 1
fi
echo "  ✓ 容器镜像 ID 与拉取到的镜像一致"

# 4b. 健康检查（compose 里 ai-lubricant 服务带 healthcheck，指向 /mcp/health）。
echo "  等待健康检查（上限 ${WAIT}s）…"
deadline=$((SECONDS + WAIT))
health="unknown"
while [ "${SECONDS}" -lt "${deadline}" ]; do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${CONTAINER}" 2>/dev/null || echo missing)"
  case "${health}" in
    healthy) break ;;
    unhealthy|exited|dead|missing) break ;;
  esac
  sleep 3
done
echo "  健康状态: ${health}"
if [ "${health}" != "healthy" ]; then
  echo "✗ 升级失败：容器未达到 healthy（当前 ${health}）。" >&2
  echo "  查看日志：docker compose logs --tail 100 ${CONTAINER}" >&2
  exit 1
fi

# 4c. 信息性输出：镜像内版本标识（分支推送为 main-<7hex>，tag 推送为 vYYMMDD.N）。
#     注意它**不等于** ${REF}（latest 场景下本来就不该相等），仅用于人工确认。
echo "  --- 供人工确认（非判据）---"
echo "  请求的 ref        : ${REF}"
echo "  镜像内版本标识    : $(docker exec "${CONTAINER}" printenv AI_LUBRICANT_VERSION 2>/dev/null || echo '<空>')"
echo "  本地共享 tag      : ${LOCAL_REF}"
echo "  镜像 ID           : ${new_id}"
echo "✓ 升级完成：${LOCAL_REF} 已指向 ${PULL_REF}，容器已重建且 healthy。"
