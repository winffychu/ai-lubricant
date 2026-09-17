"""Contract tests for ``.env.example``.

A ``.env`` derived from the template must satisfy every startup validator — that
is what makes "``cp .env.example .env`` and run" work after the template was
slimmed down (see deliverables/hardening-design.md §3).

No Docker, no database: the template's own values are injected into the
environment and the bootstrap validators must accept them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJ = Path(__file__).resolve().parents[1]
for _p in (str(_PROJ), str(_PROJ / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_ENV_EXAMPLE = _PROJ / ".env.example"

# Keys the app resolves from the environment (bootstrap_config + user_platform +
# marketplace). Cleared before loading the template so the test sees only what
# the template provides — otherwise a value leaking in from the CI job env would
# mask a missing template key.
_BOOTSTRAP_KEYS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE", "POSTGRES_DB", "POSTGRES_POOL_MIN_SIZE", "POSTGRES_POOL_MAX_SIZE",
    "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PREFIX_KEY", "REDIS_DECODE_RESPONSES",
    "REDIS_MAX_CONNECTIONS", "REDIS_STREAM_TIMEOUT", "REDIS_POOL_TIMEOUT",
    "CLICKHOUSE_REQUEST_PAYLOAD_ENABLED", "CLICKHOUSE_ADDR", "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_USERNAME", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_REQUEST_PAYLOAD_TTL_DAYS",
    "CLICKHOUSE_MAX_PAYLOAD_BYTES",
    "AI_LUBRICANT_DATABASE_URL", "AI_LUBRICANT_COMPAT_ENABLED",
    "AI_LUBRICANT_USER_ADAPTER_ENABLED", "AI_LUBRICANT_SYSTEM_USER_ID",
    "AI_LUBRICANT_SYSTEM_USER_NAME", "AI_LUBRICANT_SYSTEM_USER_EMAIL",
    "AGENT_COMPOSE_BASE_URL", "AGENT_COMPOSE_TIMEOUT", "NODE_CONTROL_TOKEN",
    "NODE_CREDENTIAL_ENCRYPTION_KEY", "AGENT_COMPOSE_NODE_SERVER_PUBLIC_URL",
    "AGENT_ATTACHMENT_SIGNING_KEY",
    "AI_LUBRICANT_BOOTSTRAP_ADMIN_EMAIL", "AI_LUBRICANT_BOOTSTRAP_ADMIN_PASSWORD",
    "AI_LUBRICANT_BOOTSTRAP_ADMIN_NAME",
    "MARKETPLACE_REPO_URL", "MARKETPLACE_GITHUB_BRANCH", "MARKETPLACE_GITHUB_TOKEN",
    "MARKETPLACE_MODULES", "MARKETPLACE_INDEX_NAME",
)

# The 16 keys the template must always carry (design §3.6 A/B/C).
REQUIRED_KEYS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "REDIS_HOST", "REDIS_PORT", "REDIS_PREFIX_KEY", "REDIS_DB",
    "REDIS_DECODE_RESPONSES", "REDIS_MAX_CONNECTIONS",
    "NODE_CONTROL_TOKEN", "NODE_CREDENTIAL_ENCRYPTION_KEY", "AGENT_ATTACHMENT_SIGNING_KEY",
    "AI_LUBRICANT_BOOTSTRAP_ADMIN_EMAIL", "AI_LUBRICANT_BOOTSTRAP_ADMIN_PASSWORD",
)

# Keys that must be non-empty because the app hard-fails on an empty value
# (`_required_text` / `_required_int` / `_required_bool` in server/bootstrap_config.py).
NON_EMPTY_KEYS = (
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "POSTGRES_DATABASE",
    "REDIS_HOST", "REDIS_PORT", "REDIS_PREFIX_KEY", "REDIS_DB",
    "REDIS_DECODE_RESPONSES", "REDIS_MAX_CONNECTIONS",
)

# Keys that must be *present* but are allowed to be empty on purpose:
#   * the three shared secrets — the deployer must fill them in before the
#     container is created (auto-generated values are written back too late);
#   * the bootstrap admin — empty means "no admin is seeded", the app still boots.
ALLOWED_EMPTY_KEYS = (
    "NODE_CONTROL_TOKEN", "NODE_CREDENTIAL_ENCRYPTION_KEY", "AGENT_ATTACHMENT_SIGNING_KEY",
    "AI_LUBRICANT_BOOTSTRAP_ADMIN_EMAIL", "AI_LUBRICANT_BOOTSTRAP_ADMIN_PASSWORD",
)


def _effective_keys(text: str) -> dict[str, str]:
    """Uncommented ``KEY=VALUE`` pairs (a commented key is not effective)."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


@pytest.fixture()
def template_text() -> str:
    return _ENV_EXAMPLE.read_text(encoding="utf-8")


