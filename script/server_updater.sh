#!/usr/bin/env bash
# server_updater.sh —— 服务端一键升级的宿主侧执行器（root，独立 cgroup）。
#
# 由 alb-upgrade.path（标记文件变更即触发）或 alb-upgrade.timer（30s 兜底）拉起，
# ExecStart 先把本脚本 cp 到 /tmp 再执行，防翻指针时自我替换竞态。
#
# 职责：认领标记 → venv/pip 装依赖 → 翻 current 软链 → 顺序重启三服务 →
#       健康验证 → 失败回切旧版 → GC 旧 release。全程写 upgrade_state.json 供
#       管理端轮询（cloning/clone_done 由 web 写，installing 之后由本脚本写）。
#
# 切换铁律（DEPLOY.md）：控制面（alb-node）先行健康，再动主服务（alb-main）。
# 断电矩阵：venv/pip 中断电 → current 仍在旧版，开机起完整旧版；翻指针后断电 →
# 开机起完整新版（venv 已装毕），自愈分支（target==current）补写 env + 重启收敛。
#
# 仅支持 Linux（GNU coreutils + systemd）：用到 `mv -T`、`flock`、`ln -sfn`、
# systemctl。部署机即 Linux，不需要 BSD/macOS 兼容。
#
# 用法：
#   bash server_updater.sh            # 正常一轮（由 path/timer 拉起）
#   DRY_RUN=1 bash server_updater.sh  # 空跑：只读标记不认领、不装依赖不重启，
#                                     # 打印将要做什么（部署机预检用）
set -euo pipefail

# ── 配置（env 可覆盖）──────────────────────────────────────────────────────
STATE_DIR="${SERVER_UPGRADE_STATE_DIR:-/var/lib/alb}"
RELEASES_DIR="${SERVER_RELEASES_DIR:-/opt/ai-lubricant/releases}"
CURRENT_LINK="${SERVER_CURRENT_LINK:-/opt/ai-lubricant/current}"
SHARED_DIR="${SERVER_SHARED_DIR:-/opt/ai-lubricant/shared}"
LOG_FILE="${SERVER_UPGRADE_LOG:-$SHARED_DIR/logs/upgrade.log}"
ALB_ENV="$SHARED_DIR/alb.env"
NODE_SVC="${ALB_NODE_SERVICE:-alb-node}"
MAIN_SVC="${ALB_MAIN_SERVICE:-alb-main}"
TUNNEL_SVC="${ALB_TUNNEL_SERVICE:-alb-tunnel}"
MAIN_PORT="${ALB_MAIN_PORT:-8001}"
NODE_PORT="${ALB_NODE_PORT:-8003}"
TUNNEL_PORT="${ALB_TUNNEL_PORT:-8004}"
PIP_INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
HEALTH_HOST="${SERVER_UPGRADE_HEALTH_HOST:-127.0.0.1}"
HEALTH_TIMEOUT="${SERVER_UPGRADE_HEALTH_TIMEOUT:-60}"   # 单端点等待秒数
PIP_TIMEOUT="${SERVER_UPGRADE_PIP_TIMEOUT:-600}"         # pip install 总超时
DRY_RUN="${DRY_RUN:-0}"

REQ="$STATE_DIR/upgrade_target"
LOCK="/run/alb-upgrade.lock"

mkdir -p "$STATE_DIR" "$(dirname "$LOG_FILE")" 2>/dev/null || true

# ── 工具函数 ──────────────────────────────────────────────────────────────

_log() { echo "$(date '+%F %T') $*" >> "$LOG_FILE" 2>/dev/null || echo "$*"; }

# 执行命令；DRY_RUN=1 时只记录不执行（危险动作全部走这里）
_do() {
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] $*"
    return 0
  fi
  "$@"
}

_write_state() {  # phase [target] [error]
  local phase="$1" target="${2:-}" error="${3:-}"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] state: phase=$phase target=$target error=$error"
    return 0
  fi
  # 状态写失败绝不能中断升级流程本身（UI 轮询降级为 idle，不影响切换正确性）
  python3 - "$STATE_DIR/upgrade_state.json" "$phase" "$target" "$error" <<'PY' || _log "⚠ 状态写入失败"
