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

from native_deps import lifecycle, postgres, redis, sources
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


def test_resolve_postgres_database_prefers_env_file_over_default(tmp_path, monkeypatch):
    """``.env`` may pin ``ai-lubricant`` (hyphen) while the module default is
    ``ai_lubricant`` (underscore). The resolver must follow the .env value —
    otherwise the bootstrap creates the wrong DB and the app still fails."""
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    env = tmp_path / ".env"
    env.write_text("POSTGRES_DATABASE=ai-lubricant\n", encoding="utf-8")
    assert lifecycle.resolve_postgres_database(env) == "ai-lubricant"
    assert lifecycle.resolve_postgres_database(env) != postgres.DEFAULT_DATABASE


def test_resolve_postgres_database_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_DATABASE", "from-env")
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    env = tmp_path / ".env"
    env.write_text("POSTGRES_DATABASE=from-file\n", encoding="utf-8")
    assert lifecycle.resolve_postgres_database(env) == "from-env"


def test_resolve_postgres_database_accepts_env_alias_postgres_db(tmp_path, monkeypatch):
    """The app reads ``_required_text(("POSTGRES_DATABASE", "POSTGRES_DB"))`` —
    the resolver must honour the legacy alias too, otherwise it creates the
    module-default DB while the app connects to the aliased one."""
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.setenv("POSTGRES_DB", "alias-from-env")
    env = tmp_path / ".env"
    assert lifecycle.resolve_postgres_database(env) == "alias-from-env"


def test_resolve_postgres_database_accepts_file_alias_postgres_db(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    env = tmp_path / ".env"
    env.write_text("POSTGRES_DB=alias-from-file\n", encoding="utf-8")
    assert lifecycle.resolve_postgres_database(env) == "alias-from-file"


def test_resolve_postgres_database_matches_dotenv_override_false(tmp_path, monkeypatch):
    """``python-dotenv`` loads ``.env`` with ``override=False``: the file is
    merged *under* the environment. So with the file holding
    ``POSTGRES_DATABASE`` and the environment holding the alias ``POSTGRES_DB``,
    the app keeps the **file** value (``POSTGRES_DATABASE`` wins by key order,
    and it is present because the environment never set it).

    A naive two-pass lookup (env ``POSTGRES_DB`` first, then file) would return
    ``alias-from-env`` here and create the wrong database. This is the case that
    pins the merge semantics.
    """
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.setenv("POSTGRES_DB", "alias-from-env")
    env = tmp_path / ".env"
    env.write_text("POSTGRES_DATABASE=file-wins\n", encoding="utf-8")
    assert lifecycle.resolve_postgres_database(env) == "file-wins"


def test_resolve_postgres_database_falls_back_to_module_default(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    env = tmp_path / ".env"
    assert lifecycle.resolve_postgres_database(env) == postgres.DEFAULT_DATABASE
    env.write_text("# only comments\n\n", encoding="utf-8")
    assert lifecycle.resolve_postgres_database(env) == postgres.DEFAULT_DATABASE


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
