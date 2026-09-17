#!/usr/bin/env bash
# server_upgrade_bootstrap.sh —— 部署机一次性安装：把裸跑/手工目录迁到
# releases + current + shared 布局，并装好 systemd 单元。
#
# 做四件事（幂等，可重复跑）：
#   1. 建目录骨架：/opt/ai-lubricant/{releases,shared/{data,logs,...}}、/var/lib/alb
#   2. 迁移运行时数据：现有 data/ attachments/ logs/ deleted_backups/ mc-tunnel-bins
#      移到 shared/（已存在则跳过，不覆盖）
#   3. 生成 shared/alb.env（数据目录覆盖 + APP_VERSION）与 shared/.env 软链
#   4. 装 systemd 单元（alb-node/main/tunnel + alb-upgrade.path/.timer）并 enable
#
# 首个 release：用 --repo-url + --tag 指定（默认取当前工作目录的 origin 与
# 当前 HEAD 所在 tag），clone 到 releases/<tag> 后翻 current 指针。
#
# 用法（部署机 root）：
#   bash server_upgrade_bootstrap.sh --tag v260912 [--repo-url <url>] [--source-dir <现有代码目录>]
#   bash server_upgrade_bootstrap.sh --tag v260912 --dry-run    # 只打印将做什么
#
# 前提：Linux + systemd + git + python3（含 venv 模块）+ curl。
set -euo pipefail

ROOT="${ALB_ROOT:-/opt/ai-lubricant}"
STATE_DIR="${SERVER_UPGRADE_STATE_DIR:-/var/lib/alb}"
RELEASES_DIR="$ROOT/releases"
SHARED_DIR="$ROOT/shared"
CURRENT_LINK="$ROOT/current"
UNITS_SRC="${ALB_UNITS_SRC:-$(cd "$(dirname "$0")/../deploy/systemd" && pwd)}"
UNITS_DST="/etc/systemd/system"
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
DRY_RUN="${DRY_RUN:-0}"

TAG=""
REPO_URL=""
SOURCE_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --source-dir) SOURCE_DIR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

_log() { echo "[bootstrap] $*"; }
_do() { if [ "$DRY_RUN" = "1" ]; then _log "[dry-run] $*"; else "$@"; fi; }

[ "$(id -u)" = "0" ] || { echo "需要 root（装 systemd 单元 + 写 /opt）" >&2; exit 1; }

# ── 1. 目录骨架 ───────────────────────────────────────────────────────────
_log "建目录骨架：$ROOT"
for d in "$RELEASES_DIR" "$SHARED_DIR" "$SHARED_DIR/data" "$SHARED_DIR/logs" \
         "$SHARED_DIR/attachments" "$SHARED_DIR/deleted_backups" "$SHARED_DIR/mc-tunnel-bins" \
         "$STATE_DIR"; do
  _do mkdir -p "$d"
done

# ── 2. 迁移现有运行时数据（不覆盖已有）────────────────────────────────────
# 现有裸跑布局里这些目录在代码根下；迁到 shared 后跨 release 存续。
if [ -n "$SOURCE_DIR" ] && [ -d "$SOURCE_DIR" ]; then
  _log "迁移运行时数据：$SOURCE_DIR → $SHARED_DIR"
  _migrate() {  # $1 = 源子目录 $2 = 目标
    local src="$SOURCE_DIR/$1" dst="$2"
    if [ -e "$src" ] && [ ! -e "$dst" ]; then
      _do cp -a "$src" "$dst"
      _log "  ✓ $1 → $dst"
    elif [ -e "$dst" ]; then
      _log "  · 跳过 $1（$dst 已存在）"
    fi
  }
  _migrate data "$SHARED_DIR/data"
  _migrate agent/attachments "$SHARED_DIR/attachments"
  _migrate logs "$SHARED_DIR/logs"
  _migrate deleted_backups "$SHARED_DIR/deleted_backups"
  # .env 是真身迁到 shared；代码目录里的留作软链源
  if [ -f "$SOURCE_DIR/.env" ] && [ ! -f "$SHARED_DIR/.env" ]; then
    _do cp -a "$SOURCE_DIR/.env" "$SHARED_DIR/.env"
    _log "  ✓ .env → $SHARED_DIR/.env"
  fi
else
  _log "未指定 --source-dir，跳过数据迁移（新部署可忽略）"
fi

# ── 3. 生成 shared/alb.env（systemd EnvironmentFile）──────────────────────
# 运行时数据目录覆盖：让 release 目录可随升级 GC，状态落在 shared。
ALB_ENV="$SHARED_DIR/alb.env"
if [ ! -f "$ALB_ENV" ]; then
  _log "生成 $ALB_ENV"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] 写 alb.env（数据目录覆盖 + APP_VERSION）"
  else
    cat > "$ALB_ENV" <<EOF
