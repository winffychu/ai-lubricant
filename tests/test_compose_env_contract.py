"""Contract test between ``docker-compose.yml`` and ``.env.example``.

Rule (design §3.8 layer 1): every variable interpolated by compose must either
carry its own ``:-default`` **or** be declared in ``.env.example``. That keeps a
future "add ``${NEW_VAR}`` without a default and forget the template" change from
silently breaking ``docker compose up`` for a fresh clone.

No Docker needed — this only parses the two files.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

_COMPOSE = _PROJ / "docker-compose.yml"
_ENV_EXAMPLE = _PROJ / ".env.example"

# ${VAR} / ${VAR:-default} / ${VAR-default} — capture name + the rest of the expr.
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)([^}]*)\}")


def _compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.partition("=")[0].strip())
    return keys


def _interpolations() -> list[tuple[str, str]]:
    """(variable name, rest-of-expression) for every compose interpolation."""
    return [(m.group(1), m.group(2)) for m in _INTERPOLATION.finditer(_compose_text())]


def test_compose_file_is_parseable_yaml():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_compose_text())
    assert isinstance(data, dict) and "services" in data


def test_compose_file_is_not_empty():
    assert _interpolations(), "未在 docker-compose.yml 中解析到任何 ${...} 插值，正则可能已失效"


def test_every_interpolation_has_a_default_or_is_in_the_template():
    declared = _env_example_keys()
    offenders: list[str] = []
    for name, rest in _interpolations():
        has_default = rest.startswith(":-") or (rest.startswith("-") and not rest.startswith(":-"))
        if not has_default and name not in declared:
            offenders.append(name)
    assert not offenders, (
        "docker-compose.yml 里这些插值既没有 :-默认值、也不在 .env.example 中："
        f"{sorted(set(offenders))}（新部署会在 compose 插值阶段报 variable is not set）"
    )


def test_bare_interpolations_without_default_are_zero():
    """Design §3.2: today compose has *no* ``${VAR}`` without a fallback."""
    bare = [name for name, rest in _interpolations() if not rest.startswith(":-") and not rest.startswith("-")]
    assert bare == [], f"发现无默认值的裸插值: {bare}"


def test_template_covers_the_db_and_redis_service_wiring():
    """The keys compose reads for the app services must exist in the template."""
    declared = _env_example_keys()
    for key in (
        "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DATABASE", "POSTGRES_PORT",
        "REDIS_PORT", "MIRROR_MODE",
    ):
        assert key in declared, f".env.example 缺少 compose 会用到的 {key}"
