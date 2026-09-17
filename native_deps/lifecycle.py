"""Orchestrator for native PG/Redis/ClickHouse across the three launch shapes.

Two layers:
  * :func:`ensure_all` — download + initialize only (no process spawn). Used by
    the supervisord shape (the DBs run as ``[program]`` blocks) and as the
    first phase of the exe/script shapes.
  * :class:`DepsRuntime` — spawns the three DB processes as children, waits
    for protocol-level readiness, creates databases, tears them down on exit.
    Used by the exe and Linux-single-file shapes.

:func:`ensure_databases` bridges the two: the supervisord shape has no
:class:`DepsRuntime`, so it calls this once before generating the conf to create
the target databases (start → wait → create → stop, idempotent).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from . import binaries, clickhouse, layout, postgres, redis
from .binaries import DownloadError


@dataclass
class DepsConfig:
    postgres_exe: Path | None = None
    redis_exe: Path | None = None
    clickhouse_exe: Path | None = None
    clickhouse_enabled: bool = False

    @property
    def postgres_local(self) -> bool:
        return self.postgres_exe is not None

    @property
    def redis_local(self) -> bool:
        return self.redis_exe is not None


def _env_default(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _read_env_file(env_file: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from an env file (blanks/comments ignored)."""
    values: dict[str, str] = {}
    if not env_file.exists():
        return values
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def resolve_postgres_database(env_file: Path) -> str:
    """Return the *actual* database name the app will connect to.

    Mirrors the app's own resolution exactly (``server/bootstrap_config.py``):

    * the value is looked up under **both** ``POSTGRES_DATABASE`` and the legacy
      alias ``POSTGRES_DB``, first non-empty wins — the app uses
      ``_required_text(("POSTGRES_DATABASE", "POSTGRES_DB"), ...)``;
    * real environment variables win over the ``.env`` file, matching
      ``python-dotenv``'s ``override=False``: the file is merged *under* the
      environment, not consulted in a second pass. (Merging matters: with
      ``.env`` holding ``POSTGRES_DATABASE=A`` and the environment holding
      ``POSTGRES_DB=B``, a two-pass lookup would pick ``B`` while the app picks
      ``A`` — and we would create the wrong database.)

    Using the resolved name instead of :data:`postgres.DEFAULT_DATABASE` is
    mandatory: ``.env.example`` / ``docker-compose.yml`` ship
    ``POSTGRES_DATABASE=ai-lubricant`` while the module default is
    ``ai_lubricant`` — creating the wrong one leaves the target DB missing and
    the app still fails with ``InvalidCatalogNameError``.
    """
    merged = dict(_read_env_file(env_file))
    merged.update(os.environ)  # dotenv override=False：真实环境变量优先
    for name in ("POSTGRES_DATABASE", "POSTGRES_DB"):
        value = (merged.get(name) or "").strip()
        if value:
            return value
    return postgres.DEFAULT_DATABASE


async def ensure_all(clickhouse_enabled: bool | None = None) -> DepsConfig:
    """Download binaries and initialize data dirs/configs. No processes started."""
    if clickhouse_enabled is None:
        clickhouse_enabled = os.environ.get("CLICKHOUSE_REQUEST_PAYLOAD_ENABLED", "").lower() in ("1", "true", "yes")

    pg_exe = await _ensure_or_none("postgres", required=True)
    redis_exe = await _ensure_or_none("redis", required=True)
    ch_exe = None
    if clickhouse_enabled:
        ch_exe = await _ensure_or_none("clickhouse", required=False)

    # PG initdb (idempotent). Redis/ClickHouse configs are written lazily in
    # their modules' start_command helpers; touch them now so a supervisord
    # ``command=`` finds the config without a prior foreground run.
    if pg_exe is not None:
        postgres.ensure_cluster(pg_exe)
    redis.config_path()
    if ch_exe is not None:
        clickhouse.config_path()
        clickhouse.data_root().mkdir(parents=True, exist_ok=True)
        (clickhouse.data_root() / "tmp").mkdir(parents=True, exist_ok=True)
        (clickhouse.data_root() / "user_files").mkdir(parents=True, exist_ok=True)

    return DepsConfig(postgres_exe=pg_exe, redis_exe=redis_exe,
                      clickhouse_exe=ch_exe, clickhouse_enabled=clickhouse_enabled and ch_exe is not None)


async def _ensure_or_none(kind: str, *, required: bool) -> Path | None:
    try:
        exe = await binaries.ensure_dep(kind)
        logger.info("[native-deps] {} ready: {}", kind, exe)
        return exe
    except DownloadError as exc:
        if required:
            raise
        logger.warning("[native-deps] {} skipped: {}", kind, exc)
        return None


