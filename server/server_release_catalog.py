"""服务端（数据服务/控制面/Tunnel Runtime）发行版本目录。

与节点/移动端/设备控制三条线同构但有一处本质差异：**服务端没有二进制资产要
托管**。节点程序要传 tar.gz 进独立发行仓库、移动端要传 APK，服务端的一版就是
一个 git tag——部署机 ``git clone --branch <tag>`` 即得全套代码（前端 dist 已
随库发布，见 ``script/publish_github.sh`` 第 0.5 步）。

因此本模块不渲染 version.json 到市场仓库、不碰 GitHub Releases，只做两件事：

1. **登记**：管理员在市场管理「服务端」线新建版本时选定 git tag + 写备注，
   条目落 ``marketplace_items``（module=``server-versions``），与其余三条线共用
   同一张表与同一套 CRUD/校验/发布机制。
2. **快照**：本进程内存 + ``app_config.server_release_snapshot`` 缓存"当前已
   登记的最新版本"，供管理端升级卡片读取。**登记即门槛**——publish 只是把 tag
   推上 GitHub 当源料，登记了用户才看得见，升级再要一次显式确认。

与另三条线的关键区别：消费侧（升级卡片）读的是**本模块的内存快照**而非
GitHub raw，所以登记后无需等 CDN 传播；``apply_release`` 由 publisher 在同进程
调用（与 node/mobile 同款），多实例经 ``runtime_sync`` 广播后从 DB 重载。
"""
from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import os
from typing import Any

from loguru import logger

from db import PostgresClient

SNAPSHOT_KEY = "server_release_snapshot"

_lock = asyncio.Lock()
_snapshot: dict[str, Any] = {
    "version": "",
    "version_notes": "",
    "release_tag": "",
    "repo_url": "",
    "updated_at": "",
    "stale": True,
}


async def load_snapshot() -> None:
    global _snapshot
    try:
        stored = await PostgresClient.get_config(SNAPSHOT_KEY)
    except Exception as exc:
        logger.warning("[server-release] load snapshot failed: {}", exc)
        return
    if isinstance(stored, dict) and stored.get("version"):
        _snapshot = copy.deepcopy(stored)
        logger.info(
            "[server-release] loaded snapshot version={}", _snapshot.get("version") or "(empty)"
        )


async def get_latest_release() -> dict[str, Any]:
    """返回当前已登记的版本快照（或空骨架）。永远不抛。"""
    return copy.deepcopy(_snapshot)


async def apply_release(release: dict[str, Any], *, publish: bool = True) -> dict[str, Any]:
    """立即应用市场写侧刚登记的版本，避免等待定时同步。

    与另三条线同款语义：登记完成后已拿到权威 payload，直接写 PG + 内存，多实例经
    runtime_sync 广播后从 DB 重载。本模块不做 GitHub 校验（无资产可校验）——tag
    的合法性由登记时的 GitHub 标签列表接口保证（见 ``server_upgrade.list_release_tags``）。

    **空 payload 表示「当前无已登记版本」**（最新那条被改回 draft 或删除）：此时
    写空骨架而不是保留旧值——否则升级卡片会一直推荐一个已被撤回的版本。
    """
    global _snapshot
    version = str(release.get("version") or "").strip()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    next_snapshot = {
        "version": version,
        "version_notes": str(release.get("version_notes") or ""),
        "release_tag": str(release.get("release_tag") or version),
        "repo_url": str(release.get("repo_url") or ""),
        "updated_at": release.get("updated_at") or now,
        "stale": False,
        "fetched_at": now,
    }
    async with _lock:
        await PostgresClient.set_config(SNAPSHOT_KEY, next_snapshot)
        _snapshot = copy.deepcopy(next_snapshot)
    if publish:
        import runtime_sync

        await runtime_sync.publish(runtime_sync.EVENT_SERVER_RELEASE, "__all__")
    logger.info("[server-release] applied version={}", _snapshot.get("version") or "(empty)")
    return copy.deepcopy(_snapshot)


async def reload_from_db() -> None:
    await load_snapshot()


def _clean_version(value: Any) -> str:
    """归一版本号：去前导 v/V、空白，统一成裸串用于比较。"""
    return str(value or "").strip().lstrip("vV").strip()


def server_upgrade_status(
    latest: dict[str, Any], *, current_version: str
) -> dict[str, Any]:
    """计算服务端当前/最新/是否可升级。

    版本号是日期串（``vYYMMDD``），字典序即时间序，字符串比较即可；``stale``
    表示快照未能刷新（本模块无远端拉取，恒 False，保留字段与另三条线同形）。
    """
    current = _clean_version(current_version)
    latest_version = _clean_version(latest.get("version") or "")
    needs = bool(latest_version) and current != latest_version
    return {
        "stale": bool(latest.get("stale")),
        "current": current,
        "latest": latest_version,
        "release_tag": str(latest.get("release_tag") or ""),
        "version_notes": str(latest.get("version_notes") or ""),
        "repo_url": str(latest.get("repo_url") or ""),
        "needs_upgrade": needs,
        "updated_at": str(latest.get("updated_at") or ""),
    }


def current_version() -> str:
    """本进程运行版本。

    裸跑（releases+current 布局）：由 systemd 从 shared/alb.env 注入 ``APP_VERSION``
    （updater 翻 current 指针后写入）。镜像部署：构建期 ``--build-arg APP_VERSION``
    烧进 ``ENV``。两者都缺失时回落 ``dev``，与 ``routes_server._version()`` 同口径。
    """
    return (os.getenv("APP_VERSION") or "dev").strip() or "dev"


async def sync_loop() -> None:
    """占位循环：本模块无远端拉取，仅周期性从 DB 重载以收敛多实例。

    与另三条线保持同形的启动方式（main.py 统一 create_task），但间隔更长——登记
    动作已由 publisher 同进程 apply，这里的重载只是多实例兜底。
    """
    interval = max(60, int(os.getenv("SERVER_RELEASE_SYNC_INTERVAL", "600")))
    while True:
        try:
            await reload_from_db()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[server-release] reload cycle failed: {}", exc)
        await asyncio.sleep(interval)