import json, datetime, os, sys, tempfile
path, phase, target, error = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
state = {"phase": phase, "target": target, "error": (error or "")[:500]}
try:
    old = json.load(open(path, encoding="utf-8"))
    if isinstance(old, dict) and old.get("started_at"):
        state["started_at"] = old["started_at"]
except Exception:
    pass
state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".upgrade_state.", suffix=".json")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(state, fh, ensure_ascii=False)
os.replace(tmp, path)
PY
}

_current_release() {
  local link
  link=$(readlink "$CURRENT_LINK" 2>/dev/null) || return 0
  [ -n "$link" ] || return 0
  basename "$link"
}

_refresh_app_version() {  # $1 = tag
  local tag="$1" tmp
  if [ ! -f "$ALB_ENV" ]; then
    _log "[dry-run] 将新建 $ALB_ENV（APP_VERSION=$tag）"
    [ "$DRY_RUN" = "1" ] && return 0
    echo "APP_VERSION=$tag" > "$ALB_ENV"
    return
  fi
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] 将把 $ALB_ENV 的 APP_VERSION 改为 $tag"
    return 0
  fi
  tmp=$(mktemp "$ALB_ENV.XXXXXX")
  if grep -q '^APP_VERSION=' "$ALB_ENV" 2>/dev/null; then
    sed "s|^APP_VERSION=.*|APP_VERSION=$tag|" "$ALB_ENV" > "$tmp"
  else
    cp "$ALB_ENV" "$tmp"
    echo "APP_VERSION=$tag" >> "$tmp"
  fi
  mv -T "$tmp" "$ALB_ENV"
}

_restart_services() {
  _log "重启服务：$NODE_SVC → $MAIN_SVC + $TUNNEL_SVC"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] systemctl restart $NODE_SVC; 等 :$NODE_PORT/health; systemctl restart $MAIN_SVC $TUNNEL_SVC"
    return 0
  fi
  systemctl restart "$NODE_SVC" || { _log "✗ $NODE_SVC 重启失败"; return 1; }
  # 控制面先行健康（切换铁律：绝不让两个 Registry 同时下发；重建=停旧起新）
  local i=0
  until curl -sf "http://$HEALTH_HOST:$NODE_PORT/health" >/dev/null 2>&1; do
    i=$((i+1)); [ "$i" -ge "$HEALTH_TIMEOUT" ] && { _log "✗ $NODE_SVC 健康超时"; return 1; }
    sleep 2
  done
  systemctl restart "$MAIN_SVC" "$TUNNEL_SVC" || { _log "✗ 主服务重启失败"; return 1; }
}

_verify_health() {
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] 将轮询 :$MAIN_PORT/mcp/health 与 :$TUNNEL_PORT/ready（各 ${HEALTH_TIMEOUT}s）"
    return 0
  fi
  sleep 5   # 给 uvicorn 起时间
  local url i
  for url in "http://$HEALTH_HOST:$MAIN_PORT/mcp/health" "http://$HEALTH_HOST:$TUNNEL_PORT/ready"; do
    i=0
    until curl -sf "$url" >/dev/null 2>&1; do
      i=$((i+1))
      if [ "$i" -ge "$HEALTH_TIMEOUT" ]; then
        _log "✗ 健康验证超时: $url"
        return 1
      fi
      sleep 2
    done
  done
  return 0
}

_rollback() {  # $1 = target $2 = old $3 = reason
  local target="$1" old="$2" reason="$3"
  _log "回滚：$target → $old（$reason）"
  if [ -n "$old" ] && [ -d "$RELEASES_DIR/$old" ]; then
    if [ "$DRY_RUN" = "1" ]; then
      _log "[dry-run] 将回切 current → $old 并重启三服务"
      return 0
    fi
    ln -sfn "releases/$old" "$CURRENT_LINK.new" && mv -T "$CURRENT_LINK.new" "$CURRENT_LINK"
    _refresh_app_version "$old"
    systemctl restart "$NODE_SVC" 2>/dev/null || true
    sleep 3
    systemctl restart "$MAIN_SVC" "$TUNNEL_SVC" 2>/dev/null || true
    _write_state failed "$target" "已回滚到 $old：$reason"
  else
    # 旧版目录不在（异常）：留 current 在新版，标 failed，人工介入
    _write_state failed "$target" "回滚失败（旧版 $old 不存在）：$reason"
    _log "✗✗ 回滚失败，旧版 $old 不存在，current 留在 $target，需人工介入"
  fi
}

