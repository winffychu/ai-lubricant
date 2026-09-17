"""服务端一键升级的 web 侧（登记即门槛 + 检测 + 写标记）。

**职责切分（v3，生产=docker compose 后收敛）**：

- **登记**：管理员在市场管理「服务端」线选定 git tag + 备注 → 落
  ``marketplace_items``（module=``server-versions``）。复用通用市场 CRUD，不在本模块。
- **检测**：本模块读 ``server_release_catalog`` 内存快照 + 当前运行版本（APP_VERSION），
  算出 ``current`` / ``latest`` / ``needs_upgrade``。**离线可检**——不查 GitHub。
- **写标记**：管理员确认升级后，本模块**只写一个 JSON 标记文件**到挂载目录
  （docker: ``./data/ai_data`` ↔ 容器 ``/app/data``；裸跑: ``/var/lib/alb``），
  带上 target + 解析好的代理 URL + repo_url。**不 clone**——容器里没 git，
  而宿主才有 git/docker。web 进程不能重启自己（systemd 杀 cgroup / docker 重创建
  都会带走它 spawn 的一切），所以执行一律在宿主侧。

**宿主侧执行**（不在本模块，见 ``script/docker_updater.sh`` 与
``script/server_updater.sh``）：认领标记 → git 拉取/切换 → 装依赖/构建 → 重启
→ 健康验证 → 失败回切 → GC。两边读同一份标记 JSON，web 完全不关心执行形态。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import datetime as _dt
from pathlib import Path
from typing import Any

from loguru import logger


def _state_dir() -> Path:
    """标记/状态目录：docker 默认 ``/app/data``（与宿主 ``./data/ai_data`` 同一份
    bind mount）；裸跑默认 ``/var/lib/alb``。env 可覆盖。"""
    return Path(os.environ.get("SERVER_UPGRADE_STATE_DIR", "/var/lib/alb"))


def _marker_path() -> Path:
    return _state_dir() / "upgrade_target"


def _state_path() -> Path:
    return _state_dir() / "upgrade_state.json"


# 升级进行中的 phase（执行侧写的）：处于这些状态时拒绝新请求 + UI 显示进度
_INFLIGHT_PHASES = frozenset(
    {"requested", "pulling", "installing", "restarting", "verifying"}
)

_TAG_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


async def detect() -> dict[str, Any]:
    """返回升级卡片所需的全部状态：检测 + 进行中的 phase。永远不抛。"""
    import server_release_catalog as catalog

    latest = await catalog.get_latest_release()
    status = catalog.server_upgrade_status(latest, current_version=catalog.current_version())
    state = _read_state()
    return {
        **status,
        "phase": state.get("phase") or "idle",
        "target": state.get("target") or "",
        "error": state.get("error") or "",
        "started_at": state.get("started_at") or "",
        "updated_at": state.get("updated_at") or "",
    }


async def list_release_tags(*, proxy_config_id: str = "") -> dict[str, Any]:
    """拉取服务端发行仓库的 git tag 列表（登记对话框下拉用）。

    走 ``git ls-remote --tags`` 而不是 GitHub API：与升级时实际 clone 走同一条
    网络路径（代理一致即说明 clone 也能通），免 token、免 API 限流。代理从平台
    代理池按 ``proxy_config_id`` 取（与节点升级同款 ``resolve_proxy``）。

    docker 形态下 web 容器内可能没装 git——那时返回空列表 + 错误，让管理员去
    宿主上配代理或在裸跑形态用。这是检测接口的退化，不影响升级主链路（拉取代码
    由宿主执行器完成，那里有 git）。
    """
    repo_url = os.environ.get(
        "SERVER_UPGRADE_REPO_URL", "https://github.com/wuxin-gh/ai-lubricant.git"
    )
    env = await _git_env_for_proxy(proxy_config_id)
    cmd = ["git", "ls-remote", "--tags", repo_url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"tags": [], "error": "拉取 tag 超时（检查代理或 GitHub 可达性）"}
        if proc.returncode != 0:
            return {"tags": [], "error": (stderr or b"").decode("utf-8", "replace").strip()[:200]}
    except FileNotFoundError:
        return {"tags": [], "error": "web 容器未安装 git（在宿主侧拉 tag 或用裸跑形态）"}
    except Exception as exc:  # noqa: BLE001
        return {"tags": [], "error": str(exc)[:200]}

    tags: list[str] = []
    seen: set[str] = set()
    for line in (stdout or b"").decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ref = parts[1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):].split("^{", 1)[0].strip()
        if not tag or not _TAG_RE.match(tag) or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
    tags.sort(reverse=True)  # vYYMMDD 字典序即时间倒序
    return {"tags": tags[:100], "error": ""}


async def start_upgrade(*, target_tag: str, proxy_config_id: str = "") -> dict[str, Any]:
    """触发一次升级：解析代理 + 原子写 JSON 标记文件，宿主执行器认领后干活。

    返回 ``{accepted}``——与节点升级同款语义。进度靠 ``detect()`` 轮询 phase
    （requested → pulling → installing → restarting → verifying → done|failed）。
    本函数只写标记，**不 clone**——执行（含 git 拉取）全在宿主侧，web 不能
    重启自己（docker recreate / systemd cgroup kill 都会带走它 spawn 的一切）。
    """
    import server_release_catalog as catalog

    target = (target_tag or "").strip()
    if not target or not _TAG_RE.match(target):
        return {"accepted": False, "error": "target_tag 非法"}

    latest = await catalog.get_latest_release()
    release_tag = str(latest.get("release_tag") or "").strip()
    # 必须是已登记的版本：升级到任意 tag = 绕过「登记即门槛」
    if not release_tag:
        return {"accepted": False, "error": "尚未登记任何服务端版本"}
    if target != release_tag:
        return {"accepted": False, "error": f"目标 {target} 未登记（最新登记版 {release_tag}）"}

    current = catalog.current_version()
    if _strip_v(target) == _strip_v(current):
        return {"accepted": False, "error": f"当前已运行 {current}，无需升级"}

    state = _read_state()
    if state.get("phase") in _INFLIGHT_PHASES:
        return {"accepted": False, "error": f"已有升级进行中（{state.get('phase')}）"}

    # 解析代理：web 从平台代理池拿 URL，写进标记，宿主执行器据此 export http_proxy。
    # 这样宿主不用知道代理池配置，只认一个 URL——docker 形态下宿主与容器的代理
    # 配置本就分离，靠标记传递是唯一干净的方式。
    proxy_url = await _resolve_proxy_url(proxy_config_id)
    repo_url = str(latest.get("repo_url") or "") or os.environ.get(
        "SERVER_UPGRADE_REPO_URL", "https://github.com/wuxin-gh/ai-lubricant.git"
    )

    try:
        _atomic_write_marker({
            "target": target,
            "proxy_url": proxy_url,
            "repo_url": repo_url,
            "previous": _strip_v(current),
            "started_at": _now(),
        })
        _write_state({"phase": "requested", "target": target, "started_at": _now(), "updated_at": _now()})
    except Exception as exc:  # noqa: BLE001
        logger.warning("[server-upgrade] write marker/state failed: {}", exc)
        return {"accepted": False, "error": f"写标记失败：{exc}"}

    logger.info("[server-upgrade] marker written for {} (proxy={})", target, "direct" if not proxy_url else "set")
    return {"accepted": True}


# ── 工具函数 ──────────────────────────────────────────────────────────────

async def _git_env_for_proxy(proxy_config_id: str) -> dict[str, str]:
    """为 ``git ls-remote`` 子进程注入代理环境变量。"""
    url = await _resolve_proxy_url(proxy_config_id)
    if not url:
        return {}
    return {"http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url}


async def _resolve_proxy_url(proxy_config_id: str) -> str:
    """从平台代理池解析出代理 URL（network 模式）。url_prefix / node 模式不适用 git
    clone（前者是 HTTP 前缀转发，后者走节点），返回空让宿主直连。"""
    pid = (proxy_config_id or "").strip()
    if not pid:
        return ""
    from server.node_upgrade_targets import resolve_proxy

    try:
        fields = await resolve_proxy(pid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[server-upgrade] resolve_proxy failed: {}", exc)
        return ""
    return str(fields.get("proxy_url") or "").strip()


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".upgrade_state.", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[server-upgrade] write state failed: {}", exc)


def _atomic_write_marker(payload: dict[str, Any]) -> None:
    """原子写 JSON 标记：tmp + rename，执行器看到的永远是完整 JSON。"""
    path = _marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".upgrade_target.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _strip_v(value: str) -> str:
    return (value or "").strip().lstrip("vV").strip()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
