"""Native (non-Docker) dependency provisioning for the three no-Docker launch shapes.

Owns: acquire (download/unpack/verify) + initialize (PG ``initdb``, Redis/CH
config) + start/stop/wait-healthy for PostgreSQL / Redis / ClickHouse, so the
existing service supervisor can bring the whole stack up without Docker.

Layering rule: this package must import cleanly from any entry (supervisord
``[program]`` wrapper, frozen exe, plain shell script) WITHOUT importing
``main`` / ``node_server`` / ``tunnel_server`` — those call ``load_project_env()``
at import time and pull heavy deps. Only stdlib + ``aiohttp`` + ``loguru`` +
(tiny stdlib-only probes) are used here.

Three shapes consume the same :mod:`native_deps.lifecycle`:
  * Windows exe      — ``desktop/main_window.py`` inserts a provision phase.
  * Linux supervisord — ``native_deps.cli supervisord-conf`` runs provision
    **including database creation** (start the local DBs once → create → stop)
    and then writes the conf; ``supervisord -c`` owns the DB processes.
  * Linux single-file — ``script/native_launch.sh`` → ``python -m native_deps.cli up``.
"""
