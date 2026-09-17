"""Command-line entry: ``python -m native_deps.cli {provision,up,down,status}``.

* ``provision`` — download + initdb + write configs + write ``.env``. No process
  spawn.
* ``up``       — provision + start the DBs + start main/node/tunnel in the
  foreground (the Linux single-file shape). Traps signals for clean teardown.
* ``down``     — stop a running native stack (best-effort by pidfile/probe).
* ``supervisord-conf`` — provision + **create the target databases** (start the
  local DBs once, create, stop) + write the supervisord conf. Used by the
  supervisord shape (form E / codespace) before ``supervisord -c``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from loguru import logger

from . import lifecycle, postgres, redis, clickhouse


def _env_file() -> Path:
    override = os.environ.get("DESKTOP_ENV_FILE", "").strip()
    if override:
        return Path(override)
    try:
        from desktop.paths import env_file_path

        return env_file_path()
    except Exception:
        return Path.home() / ".ailubricant" / ".env"


async def cmd_provision(args: argparse.Namespace) -> int:
    env_file = _env_file()
    lifecycle.ensure_security_keys(env_file)
    cfg = await lifecycle.ensure_all()
    lifecycle.write_env_file(env_file, lifecycle.env_updates(cfg))
    print(f"postgres: {cfg.postgres_exe}")
    print(f"redis:    {cfg.redis_exe}")
    if cfg.clickhouse_enabled:
        print(f"clickhouse: {cfg.clickhouse_exe}")
    print(f"env file: {env_file}")
    return 0


async def cmd_up(args: argparse.Namespace) -> int:
    env_file = _env_file()
    lifecycle.ensure_security_keys(env_file)
    cfg = await lifecycle.ensure_all()
    lifecycle.write_env_file(env_file, lifecycle.env_updates(cfg))
    deps = lifecycle.DepsRuntime(cfg)
    if not await deps.start_all():
        logger.error("[native-deps] dependency startup failed; aborting")
        deps.stop_all()
        return 1

    # Reload .env so the app processes inherit the resolved connection keys
    # and the freshly generated control-plane token.
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=env_file, override=True)

    stopping = {"flag": False}

    def _stop(*_: object) -> None:
        if stopping["flag"]:
            return
        stopping["flag"] = True
        logger.info("[native-deps] stopping stack…")
        deps.stop_all()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with __import__("contextlib").suppress(Exception):
            signal.signal(sig, _stop)

    # Reuse the existing desktop supervisor for the three app services — it
    # already handles serial start, health waits and Windows Job teardown.
    from desktop.supervisor import Supervisor

    supervisor = Supervisor()
    try:
        if not supervisor.start_all():
            logger.error("[native-deps] app services failed to start")
            return 1
        # Block until interrupted. The supervisor's children run detached.
        while not stopping["flag"]:
            __import__("time").sleep(1)
    finally:
        supervisor.stop_all()
        deps.stop_all()
    return 0


async def cmd_down(args: argparse.Namespace) -> int:
    # Best-effort: graceful-stop via the resolved binaries (downloaded or PATH).
    for kind, module in (("postgres", postgres), ("clickhouse", clickhouse)):
        try:
            exe = await __import__("native_deps.binaries", fromlist=["ensure_dep"]).ensure_dep(kind)
            if kind == "postgres":
                module.stop(exe)
            else:
                module.stop(exe)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[native-deps] stop {}: {}", kind, exc)
    return 0


async def cmd_supervisord_conf(args: argparse.Namespace) -> int:
    """Provision + create the target databases + generate the supervisord conf.

    The supervisord shape never runs :class:`lifecycle.DepsRuntime` (the DBs are
    ``[program]`` blocks), so the database-create step has to happen here, before
    the conf is generated and supervisord starts the app programs.
    """
    from . import supervisord_config

    env_file = _env_file()
    lifecycle.ensure_security_keys(env_file)
    cfg = await lifecycle.ensure_all()
    lifecycle.write_env_file(env_file, lifecycle.env_updates(cfg))
    # Start PG (and ClickHouse when enabled) once, create the resolved databases,
    # stop them again — idempotent, and must run before supervisord comes up.
    await lifecycle.ensure_databases(cfg, env_file)
    conf = supervisord_config.generate(cfg)
    print(f"supervisord config: {conf}")
    print(f"run: supervisord -c {conf} -n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="native_deps", description="Native dependency launcher")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("provision", help="download + init, no process spawn")
    sub.add_parser("up", help="provision + start DBs + app services (foreground)")
    sub.add_parser("down", help="best-effort graceful stop")
    sub.add_parser("supervisord-conf", help="provision + generate supervisord config")
    args = parser.parse_args()
    handlers = {"provision": cmd_provision, "up": cmd_up, "down": cmd_down,
                 "supervisord-conf": cmd_supervisord_conf}
    return asyncio.run(handlers[args.cmd](args))


if __name__ == "__main__":
    raise SystemExit(main())
