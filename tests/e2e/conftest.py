# region MODULE_CONTRACT
# PURPOSE: E2E test fixtures — PostgreSQL + SSH container pool, config, schema, log capture, and UoW-based DB access.
# SCOPE: Session-scoped containers (postgres + ssh_pool of two), config; function-scoped pg_conn/pg_executor/uow_factory with TRUNCATE, log_records (getMessage() + extra-diff assertions against _NATIVE_KEYS).
# DEPENDENCIES: USES API: testcontainers (PostgreSQL + SSH containers)
# KEYWORDS: e2e fixtures, SSH container pool, PostgresContainer, log capture
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import asyncssh
import pg8000.native
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from testcontainers.postgres import PostgresContainer

from yascheduler.application import MessageBus
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.infra.persistence import PostgresDbConfig, apply_migrations
from yascheduler.infra.persistence.postgres_schema import apply_schema
from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

    from yascheduler.entrypoints import Config


_SSH_IMAGE = "serversideup/docker-ssh"
_SSH_USERNAME = "testuser"
_YASCHEDULER_LOGGER = "yascheduler"


def _container_loopback_ip(index: int) -> str:
    """Return a distinct loopback address for pool container ``index``.

    `get_container_host_ip()` returns the docker host (e.g. ``localhost``)
    for every container in the default docker_host connection mode, which
    collapses the two-container pool into a single indistinguishable host.
    Published ports listen on the whole 127.0.0.0/8 range on Docker and on
    rootless podman, so distinct loopback addresses (127.0.0.1, 127.0.0.2, ...)
    give per-container reachable endpoints. Container bridge IPs are NOT used:
    on rootless podman the bridge lives in a dedicated network namespace and
    is unreachable from the host.
    """
    return f"127.0.0.{index + 1}"


class LogCaptureHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "/tests/e2e/" in str(item.path):
            item.add_marker("e2e")


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    with PostgresContainer("docker.io/library/postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def _db_config(postgres_container: PostgresContainer) -> PostgresDbConfig:
    url = urlparse(postgres_container.get_connection_url())
    return PostgresDbConfig(
        user=url.username or "test",
        password=url.password or "test",
        database=url.path.lstrip("/"),
        host=url.hostname or "localhost",
        port=url.port or 5432,
    )


@pytest.fixture(scope="session")
async def ssh_pool(
    tmp_path_factory: Any,
) -> AsyncGenerator[list[dict[str, Any]], None]:
    key_dir = tmp_path_factory.mktemp("ssh_keys")
    key_path = key_dir / "id_rsa"
    key = asyncssh.generate_private_key("ssh-rsa")
    public_key_str = key.export_public_key("openssh").decode().strip()
    key.write_private_key(str(key_path))

    containers: list[DockerContainer] = []
    try:
        for _ in range(2):
            c = DockerContainer(_SSH_IMAGE)
            c.with_env("SSH_USER", _SSH_USERNAME)
            c.with_env("AUTHORIZED_KEYS", public_key_str)
            c.with_env("ALLOWED_IPS", "AllowUsers testuser")
            c.with_exposed_ports(2222)
            c.waiting_for(LogMessageWaitStrategy("Server listening on"))
            c.start()
            containers.append(c)

        # Give sshd a moment to accept connections after the log line; the
        # wait strategy only confirms the listener, not a ready socket.
        import asyncio

        await asyncio.sleep(1)

        entries: list[dict[str, Any]] = []
        for i, c in enumerate(containers):
            host = _container_loopback_ip(i)
            entries.append(
                {
                    "host": host,
                    "port": int(c.get_exposed_port(2222)),
                    "username": _SSH_USERNAME,
                    "key_path": PurePosixPath(str(key_path)),
                },
            )
        assert entries[0]["host"] != entries[1]["host"], (
            "ssh_pool containers must have distinct host addresses; "
            f"got {entries[0]['host']} twice"
        )
        yield entries
    finally:
        for c in containers:
            c.stop()


@pytest.fixture(scope="session")
def ssh_container(ssh_pool: list[dict[str, Any]]) -> dict[str, Any]:
    # Backward-compat wrapper for test_consume_retry.py: the single-container
    # fixture is now the first entry of the pool. The pool shares one keypair,
    # so key_path/username are identical to the pre-2.3.0 single-container case.
    return ssh_pool[0]


@pytest.fixture(scope="session")
def e2e_config(
    tmp_path_factory: Any,
    _db_config: PostgresDbConfig,
    ssh_pool: list[dict[str, Any]],
) -> Config:
    # ssh_pool shares one keypair across both containers; index 0 carries the
    # shared username/key_path that the single-container fixture used to provide.
    ssh = ssh_pool[0]
    tmp = tmp_path_factory.mktemp("e2e_config")
    data_dir = tmp / "data"

    ini_path = tmp / "yascheduler.conf"
    db_cfg = _db_config
    ini_content = (
        f"[db]\n"
        f"host = {db_cfg.host}\n"
        f"port = {db_cfg.port}\n"
        f"user = {db_cfg.user}\n"
        f"password = {db_cfg.password}\n"
        f"database = {db_cfg.database}\n"
        f"\n"
        f"[local]\n"
        f"data_dir = {data_dir}\n"
        f"\n"
        f"[remote]\n"
        f"user = {ssh['username']}\n"
        f"\n"
        f"[engine.test_shell]\n"
        f"spawn = {{engine_path}}/run.sh\n"
        f"check_pname = sleep\n"
        f"input_files = 1.input\n"
        f"output_files = 1.input.out\n"
        f"deploy_local_files = run.sh\n"
        f"sleep_interval = 1\n"
        f"platforms = linux\n"
    )
    ini_path.write_text(ini_content)

    engines_dir = tmp / "data" / "engines" / "test_shell"
    engines_dir.mkdir(parents=True)
    run_sh = engines_dir / "run.sh"
    run_sh.write_text("#!/bin/sh\nsleep 3\ncat 1.input > 1.input.out\n")
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)

    keys_dir = tmp / "data" / "keys"
    keys_dir.mkdir(parents=True)
    src = Path(str(ssh["key_path"]))
    dst = keys_dir / src.name
    dst.symlink_to(src)

    os.environ["YASCHEDULER_CONF_PATH"] = str(ini_path)
    return parse_config(str(ini_path))


@pytest.fixture(scope="session")
def _init_schema(
    _db_config: PostgresDbConfig,
) -> None:
    """Apply schema once per session, then apply pending migrations."""
    apply_schema(_db_config)
    apply_migrations(_db_config)


@pytest.fixture(scope="session")
def _bus() -> MessageBus:
    return MessageBus()


@pytest.fixture
def pg_executor() -> Generator[ThreadPoolExecutor, None, None]:
    executor = ThreadPoolExecutor(max_workers=1)
    yield executor
    executor.shutdown(wait=False)


@pytest.fixture
async def pg_conn(
    _db_config: PostgresDbConfig,
    _init_schema: None,
    pg_executor: ThreadPoolExecutor,
) -> AsyncGenerator[pg8000.native.Connection, None]:
    conn = pg8000.native.Connection(
        user=_db_config.user,
        host=_db_config.host,
        database=_db_config.database,
        port=_db_config.port,
        password=_db_config.password,
    )
    yield conn
    conn.run("TRUNCATE yascheduler_tasks, yascheduler_nodes CASCADE")
    conn.close()
    pg_executor.shutdown(wait=False)


@pytest.fixture
def uow_factory(
    _db_config: PostgresDbConfig,
    _init_schema: None,
    _bus: MessageBus,
    pg_conn: pg8000.native.Connection,
) -> Callable[[], PostgresUnitOfWork]:
    def _factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(_db_config, _bus)

    return _factory


@pytest.fixture
def log_records() -> Generator[list[logging.LogRecord], None, None]:
    logger = logging.getLogger(_YASCHEDULER_LOGGER)
    records: list[logging.LogRecord] = []
    handler = LogCaptureHandler(records)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