_gc_releases() {  # $1 = current tag；保留 current + 最近 2 个
  local keep="$1" n=0 d name
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    name=$(basename "$d")
    [ "$name" = "$keep" ] && continue
    n=$((n+1))
    if [ "$n" -gt 2 ]; then
      _do rm -rf -- "$d"
      _log "GC 删除旧 release：$name"
    fi
  done < <(ls -1dt "$RELEASES_DIR"/*/ 2>/dev/null | sed 's|/$||' || true)
}

_cleanup_claimed() { rm -f -- "$1" 2>/dev/null || true; }

# 删半成品 release 目录（clone 失败/预检失败留下的残骸），失败不阻断
_safe_rmtree() {  # $1 = 目录
  rm -rf -- "$1" 2>/dev/null || true
}

# 标记是 JSON（web 统一契约）：{"target": "<tag>", "proxy_url": "...", "repo_url": "..."}。
# 与 docker_updater.sh 读同一份格式——两个执行器形态不同但契约一致。
_marker_field() {  # $1=文件 $2=键
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    v = d.get(sys.argv[2])
except Exception:
    v = None
print(v if isinstance(v, str) else "")
PY
}

# ── 主流程 ────────────────────────────────────────────────────────────────

# 单飞：上一轮还在跑（pip/restart 耗时）就闪人，timer 兜底
if [ "$DRY_RUN" != "1" ]; then
  exec 9>"$LOCK"
  flock -n 9 || { _log "上一轮升级仍在进行，跳过"; exit 0; }
fi

[ -f "$REQ" ] || exit 0   # 无标记，完

# 认领标记：正常模式 mv 走（升级期间 web 写的新请求落新文件，timer 下一轮兜走）；
# dry-run 只读不认领，避免把真实升级请求吃掉。
claimed="$REQ.claimed-$$"
if [ "$DRY_RUN" = "1" ]; then
  _log "[dry-run] 读取标记 $REQ（不认领）"
  claimed="$REQ"
else
  mv "$REQ" "$claimed" 2>/dev/null || exit 0   # 已被并发实例认领
fi
target=$(_marker_field "$claimed" target)
proxy_url=$(_marker_field "$claimed" proxy_url)
repo_url=$(_marker_field "$claimed" repo_url)
old=$(_current_release)

# 标记内容必须是合法 tag（与 web 端 _TAG_RE 同口径），否则丢弃
case "$target" in
  "" | *[!A-Za-z0-9._/-]*)
    _write_state failed "$target" "标记内容非法"
    [ "$DRY_RUN" = "1" ] || _cleanup_claimed "$claimed"
    exit 0 ;;
esac

_log "── 升级请求：$old → $target（dry-run=$DRY_RUN）"

# 自愈分支：target == 当前指针 → 只补 env + 重启（断电后指针已翻但 env 没写）
if [ -n "$old" ] && [ "$target" = "$old" ]; then
  _log "自愈：$target（补 APP_VERSION + 重启）"
  _refresh_app_version "$target"
  if _restart_services && _verify_health; then
    _write_state done "$target"
  else
    _write_state failed "$target" "自愈重启失败（服务起不来，检查日志）"
  fi
  [ "$DRY_RUN" = "1" ] || _cleanup_claimed "$claimed"
  exit 0
fi

# 目标 release：不存在则由本脚本 clone（web 只写标记，不再 clone——与 docker 形态
# 统一契约：执行侧负责全部 git 工作）。
target_dir="$RELEASES_DIR/$target"
if [ ! -d "$target_dir" ]; then
  _write_state pulling "$target"
  _log "clone $target → $target_dir"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] git clone --depth 1 --branch $target $repo_url $target_dir + 子模块"
  else
    _clone_env=()
    if [ -n "$proxy_url" ]; then
      export http_proxy="$proxy_url" https_proxy="$proxy_url" HTTP_PROXY="$proxy_url" HTTPS_PROXY="$proxy_url"
    fi
    if [ -z "$repo_url" ]; then
      _write_state failed "$target" "标记缺 repo_url 且未配 SERVER_UPGRADE_REPO_URL"
      _cleanup_claimed "$claimed"; exit 0
    fi
    if ! git clone --depth 1 --branch "$target" --no-tags "$repo_url" "$target_dir" 2>>"$LOG_FILE"; then
      _write_state failed "$target" "git clone 失败（检查网络/代理/repo_url）"
      _safe_rmtree "$target_dir"; _cleanup_claimed "$claimed"; exit 0
    fi
    if ! git -C "$target_dir" submodule update --init --depth 1 node_server user-frontend 2>>"$LOG_FILE"; then
      _write_state failed "$target" "子模块拉取失败"
      _safe_rmtree "$target_dir"; _cleanup_claimed "$claimed"; exit 0
    fi
  fi
fi

# 预检：关键文件在（clone 残缺就拒，不冒险翻指针）
for f in main.py requirements.txt node_server user-frontend/dist/index.html; do
  if [ ! -e "$target_dir/$f" ]; then
    _write_state failed "$target" "预检失败：$f 不存在（clone 残缺）"
    [ "$DRY_RUN" = "1" ] || _cleanup_claimed "$claimed"
    exit 0
  fi
done
_log "预检通过：$target_dir 关键文件齐全"

# venv + pip（翻指针之前——现役毫发无损，失败直接退出）
if [ ! -x "$target_dir/venv/bin/python" ]; then
  _write_state installing "$target"
  _log "创建 venv：$target_dir/venv"
  if [ "$DRY_RUN" = "1" ]; then
    _log "[dry-run] python3 -m venv + pip install -r requirements.txt（${PIP_TIMEOUT}s 超时）"
  else
    if ! python3 -m venv "$target_dir/venv" 2>>"$LOG_FILE"; then
      _write_state failed "$target" "venv 创建失败"
      _cleanup_claimed "$claimed"; exit 0
    fi
    _log "pip install（$PIP_TIMEOUTs 超时）"
    if ! timeout "$PIP_TIMEOUT" "$target_dir/venv/bin/pip" install -q -r "$target_dir/requirements.txt" -i "$PIP_INDEX" 2>>"$LOG_FILE"; then
      _write_state failed "$target" "pip install 失败"
      # 半装 venv 留着（下次重试可复用）；目录未翻指针，现役无损
      _cleanup_claimed "$claimed"; exit 0
    fi
  fi
else
  _log "venv 已存在，跳过装依赖：$target_dir/venv"
fi

# .env 软链进 release（shared/.env 是真身，跨版本存续，附件签名 key 不丢）
_do ln -sfn "$SHARED_DIR/.env" "$target_dir/.env"

# ★ 翻指针 = 升级提交点（ln -sfn + mv -T 原子 rename）
_write_state restarting "$target"
if [ "$DRY_RUN" = "1" ]; then
  _log "[dry-run] 将翻转 current → releases/$target"
else
  ln -sfn "releases/$target" "$CURRENT_LINK.new"
  mv -T "$CURRENT_LINK.new" "$CURRENT_LINK"
fi
_refresh_app_version "$target"

# 顺序重启 + 健康验证；失败回切旧版（旧 venv 完好，秒回）
if ! _restart_services; then
  _rollback "$target" "$old" "服务重启失败"
  [ "$DRY_RUN" = "1" ] || _cleanup_claimed "$claimed"; exit 0
fi
if ! _verify_health; then
  _rollback "$target" "$old" "健康验证超时"
  [ "$DRY_RUN" = "1" ] || _cleanup_claimed "$claimed"; exit 0
fi

# 成功：GC + 状态 + 日志
_gc_releases "$target"
_write_state done "$target"
[ "$DRY_RUN" = "1" ] || _cleanup_claimed "$claimed"
_log "✓ $old → $target 升级完成"