async def ensure_databases(cfg: DepsConfig, env_file: Path) -> None:
    """Start the local DBs once, create the target databases, then stop them.

    Idempotent. Required by the supervisord shape: :func:`ensure_all` only runs
    ``initdb`` and never starts a process, while ``ensure_database`` otherwise
    lives on the :class:`DepsRuntime` path (``up`` / exe) — so supervisord would
    start the app programs against a database that does not exist yet, and they
    would restart until FATAL with ``InvalidCatalogNameError``. Same approach the
    codespace entrypoint used inline.
    """
    if cfg.postgres_local and cfg.postgres_exe is not None:
        database = resolve_postgres_database(env_file)
        child = subprocess.Popen(postgres.start_command(cfg.postgres_exe))
        try:
            if not await postgres.wait_ready(cfg.postgres_exe, timeout=60):
                raise RuntimeError("postgres did not become ready; cannot create database")
            postgres.ensure_database(cfg.postgres_exe, database)
            logger.info("[native-deps] postgres database ensured: {}", database)
        finally:
            with contextlib.suppress(Exception):
                postgres.stop(cfg.postgres_exe)
            with contextlib.suppress(Exception):
                child.wait(timeout=15)
            if child.poll() is None:
                with contextlib.suppress(Exception):
                    child.kill()
    if cfg.clickhouse_enabled and cfg.clickhouse_exe is not None:
        child = subprocess.Popen(clickhouse.start_command(cfg.clickhouse_exe))
        try:
            if await clickhouse.wait_ready():
                clickhouse.ensure_database(cfg.clickhouse_exe)
                logger.info("[native-deps] clickhouse database ensured: {}", clickhouse.DEFAULT_DATABASE)
            else:
                logger.warning("[native-deps] clickhouse not ready; skipping database create (optional)")
        finally:
            with contextlib.suppress(Exception):
                clickhouse.stop(cfg.clickhouse_exe)
            with contextlib.suppress(Exception):
                child.wait(timeout=15)
            if child.poll() is None:
                with contextlib.suppress(Exception):
                    child.kill()


def env_updates(cfg: DepsConfig) -> dict[str, str]:
    """Connection keys to merge into ``.env`` so the app services find local deps."""
    updates: dict[str, str] = {}
    if cfg.postgres_local:
        updates.update({
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(postgres.DEFAULT_PORT),
            "POSTGRES_USER": postgres.DEFAULT_USER,
            "POSTGRES_PASSWORD": postgres.DEFAULT_USER,
            "POSTGRES_DATABASE": postgres.DEFAULT_DATABASE,
        })
    if cfg.redis_local:
        updates.update({
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": str(redis.DEFAULT_PORT),
            "REDIS_DB": "0",
            "REDIS_PREFIX_KEY": "ai_lubricant",
            "REDIS_DECODE_RESPONSES": "true",
            "REDIS_MAX_CONNECTIONS": "500",
        })
    if cfg.clickhouse_enabled:
        updates.update({
            "CLICKHOUSE_ADDR": f"127.0.0.1:{clickhouse.DEFAULT_HTTP_PORT}",
            "CLICKHOUSE_DATABASE": clickhouse.DEFAULT_DATABASE,
            "CLICKHOUSE_USERNAME": "default",
            "CLICKHOUSE_PASSWORD": "",
            "CLICKHOUSE_REQUEST_PAYLOAD_ENABLED": "true",
        })
    # Control-plane wiring for the native single-host shapes: the main service
    # reaches node_server over loopback, and the shared token must be identical
    # in both processes (node_server reads the same .env). Without these the
    # node page reports "未配置控制面 agent_compose_base_url/token".
    updates.update({
        "AGENT_COMPOSE_BASE_URL": "http://127.0.0.1:8003",
        "NODE_CONTROL_HOST": "127.0.0.1",
        "NODE_CONTROL_PORT": "8003",
        "AGENT_COMPOSE_NODE_SERVER_ENABLED": "true",
        "TUNNEL_RUNTIME_ENABLED": "true",
        "AI_LUBRICANT_COMPAT_ENABLED": "true",
    })
    return updates


def _generate_token() -> str:
    """Mirror desktop/env_bootstrap + node_server/wiring (secrets.token_urlsafe(32))."""
    import secrets

    return secrets.token_urlsafe(32)


def _generate_master_key() -> str:
    """Mirror node_server/wiring (secrets.token_hex(32)). Non-rotatable."""
    import secrets

    return secrets.token_hex(32)