# 由 server_upgrade_bootstrap.sh 生成；APP_VERSION 行由 server_updater.sh 维护。
# 运行时数据目录全部指到 shared/，release 目录随升级 GC 不影响状态。
GENERIC_AGENT_MEMORY_ROOT=$SHARED_DIR/data/agents
GENERIC_AGENT_LEGACY_MEMORY_ROOT=$SHARED_DIR/data/generic-agent-memory
GENERIC_AGENT_SOP_SOURCE_ROOT=$SHARED_DIR/data/agent-sops
AGENT_RESOURCE_STORE=$SHARED_DIR/data/agent-resources
ATTACHMENTS_ROOT=$SHARED_DIR/attachments
DELETED_BACKUPS_DIR=$SHARED_DIR/deleted_backups
LOG_DIR=$SHARED_DIR/logs
MC_TUNNEL_BIN_DIR=$SHARED_DIR/mc-tunnel-bins
APP_VERSION=${TAG:-dev}
EOF
    chmod 600 "$ALB_ENV"
  fi
else
  _log "$ALB_ENV 已存在，保留（只更新 APP_VERSION 由 updater 负责）"
fi

# ── 4. 首个 release：clone + venv + 翻指针 ────────────────────────────────
if [ -n "$TAG" ]; then
  [ -n "$REPO_URL" ] || { echo "--tag 需要配合 --repo-url" >&2; exit 1; }
  target_dir="$RELEASES_DIR/$TAG"
  if [ -d "$target_dir" ]; then
    _log "release $TAG 已存在，跳过 clone"
  else
    _log "clone $REPO_URL @ $TAG → $target_dir"
    _do git clone --depth 1 --branch "$TAG" --no-tags "$REPO_URL" "$target_dir"
    _do git -C "$target_dir" submodule update --init --depth 1 node_server user-frontend
  fi
  if [ ! -x "$target_dir/venv/bin/python" ]; then
    _log "建 venv + 装依赖（$target_dir/venv）"
    _do python3 -m venv "$target_dir/venv"
    _do "$target_dir/venv/bin/pip" install -q -r "$target_dir/requirements.txt" -i "$PIP_INDEX"
  fi
  _do ln -sfn "$SHARED_DIR/.env" "$target_dir/.env"
  _log "翻转 current → releases/$TAG"
  if [ "$DRY_RUN" != "1" ]; then
    ln -sfn "releases/$TAG" "$CURRENT_LINK.new"
    mv -T "$CURRENT_LINK.new" "$CURRENT_LINK"
  fi
else
  _log "未指定 --tag，跳过首个 release（装完单元后手动 clone 再跑）"
fi

# ── 5. 装 systemd 单元 + 健康探针脚本 ─────────────────────────────────────
if [ ! -d "$UNITS_SRC" ]; then
  echo "找不到单元源目录：$UNITS_SRC（用 ALB_UNITS_SRC 指定）" >&2
  exit 1
fi
# 健康探针装到 shared/（单元里的 ExecStartPost 引用它）：放 shared 而非 release，
# 保证任何 release 状态下都在——放 current/ 的话某版缺这文件会让服务起不来。
if [ -f "$UNITS_SRC/wait-health.sh" ]; then
  _log "安装健康探针：$SHARED_DIR/wait-health.sh"
  _do cp "$UNITS_SRC/wait-health.sh" "$SHARED_DIR/wait-health.sh"
  _do chmod 755 "$SHARED_DIR/wait-health.sh"
fi
_log "安装 systemd 单元：$UNITS_SRC → $UNITS_DST"
for u in alb-node.service alb-main.service alb-tunnel.service \
         alb-upgrade.service alb-upgrade.path alb-upgrade.timer; do
  if [ -f "$UNITS_SRC/$u" ]; then
    _do cp "$UNITS_SRC/$u" "$UNITS_DST/$u"
  else
    echo "缺单元文件：$UNITS_SRC/$u" >&2
  fi
done
_do systemctl daemon-reload
_do systemctl enable --now alb-node.service alb-main.service alb-tunnel.service
_do systemctl enable --now alb-upgrade.path alb-upgrade.timer

_log "完成。核对："
_log "  systemctl status alb-node alb-main alb-tunnel"
_log "  systemctl list-timers alb-upgrade.timer"
_log "  curl -s http://127.0.0.1:8001/api/v1/server/config | grep current_version"
