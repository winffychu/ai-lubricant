import os
import sys

import pytest
from fastapi import HTTPException

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

import agent.api as agent_api


class _FakeRows:
    """内存版 db 查询：按 (id, user_id) / (id) / 分组授权三条路径分别命中。"""

    def __init__(self, by_id, group_root_ids_for_caller):
        self._by_id = by_id
        self._group_root_ids = set(group_root_ids_for_caller)

    async def get_api_key_by_id_for_user(self, key_id, user_id):
        row = self._by_id.get(key_id)
        if row and str(row.get("user_id") or "") == str(user_id):
            return dict(row)
        return None

    async def get_api_key_by_id(self, key_id):
        row = self._by_id.get(key_id)
        return dict(row) if row else None

    def group_system_resolver(self):
        async def _resolve(api_key_id, caller):
            if api_key_id in self._group_root_ids:
                return dict(self._by_id[api_key_id])
            return None
        return _resolve


def _patch_db(monkeypatch, fake):
    import db as db_mod
    monkeypatch.setattr(db_mod.PostgresClient, "get_api_key_by_id_for_user", fake.get_api_key_by_id_for_user)
    monkeypatch.setattr(db_mod.PostgresClient, "get_api_key_by_id", fake.get_api_key_by_id)
    monkeypatch.setattr(agent_api, "_resolve_group_system_key", fake.group_system_resolver())


@pytest.mark.asyncio
async def test_resolve_caller_derived_key_via_group_system_root(monkeypatch):
    """任务子 Key（user_id NULL，派生自分组系统根 Key）应通过根 Key 授权放行。"""
    root = {"id": 100, "key": "sk-root", "user_id": None, "parent_id": None, "disabled": False}
    child = {"id": 7, "key": "sk-child", "user_id": None, "parent_id": 100, "disabled": False}
    fake = _FakeRows({100: root, 7: child}, group_root_ids_for_caller=[100])
    _patch_db(monkeypatch, fake)

    row = await agent_api._resolve_caller_api_key(7, "user-1")
    assert row["key"] == "sk-child"
    assert row["id"] == 7


@pytest.mark.asyncio
async def test_resolve_caller_derived_key_via_own_root(monkeypatch):
    """派生自个人根 Key 的子 Key：根 user_id 命中调用者即放行。"""
    root = {"id": 200, "key": "sk-own", "user_id": "user-1", "parent_id": None, "disabled": False}
    child = {"id": 8, "key": "sk-child2", "user_id": "user-1", "parent_id": 200, "disabled": False}
    # user_id 已复制到子，owner 路径本就命中；此处验证不会因为多走一遍派生解析而错。
    fake = _FakeRows({200: root, 8: child}, group_root_ids_for_caller=[])
    _patch_db(monkeypatch, fake)

    row = await agent_api._resolve_caller_api_key(8, "user-1")
    assert row["id"] == 8


@pytest.mark.asyncio
async def test_resolve_caller_derived_key_rejects_unauthorized_root(monkeypatch):
    """根 Key 既不属调用者、也不在分组授权内 → 仍 403（不放宽授权语义）。"""
    root = {"id": 300, "key": "sk-other", "user_id": "user-2", "parent_id": None, "disabled": False}
    child = {"id": 9, "key": "sk-child3", "user_id": "user-2", "parent_id": 300, "disabled": False}
    fake = _FakeRows({300: root, 9: child}, group_root_ids_for_caller=[])
    _patch_db(monkeypatch, fake)

    with pytest.raises(HTTPException) as exc:
        await agent_api._resolve_caller_api_key(9, "user-1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_resolve_caller_derived_key_respects_child_disabled(monkeypatch):
    """根 Key 已授权，但子 Key 自身 disabled → 403「该 API Key 已禁用」。"""
    root = {"id": 100, "key": "sk-root", "user_id": None, "parent_id": None, "disabled": False}
    child = {"id": 7, "key": "sk-child", "user_id": None, "parent_id": 100, "disabled": True}
    fake = _FakeRows({100: root, 7: child}, group_root_ids_for_caller=[100])
    _patch_db(monkeypatch, fake)

    with pytest.raises(HTTPException) as exc:
        await agent_api._resolve_caller_api_key(7, "user-1")
    assert exc.value.status_code == 403
    assert "禁用" in exc.value.detail


def _patch_admin_role(monkeypatch, is_admin: bool):
    """把 _caller_is_admin 钉成固定结果，隔离 User 查询（本文件不建 tortoise 库）。"""
    async def _fake(caller):
        return is_admin

    monkeypatch.setattr(agent_api, "_caller_is_admin", _fake)


@pytest.mark.asyncio
async def test_admin_can_use_key_outside_own_scope(monkeypatch):
    """平台管理员（role==admin）越出用户域时放宽到全平台 Key。"""
    other = {"id": 500, "key": "sk-admin-visible", "user_id": "user-2", "parent_id": None, "disabled": False}
    fake = _FakeRows({500: other}, group_root_ids_for_caller=[])
    _patch_db(monkeypatch, fake)
    _patch_admin_role(monkeypatch, True)

    row = await agent_api._resolve_caller_api_key(500, "admin-1")
    assert row["id"] == 500
    assert row["key"] == "sk-admin-visible"


@pytest.mark.asyncio
async def test_non_admin_still_denied_outside_own_scope(monkeypatch):
    """普通用户越出用户域仍 403——放宽只对管理员生效。"""
    other = {"id": 501, "key": "sk-other", "user_id": "user-2", "parent_id": None, "disabled": False}
    fake = _FakeRows({501: other}, group_root_ids_for_caller=[])
    _patch_db(monkeypatch, fake)
    _patch_admin_role(monkeypatch, False)

    with pytest.raises(HTTPException) as exc:
        await agent_api._resolve_caller_api_key(501, "user-1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_disabled_key_still_rejected(monkeypatch):
    """管理员放宽取 Key，但 disabled 检查照样生效。"""
    other = {"id": 502, "key": "sk-disabled", "user_id": "user-2", "parent_id": None, "disabled": True}
    fake = _FakeRows({502: other}, group_root_ids_for_caller=[])
    _patch_db(monkeypatch, fake)
    _patch_admin_role(monkeypatch, True)

    with pytest.raises(HTTPException) as exc:
        await agent_api._resolve_caller_api_key(502, "admin-1")
    assert exc.value.status_code == 403
    assert "禁用" in exc.value.detail


@pytest.mark.asyncio
async def test_admin_own_key_skips_role_lookup(monkeypatch):
    """管理员用自己的 Key 走原用户域路径，不应触发 role 查询（省一次 User 查询）。"""
    own = {"id": 600, "key": "sk-own", "user_id": "admin-1", "parent_id": None, "disabled": False}
    fake = _FakeRows({600: own}, group_root_ids_for_caller=[])
    _patch_db(monkeypatch, fake)
    called = {"n": 0}

    async def _spy(caller):
        called["n"] += 1
        return True

    monkeypatch.setattr(agent_api, "_caller_is_admin", _spy)

    row = await agent_api._resolve_caller_api_key(600, "admin-1")
    assert row["id"] == 600
    assert called["n"] == 0