def ensure_security_keys(env_file: Path) -> None:
    """Generate + persist the shared control-plane secrets once.

    ``NODE_CONTROL_TOKEN`` must be identical in main and node_server; generating
    it here (idempotent, append-if-missing) lets the native shapes skip
    ``desktop.env_bootstrap`` while still giving both processes the same value.
    ``NODE_CREDENTIAL_ENCRYPTION_KEY`` is non-rotatable; ``AGENT_ATTACHMENT_SIGNING_KEY``
    mirrors attachment_signing.ensure_signing_key.
    """
    keys: dict[str, str] = {}
    if not os.environ.get("NODE_CONTROL_TOKEN"):
        keys["NODE_CONTROL_TOKEN"] = _generate_token()
    if not os.environ.get("NODE_CREDENTIAL_ENCRYPTION_KEY"):
        keys["NODE_CREDENTIAL_ENCRYPTION_KEY"] = _generate_master_key()
    if not os.environ.get("AGENT_ATTACHMENT_SIGNING_KEY"):
        import secrets

        keys["AGENT_ATTACHMENT_SIGNING_KEY"] = secrets.token_urlsafe(48)
    if not keys:
        return
    write_env_file(env_file, keys)
    # write_env_file only sets os.environ for keys absent from the file; the
    # generated values were just appended so they are absent from the in-memory
    # existing-dict — set them explicitly so the current process (and children
    # spawned right after) see them without a re-read.
    for key, value in keys.items():
        os.environ[key] = value


def write_env_file(env_file: Path, updates: dict[str, str]) -> None:
    """Append missing keys (respect existing user config), mirror env_bootstrap."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            existing[key.strip()] = value
    pending = [f"{k}={v}" for k, v in updates.items() if not existing.get(k)]
    if not pending:
        return
    lines: list[str] = []
    if env_file.exists() and env_file.stat().st_size:
        lines.append("")
        lines.append("# native-deps")
    lines.extend(pending)
    with env_file.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    for key, value in updates.items():
        if not existing.get(key):
            os.environ[key] = value


class DepsRuntime:
    """Foreground process group for the exe/script shapes."""

    def __init__(self, cfg: DepsConfig) -> None:
        self._cfg = cfg
        self._procs: list[tuple[str, subprocess.Popen]] = []

    def _popen(self, label: str, argv: list[str]) -> subprocess.Popen:
        log_path = layout.logs_dir() / f"{label}.log"
        log_fh = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )
        proc = subprocess.Popen(
            argv, stdout=log_fh, stderr=subprocess.STDOUT,
            env=os.environ.copy(), cwd=str(layout.root()),
            creationflags=creationflags,
        )
        self._procs.append((label, proc))
        return proc

    async def start_all(self) -> bool:
        cfg = self._cfg
        if cfg.postgres_local and cfg.postgres_exe is not None:
            self._popen("postgres", postgres.start_command(cfg.postgres_exe))
            if not await postgres.wait_ready(cfg.postgres_exe):
                logger.error("[native-deps] postgres did not become ready")
                return False
            with contextlib.suppress(Exception):
                postgres.ensure_database(cfg.postgres_exe)
        if cfg.redis_local and cfg.redis_exe is not None:
            self._popen("redis", redis.start_command(cfg.redis_exe))
            if not await redis.wait_ready():
                logger.error("[native-deps] redis did not become ready")
                return False
        if cfg.clickhouse_enabled and cfg.clickhouse_exe is not None:
            self._popen("clickhouse", clickhouse.start_command(cfg.clickhouse_exe))
            if not await clickhouse.wait_ready():
                logger.warning("[native-deps] clickhouse did not become ready (optional)")
            else:
                with contextlib.suppress(Exception):
                    clickhouse.ensure_database(cfg.clickhouse_exe)
        return True

    def stop_all(self) -> None:
        # Graceful via per-service CLI where available, then terminate children.
        cfg = self._cfg
        if cfg.postgres_exe is not None:
            with contextlib.suppress(Exception):
                postgres.stop(cfg.postgres_exe)
        if cfg.clickhouse_exe is not None:
            with contextlib.suppress(Exception):
                clickhouse.stop(cfg.clickhouse_exe)
        for _, proc in reversed(self._procs):
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.terminate()
        deadline = __import__("time").monotonic() + 5
        for _, proc in self._procs:
            with contextlib.suppress(Exception):
                remaining = max(0.0, deadline - __import__("time").monotonic())
                proc.wait(timeout=remaining)
        for _, proc in self._procs:
            with contextlib.suppress(Exception):
                if proc.poll() is None:
                    proc.kill()
