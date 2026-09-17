"""iOS 设备状态合并：WDA 状态以**节点 inventory** 为准，不依赖易失的 job 快照。

背景（用户诉求「初始化能不能在后台进行」）：job 快照只活在服务端一条连接的
内存里（node_server/registry.py 的 _ios_job_snapshots），服务端一重启就全丢，
前端轮询必然 404 —— 此前正是界面卡死的原因。

修法：把 wda_state / wda_progress / wda_stage 挂在**设备**上——节点自报、
经 inventory 持久化、随重连重放。`/resources/devices` 把它们合并进资源的
`ios` 块，前端据此渲染「初始化中 42% / 就绪 / 失败」，刷新页面、关弹框、
服务端重启都不影响。

这些测试用 fake 的 get_ios_devices 驱动 _merge_ios_inventory，不需要真节点。
"""
from __future__ import annotations

import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)


class _FakeClient:
    """按 node_id 返回预设 inventory；记录调用次数以断言没有 N+1。"""

    def __init__(self, inventories: dict[str, dict], fail_nodes: set[str] | None = None):
        self.inventories = inventories
        self.fail_nodes = fail_nodes or set()
        self.calls: list[str] = []

    async def get_ios_devices(self, node_id: str) -> dict:
        self.calls.append(node_id)
        if node_id in self.fail_nodes:
            from user_platform.node_client import NodeServerUnavailable

            raise NodeServerUnavailable(f"node {node_id} unreachable")
        return self.inventories.get(node_id, {"node_id": node_id, "devices": []})


def _patch_client(monkeypatch, client):
    """把 routes_builtin_tools 里的 get_node_client 换成 fake。"""
    from user_platform import routes_builtin_tools

    monkeypatch.setattr(routes_builtin_tools, "get_node_client", lambda: client, raising=False)
    # get_node_client 是函数内 import 的，改模块属性不够——同时打 node_client 模块。
    from user_platform import node_client

    monkeypatch.setattr(node_client, "get_node_client", lambda: client, raising=False)


@pytest.fixture
def merge(monkeypatch):
    from user_platform.routes_builtin_tools import _merge_ios_inventory

    def _run(resources, inventories, fail_nodes=None):
        client = _FakeClient(inventories, fail_nodes)
        _patch_client(monkeypatch, client)
        import asyncio

        asyncio.run(_merge_ios_inventory(resources))
        return client

    return _run


# 注意：_merge_ios_inventory 内部是 `from .node_client import get_node_client`，
# 因此要 patch user_platform.node_client.get_node_client。上面的 fixture 已同时
# 打两处，保证无论 import 形式如何都能命中。


def _ios_resource(resource_id: int, device_id: str, node_id: str, **ios_extra) -> dict:
    ios = {"udid": "UDID-1", "node_id": node_id, "wda_state": "missing"}
    ios.update(ios_extra)
    return {"id": resource_id, "device_id": device_id, "ios": ios, "platform": "ios"}


def test_merges_wda_progress_into_ios_block(merge):
    """核心：节点 inventory 的进度落进资源的 ios 块（前端据此显示百分比）。"""
    resources = [_ios_resource(36, "dev_X", "node-a")]
    inv = {
        "node-a": {
            "devices": [
                {"device_id": "dev_X", "wda_state": "preparing", "wda_progress": 42, "wda_stage": "downloading"}
            ]
        }
    }
    merge(resources, inv)

    ios = resources[0]["ios"]
    assert ios["wda_state"] == "preparing"
    assert ios["wda_progress"] == 42
    assert ios["wda_stage"] == "downloading"


def test_merges_terminal_state(merge):
    resources = [_ios_resource(36, "dev_X", "node-a")]
    inv = {"node-a": {"devices": [{"device_id": "dev_X", "wda_state": "ready", "profile_expires_at": "2026-10-01T00:00:00Z"}]}}
    merge(resources, inv)

    assert resources[0]["ios"]["wda_state"] == "ready"
    assert resources[0]["ios"]["profile_expires_at"] == "2026-10-01T00:00:00Z"


def test_node_unreachable_keeps_existing_state(merge):
    """节点离线时**保留**已知状态，不能覆盖成空——否则界面会在节点抖动时
    闪回「待初始化」。"""
    resources = [_ios_resource(36, "dev_X", "node-a", wda_state="ready", wda_progress=100)]
    merge(resources, {}, fail_nodes={"node-a"})

    assert resources[0]["ios"]["wda_state"] == "ready", "节点不可达不应抹掉已知状态"
    assert resources[0]["ios"]["wda_progress"] == 100


def test_empty_values_do_not_overwrite(merge):
    """节点清单里字段为空（刚重连还没填全）时不覆盖已有值。"""
    resources = [_ios_resource(36, "dev_X", "node-a", wda_state="ready", wda_progress=100)]
    inv = {"node-a": {"devices": [{"device_id": "dev_X", "wda_state": "", "wda_progress": 0, "wda_stage": ""}]}}
    merge(resources, inv)

    # 空字符串不覆盖；wda_progress=0 是有效值（新 job 开始），保留 0。
    assert resources[0]["ios"]["wda_state"] == "ready"
    assert resources[0]["ios"]["wda_progress"] == 0


def test_no_n_plus_one_per_node(merge):
    """同一节点的多台设备只取一次 inventory。"""
    resources = [
        _ios_resource(1, "dev_A", "node-a"),
        _ios_resource(2, "dev_B", "node-a"),
        _ios_resource(3, "dev_C", "node-a"),
    ]
    inv = {"node-a": {"devices": [
        {"device_id": "dev_A", "wda_state": "ready"},
        {"device_id": "dev_B", "wda_state": "preparing", "wda_progress": 10},
        {"device_id": "dev_C", "wda_state": "missing"},
    ]}}
    client = merge(resources, inv)

    assert client.calls.count("node-a") == 1, f"expected 1 call for node-a, got {client.calls}"


def test_non_ios_resources_untouched(merge):
    """Android 设备（无 ios 块）不参与合并，也不会触发 inventory 请求。"""
    resources = [{"id": 35, "device_id": "dev_AND", "platform": "android", "device_info": {}}]
    client = merge(resources, {"node-a": {"devices": []}})

    assert "ios" not in resources[0]
    assert client.calls == [], "没有 iOS 设备时不该请求任何节点"


def test_device_id_mismatch_is_skipped(merge):
    """inventory 里的 device_id 对不上任何资源时不误改。"""
    resources = [_ios_resource(36, "dev_X", "node-a", wda_state="missing")]
    inv = {"node-a": {"devices": [{"device_id": "dev_OTHER", "wda_state": "ready"}]}}
    merge(resources, inv)

    assert resources[0]["ios"]["wda_state"] == "missing"
