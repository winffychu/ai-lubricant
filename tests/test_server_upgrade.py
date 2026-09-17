"""服务端自身升级链路的行为回归（登记校验 / 版本投影 / 拉取与标记 / 状态机）。

覆盖三块：
1. ``validator`` 的 server-versions 分支——release_tag 是升级动作的实际输入
   （部署机 git checkout 它），必须显式、合法、不可注入。
2. ``render_server_release``——取最大 published 版本；draft 不进（登记即门槛）。
3. ``server_upgrade``——detect 的升级判定、start_upgrade 的门禁（未登记/已是最新/
   进行中）、标记文件原子写、clone 失败清半成品目录。

不真连 GitHub：``start_upgrade`` 的克隆走假 subprocess，只验证标记与状态机的
落盘行为（这是 path unit 触发 updater 的唯一接口，必须逐字节正确）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "server")
sys.path.insert(0, ".")

from user_platform.marketplace.render import render_server_release  # noqa: E402
from user_platform.marketplace.validator import (  # noqa: E402
    _is_server_version,
    validate_manifest,
)


def _server_manifest(version="v260912", **over) -> dict:
    base = {
        "id": f"server-suite-{version}",
        "kind": "server_app_version",
        "schema": "ai-lubricant.server-version/v1",
        "name": "server",
        "display_name": f"服务端 {version}",
        "version": version,
        "status": "published",
        "version_notes": "首个版本",
        "release_tag": version,
        "repo_url": "https://github.com/wuxin-gh/ai-lubricant",
    }
    base.update(over)
    return base


# ── 版本号与 manifest 校验 ───────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["v260912", "260912", "1.2.3", "v1.2.3"])
def test_server_version_accepts_date_and_semver(value):
    assert _is_server_version(value)


@pytest.mark.parametrize("value", ["", "abc", "v", "2026-09-12"])
def test_server_version_rejects_junk(value):
    assert not _is_server_version(value)


def test_valid_server_manifest_passes():
    assert validate_manifest("server-versions", _server_manifest()) == []


def test_release_tag_is_required():
    """release_tag 是升级动作的输入，缺了整条链路无从下手。"""
    manifest = _server_manifest()
    del manifest["release_tag"]
    errors = validate_manifest("server-versions", manifest)
    assert any("release_tag" in e for e in errors)


@pytest.mark.parametrize(
    "tag",
    ["v260912; rm -rf /", "v260912 && curl evil", "v260912|x", "v 260912", "v260912\nx"],
)
def test_release_tag_rejects_shell_injection(tag):
    """tag 会被交给部署机的 git checkout，空白/元字符一律拦掉。"""
    errors = validate_manifest("server-versions", _server_manifest(release_tag=tag))
    assert any("release_tag" in e for e in errors)


def test_repo_url_must_be_https_when_present():
    errors = validate_manifest(
        "server-versions", _server_manifest(repo_url="ftp://example.com/x")
    )
    assert any("repo_url" in e for e in errors)
    # 留空允许：部署机可用 .env 的 SERVER_UPGRADE_REPO_URL 兜底
    manifest = _server_manifest()
    manifest.pop("repo_url")
    assert validate_manifest("server-versions", manifest) == []


def test_server_version_needs_no_summary():
    """与另三条发行线同口径：version_notes 即说明，不强制 summary。"""
    assert validate_manifest("server-versions", _server_manifest()) == []


# ── 版本投影（登记即门槛）────────────────────────────────────────────────────

def test_render_takes_highest_published_version():
    manifests = [
        _server_manifest("v260901", version_notes="旧"),
        _server_manifest("v260912", version_notes="新"),
        _server_manifest("v260905", version_notes="中"),
    ]
    payload, errors = render_server_release(manifests)
    assert errors == []
    assert payload["version"] == "v260912"
    assert payload["version_notes"] == "新"
    assert payload["release_tag"] == "v260912"


def test_render_excludes_draft():
    """draft = 发了但不推给用户：不进快照，卡片不会推荐它。"""
    manifests = [
        _server_manifest("v260912", status="draft"),
        _server_manifest("v260901", status="published"),
    ]
    payload, _ = render_server_release(manifests)
    assert payload["version"] == "v260901"


def test_render_empty_when_nothing_published():
    """全 draft / 全删：投影空骨架，撤回最新版后卡片不再推荐它。"""
    payload, errors = render_server_release([_server_manifest("v260912", status="draft")])
    assert errors == []
    assert payload["version"] == ""
    assert render_server_release([])[0]["version"] == ""


def test_render_ignores_non_dict_and_blank_version():
    payload, _ = render_server_release([None, "x", {"status": "published"}, _server_manifest()])
    assert payload["version"] == "v260912"


# ── server_upgrade：detect / 门禁 / 标记落盘 ─────────────────────────────────

@pytest.fixture()
def upgrade_env(tmp_path, monkeypatch):
    """把状态目录与 releases 目录指向 tmp，并重置 catalog 内存快照。"""
    state_dir = tmp_path / "alb"
    releases = tmp_path / "releases"
    state_dir.mkdir()
    releases.mkdir()
    monkeypatch.setenv("SERVER_UPGRADE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("SERVER_RELEASES_DIR", str(releases))
    monkeypatch.setenv("APP_VERSION", "v260901")

    import server_release_catalog as catalog

    monkeypatch.setattr(catalog, "_snapshot", {
        "version": "", "version_notes": "", "release_tag": "", "repo_url": "", "stale": True,
    })

    from user_platform import server_upgrade

    return {"module": server_upgrade, "state_dir": state_dir, "releases": releases, "catalog": catalog}


def _register(catalog, version, **over):
    snapshot = {
        "version": version,
        "version_notes": over.get("version_notes", ""),
        "release_tag": over.get("release_tag", version),
        "repo_url": over.get("repo_url", "https://github.com/wuxin-gh/ai-lubricant.git"),
        "stale": False,
    }
    catalog._snapshot = snapshot


def test_detect_reports_upgrade_available(upgrade_env):
    _register(upgrade_env["catalog"], "v260912")
    status = asyncio.run(upgrade_env["module"].detect())
    # current/latest 归一去 v 前缀（与 node/mobile 线同口径），release_tag 保留带 v 形态
    assert status["current"] == "260901"
    assert status["latest"] == "260912"
    assert status["needs_upgrade"] is True
    assert status["release_tag"] == "v260912"
    assert status["phase"] == "idle"


def test_detect_no_upgrade_when_current_matches(upgrade_env):
    """登记的 release_tag 带 v、APP_VERSION 不带 v 也要判等（归一比较）。"""
    _register(upgrade_env["catalog"], "v260901")
    status = asyncio.run(upgrade_env["module"].detect())
    assert status["needs_upgrade"] is False


def test_detect_without_registered_version(upgrade_env):
    status = asyncio.run(upgrade_env["module"].detect())
    assert status["latest"] == ""
    assert status["needs_upgrade"] is False
    assert status["phase"] == "idle"


def test_start_upgrade_rejects_unregistered_target(upgrade_env):
    _register(upgrade_env["catalog"], "v260912")
    result = asyncio.run(
        upgrade_env["module"].start_upgrade(target_tag="v269999")
    )
    assert result["accepted"] is False
    assert "未登记" in result["error"]


def test_start_upgrade_rejects_when_nothing_registered(upgrade_env):
    result = asyncio.run(upgrade_env["module"].start_upgrade(target_tag="v260912"))
    assert result["accepted"] is False


def test_start_upgrade_rejects_when_already_current(upgrade_env):
    _register(upgrade_env["catalog"], "v260901")
    result = asyncio.run(upgrade_env["module"].start_upgrade(target_tag="v260901"))
    assert result["accepted"] is False
    assert "已运行" in result["error"]


def test_start_upgrade_rejects_bad_tag(upgrade_env):
    _register(upgrade_env["catalog"], "v260912")
    for bad in ["", "v260912; rm -rf /", "../etc"]:
        result = asyncio.run(upgrade_env["module"].start_upgrade(target_tag=bad))
        assert result["accepted"] is False, bad


def test_start_upgrade_refuses_while_inflight(upgrade_env):
    _register(upgrade_env["catalog"], "v260912")
    state_path = upgrade_env["state_dir"] / "upgrade_state.json"
    state_path.write_text(json.dumps({"phase": "installing", "target": "v260912"}), encoding="utf-8")
    result = asyncio.run(upgrade_env["module"].start_upgrade(target_tag="v260912"))
    assert result["accepted"] is False
    assert "进行中" in result["error"]


def test_start_upgrade_writes_json_marker_with_proxy(upgrade_env, monkeypatch):
    """web 只写标记不 clone：标记必须带 target + 代理 URL + repo_url，执行器据此干活。"""
    _register(upgrade_env["catalog"], "v260912")
    module = upgrade_env["module"]

    async def _fake_proxy(pid):
        return "http://127.0.0.1:7890"

    monkeypatch.setattr(module, "_resolve_proxy_url", _fake_proxy)

    result = asyncio.run(module.start_upgrade(target_tag="v260912", proxy_config_id="p1"))
    assert result["accepted"] is True
    marker = upgrade_env["state_dir"] / "upgrade_target"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["target"] == "v260912"
    assert payload["proxy_url"] == "http://127.0.0.1:7890"
    assert payload["repo_url"]
    # 状态同步写成 requested，UI 立刻能看到「已请求」
    assert module._read_state()["phase"] == "requested"


def test_start_upgrade_reports_marker_write_failure(upgrade_env, monkeypatch):
    """写标记失败（目录不可写）要报错而不是假装 accepted——否则 UI 空等。"""
    _register(upgrade_env["catalog"], "v260912")
    module = upgrade_env["module"]

    def _boom(_payload):
        raise OSError("read-only file system")

    monkeypatch.setattr(module, "_atomic_write_marker", _boom)
    result = asyncio.run(module.start_upgrade(target_tag="v260912"))
    assert result["accepted"] is False
    assert "写标记失败" in result["error"]


def test_marker_is_written_atomically(upgrade_env):
    """标记是 path unit 的唯一触发接口，且执行器按 JSON 解析——必须是完整合法 JSON。"""
    module = upgrade_env["module"]
    module._atomic_write_marker({"target": "v260912", "proxy_url": "", "repo_url": "https://example.com/x.git"})
    marker = upgrade_env["state_dir"] / "upgrade_target"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["target"] == "v260912"
    assert payload["repo_url"] == "https://example.com/x.git"
    # 无残留 tmp 文件（tmp 必须被 rename 走，不能留在目录里被误读）
    leftovers = [p.name for p in upgrade_env["state_dir"].iterdir() if p.name.startswith(".upgrade_target.")]
    assert leftovers == []


def test_state_file_roundtrip_and_atomicity(upgrade_env):
    module = upgrade_env["module"]
    module._write_state({"phase": "cloning", "target": "v260912"})
    assert module._read_state()["phase"] == "cloning"
    leftovers = [p.name for p in upgrade_env["state_dir"].iterdir() if p.name.startswith(".upgrade_state.")]
    assert leftovers == []


def test_read_state_returns_empty_on_missing_or_corrupt(upgrade_env):
    module = upgrade_env["module"]
    assert module._read_state() == {}
    (upgrade_env["state_dir"] / "upgrade_state.json").write_text("{not json", encoding="utf-8")
    assert module._read_state() == {}
