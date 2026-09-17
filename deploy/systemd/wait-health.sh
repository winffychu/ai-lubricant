#!/bin/sh
# wait-health.sh —— 轮询本机健康端点，成功退 0、超时退 1。
#
# 被三个 service 单元的 ExecStartPost 调用（systemd 起服务后等它就绪，未就绪
# 则把 unit 标记为失败 → Restart=on-failure 接管）。
#
# 之所以独立成脚本而不是内联进 ExecStartPost：systemd 单元里 `$VAR` / `$(...)`
# 会被 systemd 自己先做变量展开，`${VAR:-default}` 这类 shell 默认值语法会被
# 吃成空串（写成 `$$` 转义可以，但整行可读性极差且易错）。脚本文件里没有这层
# 解释器，shell 语义就是 shell 语义。
#
# 用法：wait-health.sh <port> <path> [timeout_seconds]
#   例：wait-health.sh 8003 /health 120
set -eu

PORT="${1:?用法: wait-health.sh <port> <path> [timeout]}"
PATH_PART="${2:?用法: wait-health.sh <port> <path> [timeout]}"
TIMEOUT="${3:-120}"
HOST="${HEALTH_HOST:-127.0.0.1}"

[ -n "$PORT" ] || exit 1
URL="http://$HOST:$PORT$PATH_PART"
INTERVAL=2
TRIES=$((TIMEOUT / INTERVAL))
[ "$TRIES" -gt 0 ] || TRIES=1

i=0
while [ "$i" -lt "$TRIES" ]; do
  if curl -sf --max-time 3 "$URL" >/dev/null 2>&1; then
    exit 0
  fi
  i=$((i + 1))
  sleep "$INTERVAL"
done

echo "wait-health: 超时未就绪 $URL（${TIMEOUT}s）" >&2
exit 1