@pytest.fixture()
def template_env(monkeypatch) -> dict[str, str]:
    """Inject the template into the process env exactly as ``.env`` would."""
    from dotenv import dotenv_values

    values = {k: v for k, v in dotenv_values(_ENV_EXAMPLE).items() if v is not None}
    for key in _BOOTSTRAP_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        if value:
            monkeypatch.setenv(key, value)
    return values


# ── structural assertions on the template itself ──────────────────────────────

def test_required_keys_present_and_non_empty(template_text):
    effective = _effective_keys(template_text)
    missing = [k for k in REQUIRED_KEYS if k not in effective]
    assert not missing, f".env.example 缺少必填键: {missing}"
    blank = [k for k in NON_EMPTY_KEYS if not effective.get(k)]
    assert not blank, f".env.example 中这些键不能为空（应用启动即失败）: {blank}"


def test_allowed_empty_keys_are_present_but_may_be_blank(template_text):
    effective = _effective_keys(template_text)
    for key in ALLOWED_EMPTY_KEYS:
        assert key in effective, f".env.example 必须保留 {key}（允许空值，但键要在）"


def test_no_placeholder_values_left(template_text):
    """``your_pg_user`` / ``your_pg_password`` were the previous CI-failure root
    cause: non-empty but unusable, so the app accepted them and then failed to
    connect. They must be gone."""
    effective = _effective_keys(template_text)
    for key, value in effective.items():
        lowered = value.lower()
        assert "your_" not in lowered, f"{key} 仍是占位符值: {value}"
        assert "changeme" not in lowered and "<" not in value, f"{key} 仍是占位符值: {value}"


def test_mirror_mode_stays_cn(template_text):
    """User decision D1: MIRROR_MODE=cn stays an effective key."""
    assert _effective_keys(template_text).get("MIRROR_MODE") == "cn"


def test_redis_prefix_key_unified(template_text):
    assert _effective_keys(template_text).get("REDIS_PREFIX_KEY") == "ai_lubricant"
    assert "marsview" not in template_text


def test_no_credentials_or_real_proxy_in_template(template_text):
    assert "127.0.0.1:7890" not in template_text
    assert "ghp_" not in template_text and "github_pat_" not in template_text


# ── the template must satisfy the app's startup validators ────────────────────

def test_template_satisfies_postgres_config(template_env):
    import bootstrap_config

    cfg = bootstrap_config.get_postgres_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 5432
    assert cfg["database"] == "ai-lubricant"


def test_template_satisfies_redis_config(template_env):
    import bootstrap_config

    cfg = bootstrap_config.get_redis_config()
    assert cfg["prefix_key"] == "ai_lubricant"
    assert cfg["decode_responses"] is True
    assert cfg["port"] == 6379


def test_template_satisfies_pool_limits(template_env):
    import bootstrap_config

    assert bootstrap_config.get_postgres_pool_limits() == {"min_size": 1, "max_size": 10}


def test_template_satisfies_clickhouse_config(template_env):
    import bootstrap_config

    cfg = bootstrap_config.get_clickhouse_config()
    assert cfg["enabled"] is False  # default off unless the optional key is enabled


def test_template_satisfies_user_platform_and_marketplace(template_env):
    from user_platform.config import load_settings as load_user_platform
    from user_platform.marketplace.config import load_settings as load_marketplace

    settings = load_user_platform()
    # Derived from the template's POSTGRES_* — a non-empty URL proves the
    # template carries everything the compatibility layer needs.
    assert settings.database_url.startswith("asyncpg://")
    assert "ai-lubricant" in settings.database_url

    market = load_marketplace()
    assert market.repo_url  # falls back to the built-in default


def test_database_name_mismatch_regression_guard(template_env, monkeypatch):
    """Regression guard for the "form E does not create the DB" defect.

    ``.env.example`` pins ``POSTGRES_DATABASE=ai-lubricant`` (hyphen) while
    ``native_deps.postgres.DEFAULT_DATABASE`` is ``ai_lubricant`` (underscore).
    The bootstrap must therefore create the *resolved* name — creating the
    module default would leave the target database missing.
    """
    import bootstrap_config
    from native_deps import lifecycle, postgres

    resolved_from_env = lifecycle.resolve_postgres_database(_ENV_EXAMPLE)
    assert resolved_from_env == "ai-lubricant"
    assert postgres.DEFAULT_DATABASE == "ai_lubricant"
    assert bootstrap_config.get_postgres_config()["database"] == resolved_from_env

    # And the file-only path (no env var at all) resolves the same name.
    monkeypatch.delenv("POSTGRES_DATABASE", raising=False)
    assert lifecycle.resolve_postgres_database(_ENV_EXAMPLE) == "ai-lubricant"
