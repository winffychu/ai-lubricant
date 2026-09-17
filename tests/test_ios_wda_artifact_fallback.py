"""WDA 产物解析：市场为空时回退 go-ios 内置的 DeviceKit runner。

背景：prepare/renew/reinstall 都要一份 WDA runner ipa 的 download_url + sha256。
此前 ``_resolve_market_wda_asset`` 只认市场 device-control iOS 发行版，市场为空就
412「市场尚未发布 WDA iOS 产物」——把「初始化」硬卡住，逼运维自己编译并上传 ipa。

但 go-ios 本身就带这个产物：``ios ui install devicekit`` 从
``https://deviceboxhq.com/devicekit-ios-runner-0.0.18.ipa`` 拉取
（go-ios v1.3.2 cmd_device_ui_install.go:19 defaultDeviceKitArtifactURL）。节点侧
默认 bundle/xctest 也与这份产物对齐（common/ioshost/wdapipeline.go 的
defaultDeviceKitBundleID / defaultDeviceKitXctest）。所以市场为空时直接回退到它，
运维不必编译、不必上传。

sha256 必须钉死：节点 Fetch 把 digest 当作「批准产物」与「被替换产物」之间唯一
那道闸（wdapipeline.go: "Refuse rather than trust"），缺 digest 直接拒装。
"""
from __future__ import annotations

import pytest

from user_platform import routes_ios


def test_builtin_runner_constants_are_pinned():
    """内置回退的三要素（url/sha256/size）必须是钉死的常量。

    sha256 尤其关键——它是节点装代码前的唯一校验，不能是空或占位。
    """
    assert routes_ios._BUILTIN_DEVICEKIT_RUNNER_URL.startswith("https://")
    assert routes_ios._BUILTIN_DEVICEKIT_RUNNER_URL.endswith(".ipa")
    assert len(routes_ios._BUILTIN_DEVICEKIT_RUNNER_SHA256) == 64
    assert all(c in "0123456789abcdef" for c in routes_ios._BUILTIN_DEVICEKIT_RUNNER_SHA256)
    assert routes_ios._BUILTIN_DEVICEKIT_RUNNER_SIZE > 0


def test_builtin_sha256_is_not_a_placeholder():
    """防回归：不能退回成全 0 / 全 f 之类的占位值。"""
    sha = routes_ios._BUILTIN_DEVICEKIT_RUNNER_SHA256
    assert len(set(sha)) > 8, f"sha256 看起来像占位值: {sha}"


def test_builtin_url_matches_goios_default():
    """与 go-ios 内置地址同源（v1.3.2 cmd_device_ui_install.go:19）。

    若哪天 go-ios 升版换了 runner 版本，这条会失败，提醒同步更新常量与 sha256。
    """
    assert routes_ios._BUILTIN_DEVICEKIT_RUNNER_URL == (
        "https://deviceboxhq.com/devicekit-ios-runner-0.0.18.ipa"
    )
    assert routes_ios._BUILTIN_DEVICEKIT_RUNNER_VERSION == "0.0.18"


@pytest.mark.asyncio
async def test_falls_back_to_builtin_when_market_empty(monkeypatch):
    """市场没有 iOS 产物 → 回退内置，而不是 412。"""
    from server import device_control_release_catalog as dcrc

    async def _empty():
        return {"version": "", "assets": []}

    monkeypatch.setattr(dcrc, "get_latest_release", _empty)

    art = await routes_ios._resolve_market_wda_asset()

    assert art["source"] == "builtin-goios"
    assert art["download_url"] == routes_ios._BUILTIN_DEVICEKIT_RUNNER_URL
    assert art["sha256"] == routes_ios._BUILTIN_DEVICEKIT_RUNNER_SHA256
    assert art["size_bytes"] == routes_ios._BUILTIN_DEVICEKIT_RUNNER_SIZE
    # 节点 Fetch 硬要求 sha256，缺了就 artifact_missing —— 必须非空。
    assert art["sha256"]


@pytest.mark.asyncio
async def test_market_asset_wins_when_published(monkeypatch):
    """市场发布了 iOS 产物时优先用它（运营方自己的签名产物/新版 runner）。"""
    from server import device_control_release_catalog as dcrc

    async def _with_ios():
        return {
            "version": "1.2.3",
            "assets": [{
                "platform": "ios",
                "download_url": "https://example.test/own-runner.ipa",
                "digest": "sha256:" + "a" * 64,
                "size_bytes": 1234,
                "version": "1.2.3",
            }],
        }

    monkeypatch.setattr(dcrc, "get_latest_release", _with_ios)

    art = await routes_ios._resolve_market_wda_asset()

    assert art["source"] == "market"
    assert art["download_url"] == "https://example.test/own-runner.ipa"
    assert art["sha256"] == "a" * 64, "市场 digest 的 sha256: 前缀要剥掉"


@pytest.mark.asyncio
async def test_incomplete_market_asset_falls_back(monkeypatch):
    """市场有 iOS 条目但缺 digest/url → 回退内置，不让初始化硬失败。"""
    from server import device_control_release_catalog as dcrc

    async def _incomplete():
        return {"version": "9.9", "assets": [{"platform": "ios", "download_url": ""}]}

    monkeypatch.setattr(dcrc, "get_latest_release", _incomplete)

    art = await routes_ios._resolve_market_wda_asset()
    assert art["source"] == "builtin-goios"
    assert art["sha256"] == routes_ios._BUILTIN_DEVICEKIT_RUNNER_SHA256


@pytest.mark.asyncio
async def test_market_unavailable_falls_back(monkeypatch):
    """市场 catalog 抛异常（网络/未启用）→ 回退内置，不把异常抛给用户。"""
    from server import device_control_release_catalog as dcrc

    async def _boom():
        raise RuntimeError("catalog down")

    monkeypatch.setattr(dcrc, "get_latest_release", _boom)

    art = await routes_ios._resolve_market_wda_asset()
    assert art["source"] == "builtin-goios"