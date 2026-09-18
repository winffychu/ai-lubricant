#!/usr/bin/env bash
# 创空间（托管 Dockerfile 平台）单容器入口。
#
# 一个容器里用 supervisord 拉起全套：PostgreSQL + Redis + node-server +
# main（数据服务）+ tunnel-server。平台约束：
#   * 进程必须自己监听 7860（平台不做端口反向代理）—— 由 SERVER_PORT 驱动 main。
#   * 无 compose、无独立中间件容器 —— PG/Redis 装在镜像里，由本容器直接跑。
#   * 重启保留容器文件、重建清空 —— 本脚本与 native_deps 全程幂等：
#       重启时 runtime 目录在，initdb/建库/写 .env 全部跳过，PG 数据存活；
#       重建时目录不在，重新自举建库 + 种管理员。
#
# 幂等性由 native_deps 保证：initdb 看 PG_VERSION、ensure_database 存在即返回、
# ensure_security_keys append-if-missing、write_env_file 保留已存在键。
set -euo pipefail

# ---- 固定路径与端口 ----
export NATIVE_DEPS_ROOT="${NATIVE_DEPS_ROOT:-/opt/ailubricant/runtime}"
export DESKTOP_ENV_FILE="${DESKTOP_ENV_FILE:-/app/.env}"
export NATIVE_APP_ROOT="${NATIVE_APP_ROOT:-/app}"
# 数据服务对外监听 7860（创空间约定端口）。
export SERVER_PORT="${SERVER_PORT:-7860}"
# 控制面 / 穿透只监听容器内网，经穿透对外，不需要宿主映射。
export NODE_CONTROL_HOST="${NODE_CONTROL_HOST:-127.0.0.1}"
export NODE_CONTROL_PORT="${NODE_CONTROL_PORT:-8003}"
export AGENT_COMPOSE_BASE_URL="${AGENT_COMPOSE_BASE_URL:-http://127.0.0.1:8003}"
export TUNNEL_RUNTIME_ENABLED="${TUNNEL_RUNTIME_ENABLED:-true}"

cd "${NATIVE_APP_ROOT}"

# ---- 自举：provision（幂等）+ 生成 supervisord conf ----
# conf 路径由 native_deps.lifecycle.layout 决定：<NATIVE_DEPS_ROOT>/config/supervisord-native.conf。
# 先跑 supervisord-conf（同时完成 provision），再回读 stdout 校验（防止 layout 变更）。
python -m native_deps.cli supervisord-conf
conf_path="${NATIVE_DEPS_ROOT}/config/supervisord-native.conf"
if [ ! -f "${conf_path}" ]; then
  echo "[codespace] 未能生成 supervisord 配置：${conf_path}" >&2
  exit 1
fi
echo "[codespace] supervisord config: ${conf_path}"

# ---- 前台运行 supervisord，托管全部 program ----
exec supervisord -c "${conf_path}" -n
