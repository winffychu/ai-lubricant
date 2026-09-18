"""Unit tests for the native-deps package (no network, pure logic).

These exercise the decisions that are easy to break silently: manifest URL
resolution per platform, the ``.env`` key set produced from a resolved config,
and the Redis >= 6.0 version gate.
"""
from __future__ import annotations

import os
import sys

import pytest

_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _proj not in sys.path:
    sys.path.insert(0, _proj)

from native_deps import lifecycle, redis, sources
from native_deps.lifecycle import DepsConfig


def test_platform_tokens_are_valid(monkeypatch):
    monkeypatch.delenv("NATIVE_POSTGRES_VERSION", raising=False)
    os_token, arch, _ = sources.platform_tokens()
    assert os_token in ("windows", "linux", "darwin")
    assert arch in ("amd64", "arm64", "386")


def test_asset_has_url_for_each_kind(monkeypatch):
    for kind in ("postgres", "redis", "clickhouse"):
        asset = sources.asset(kind)
        assert asset.url or kind == "redis"  # redis windows url may be empty until set
        assert asset.archive in ("bare", "zip", "tar.gz", "tar.xz")


def test_env_updates_includes_pg_and_redis_keys():
    cfg = DepsConfig(
        postgres_exe=__import__("pathlib").Path("/x/postgres"),
        redis_exe=__import__("pathlib").Path("/x/redis-server"),
        clickhouse_enabled=False,
    )
    updates = lifecycle.env_updates(cfg)
    assert updates["POSTGRES_HOST"] == "127.0.0.1"
    assert updates["POSTGRES_PORT"] == str(__import__("native_deps.postgres", fromlist=["DEFAULT_PORT"]).DEFAULT_PORT)
    assert updates["REDIS_HOST"] == "127.0.0.1"
    assert "REDIS_PREFIX_KEY" in updates
    assert "CLICKHOUSE_ADDR" not in updates


def test_env_updates_includes_control_plane_defaults():
    """Native single-host shapes must wire main→node_server without env_bootstrap."""
    updates = lifecycle.env_updates(DepsConfig())
    assert updates["AGENT_COMPOSE_BASE_URL"] == "http://127.0.0.1:8003"
    assert updates["NODE_CONTROL_HOST"] == "127.0.0.1"
    assert updates["NODE_CONTROL_PORT"] == "8003"
    assert updates["AI_LUBRICANT_COMPAT_ENABLED"] == "true"


def test_env_updates_includes_clickhouse_only_when_enabled():
    no_ch = DepsConfig(clickhouse_enabled=False)
    assert "CLICKHOUSE_ADDR" not in lifecycle.env_updates(no_ch)
    with_ch = DepsConfig(clickhouse_enabled=True, clickhouse_exe=__import__("pathlib").Path("/x/clickhouse"))
    updates = lifecycle.env_updates(with_ch)
    assert updates["CLICKHOUSE_REQUEST_PAYLOAD_ENABLED"] == "true"
    assert updates["CLICKHOUSE_ADDR"].startswith("127.0.0.1:")


def test_redis_version_gate_rejects_old_redis():
    with pytest.raises(RuntimeError, match="RESP3 HELLO"):
        redis.assert_version_supported("5.0.14")
    with pytest.raises(RuntimeError):
        redis.assert_version_supported("garbage")
    redis.assert_version_supported("7.2.5")
    redis.assert_version_supported("6.0.0")


def test_write_env_file_is_idempotent_and_respects_existing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("POSTGRES_HOST=external.example\n", encoding="utf-8")
    lifecycle.write_env_file(env, {
        "POSTGRES_HOST": "127.0.0.1",  # must not overwrite the existing value
        "REDIS_HOST": "127.0.0.1",
    })
    text = env.read_text(encoding="utf-8")
    assert "external.example" in text
    assert "127.0.0.1" in text
    # Existing key preserved, new key appended exactly once.
    assert text.count("POSTGRES_HOST=external.example") == 1
    assert text.count("REDIS_HOST=127.0.0.1") == 1


def test_ensure_security_keys_generates_and_persists_once(tmp_path, monkeypatch):
    """Token must be stable across calls so main + node_server read the same value."""
    monkeypatch.delenv("NODE_CONTROL_TOKEN", raising=False)
    monkeypatch.delenv("NODE_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("AGENT_ATTACHMENT_SIGNING_KEY", raising=False)
    env = tmp_path / ".env"
    lifecycle.ensure_security_keys(env)
    first = env.read_text(encoding="utf-8")
    assert "NODE_CONTROL_TOKEN=" in first
    assert "NODE_CREDENTIAL_ENCRYPTION_KEY=" in first
    assert "AGENT_ATTACHMENT_SIGNING_KEY=" in first
    # Second call is a no-op (keys already in os.environ from the first call).
    env.write_text("", encoding="utf-8")
    lifecycle.ensure_security_keys(env)
    assert env.read_text(encoding="utf-8") == ""
