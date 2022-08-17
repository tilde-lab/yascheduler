#!/usr/bin/env python3

import asyncio
import logging
from asyncio.locks import Event, Semaphore
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from functools import partial
from pathlib import PurePath
from typing import AsyncGenerator, Optional, Pattern, Sequence, Set, Type, Union

import asyncssh
import backoff
from asyncssh.client import SSHClient
from asyncssh.connection import SSHClientConnection, SSHClientConnectionOptions
from asyncssh.process import SSHClientProcess, SSHCompletedProcess
from asyncssh.public_key import SSHKey
from asyncssh.sftp import SFTPClient
from asyncstdlib import all as aall
from asyncstdlib import map as amap
from attrs import define, field, validators
from typing_extensions import Self

from .adapters import (
    debian_adapter,
    debian_bullseye_adapter,
    debian_buster_adapter,
    debian_like_adapter,
    linux_adapter,
    windows7_adapter,
    windows8_adapter,
    windows10_adapter,
    windows11_adapter,
    windows_adapter,
)
from .exc import PlatformGuessFailed
from .protocol import (
    AllSSHRetryExc,
    PEngine,
    PEngineRepository,
    PProcessInfo,
    PRemoteMachine,
    PRemoteMachineAdapter,
    PRemoteMachineMetadata,
    SSHCheck,
    SSHRetryExc,
)

ADAPTERS: Sequence[PRemoteMachineAdapter] = [
    debian_bullseye_adapter,
    debian_buster_adapter,
    debian_adapter,
    debian_like_adapter,
    linux_adapter,
    windows11_adapter,
    windows10_adapter,
    windows8_adapter,
    windows7_adapter,
    windows_adapter,
]

# default value of MaxSessions on OpenSSH server is 10
MAX_SESSIONS = 10

my_backoff_exc = partial(
    backoff.on_exception,
    wait_gen=backoff.fibo,
    max_time=60,
    exception=SSHRetryExc,
)


class MySSHClient(SSHClient):
    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: SSHKey
    ) -> bool:
        "Trust all host keys"
        # NOTE: this is insecure for MiM attacks
        return True


DEFAULT_CONN_OPTS = SSHClientConnectionOptions(
    client_factory=MySSHClient,
    preferred_auth="publickey",
    keepalive_interval=10,
    keepalive_count_max=10,
    compression_algs=[],
    agent_path="",
    username="root",
)


@define
class RemoteMachineMetadata(PRemoteMachineMetadata):
    def __init__(self):
        self._busy = None
        self._free_since: Optional[datetime] = datetime.now()

    @property
    def busy(self) -> Optional[bool]:
        return self._busy

    @busy.setter
    def busy(self, new_busy: bool):
        if new_busy:
            self._busy = True
            self._free_since = None
        else:
            self._busy = False
            self._free_since = datetime.now()

    def is_free_longer_than(self, delta: timedelta) -> bool:
        if not self._free_since or self.busy:
            return False
        return datetime.now() - delta > self._free_since


@define(frozen=True)
class RemoteMachine(PRemoteMachine):
    "Remote SSH machine"
    conn: SSHClientConnection = field()
    conn_opts: SSHClientConnectionOptions = field()

    meta: RemoteMachineMetadata = field()

    adapter: PRemoteMachineAdapter = field()
    log: logging.Logger = field()

    platforms: Sequence[str] = field()

    data_dir: PurePath = field(validator=validators.instance_of(PurePath))
    engines_dir: PurePath = field(validator=validators.instance_of(PurePath))
    tasks_dir: PurePath = field(validator=validators.instance_of(PurePath))

    cancellation_event: Event = field(factory=Event, init=False)
    jobs: Set[asyncio.Task] = field(factory=set, init=False)
    sessions_limit: Semaphore = field(
        factory=lambda: Semaphore(MAX_SESSIONS), init=False
    )

    def __le__(self, other: Self) -> bool:
        if not self.meta._free_since:
            return True
        if not other.meta._free_since:
            return False
        return self.meta._free_since <= other.meta._free_since

    def __gt__(self, other: Self) -> bool:
        if not self.meta._free_since:
            return False
        if not other.meta._free_since:
            return True
        return self.meta._free_since > other.meta._free_since

    @classmethod
    @my_backoff_exc()
    async def create(
        cls,
        host: str,
        username: str,
        client_keys: Optional[Sequence[PurePath]],
        logger: Optional[logging.Logger] = None,
        connect_timeout: Optional[int] = None,
        data_dir: Optional[PurePath] = None,
        engines_dir: Optional[PurePath] = None,
        tasks_dir: Optional[PurePath] = None,
        jump_host: Optional[str] = None,
        jump_username: Optional[str] = None,
    ) -> "PRemoteMachine":
        logger_name = f"{cls.__name__}:{username}@{host}"
        if logger:
            log = logger.getChild(logger_name)
        else:
            log = logging.getLogger(logger_name)
        # If our logging level is not set then asyncssh is too noisy
        asyncssh.logging.set_log_level(logging.WARNING if log.level == 0 else log.level)
        # asyncssh.logging.set_log_level("DEBUG")
        # asyncssh.logging.set_debug_level(2)

        # connection
        conn_opts = SSHClientConnectionOptions(
            options=DEFAULT_CONN_OPTS,
            host=host,
            username=username,
            tunnel=jump_host and jump_username and f"{jump_username}@{jump_host}",
            client_keys=client_keys or (),
            ignore_encrypted=True,
            connect_timeout=connect_timeout,
        )

        log.debug("Open connection")
        conn = await asyncssh.connection.connect(
            options=conn_opts,
            host=conn_opts.host,
            tunnel=conn_opts.tunnel,
        )

        # guess platform
        sess_lim = Semaphore(MAX_SESSIONS)

        async def with_limit(conn: SSHClientConnection, fn: SSHCheck):
            async with sess_lim:
                return await fn(conn)

        adapter = None
        platforms: Sequence[str] = []
        checks: Sequence[bool] = [
            await aall(amap(lambda y: with_limit(conn, y), x.checks)) for x in ADAPTERS
        ]

        for candidate, check in zip(ADAPTERS, checks):
            if check:
                platforms.append(candidate.platform)
            if check and not adapter:
                adapter = candidate

        if not adapter:
            raise PlatformGuessFailed()

        log.debug(f"Detected platform: {adapter.platform}")

        Path = adapter.path
        if not isinstance(data_dir, Path):
            data_dir = Path(str(data_dir)) if data_dir else Path("./data")
        if not isinstance(engines_dir, Path):
            engines_dir = (
                Path(str(engines_dir)) if engines_dir else data_dir / "engines"
            )
        if not isinstance(tasks_dir, Path):
            tasks_dir = Path(str(tasks_dir)) if tasks_dir else data_dir / "tasks"

        return cls(
            conn=conn,
            conn_opts=conn_opts,
            meta=RemoteMachineMetadata(),
            adapter=adapter,
            platforms=platforms,
            log=log,
            data_dir=data_dir,
            engines_dir=engines_dir,
            tasks_dir=tasks_dir,
        )

    @classmethod
    @asynccontextmanager
    async def create_ctx(
        cls, *args, **kwargs
    ) -> AsyncGenerator["PRemoteMachine", None]:
        """
        Create async context.
        :raises asyncssh.Error: An SSH error has occurred.
        """
        machine = await cls.create(*args, **kwargs)
        yield machine
        await machine.close()

    @property
    def hostname(self) -> str:
        return self.conn_opts.host

    async def close(self) -> None:
        "Close connections and free resources"
        self.cancellation_event.set()
        for task in self.jobs:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.conn._transport:
            self.log.debug("Close connection")
            self.conn.close()
            await self.conn.wait_closed()

    @asynccontextmanager
    async def sftp(self, **kwargs) -> AsyncGenerator[SFTPClient, None]:
        """
        Open SFTP connection
        :raises asyncssh.SFTPError: An SFTP error has occurred.
        """
        conn = await self.get_conn()
        async with conn.start_sftp_client(**kwargs) as sftp:
            yield sftp

    @my_backoff_exc()
    async def renew_conn(self) -> SSHClientConnection:
        """
        Reopen SSH connection
        """
        conn = await asyncssh.connection.connect(
            options=self.conn_opts,
            host=self.conn_opts.host,
            tunnel=self.conn_opts.tunnel,
        )
        # MUTATE OBJECT!
        object.__setattr__(self, "conn", conn)
        return conn

    async def get_conn(self) -> SSHClientConnection:
        """
        Return current SSH connection. Reopen if needed.
        """
        async with self.sessions_limit:
            if self.conn._transport and not self.conn._transport.is_closing():
                return self.conn
            self.log.debug("Connection is closed - reopening")
            return await self.renew_conn()

    @property
    def path(self) -> Type[PurePath]:
        return self.adapter.path

    def quote(self, s: str) -> str:
        "Platform-specific shell quoting"
        return self.adapter.quote(s)

    @my_backoff_exc()
    async def run(
        self, *args, cwd: Optional[str] = None, **kwargs
    ) -> SSHCompletedProcess:
        "Run process and wait for exit"
        conn = await self.get_conn()
        return await self.adapter.run(
            conn, self.adapter.quote, *args, cwd=cwd, **kwargs
        )

    async def run_bg(
        self, command: str, *args, cwd: Optional[str] = None, **kwargs
    ) -> SSHClientProcess:
        "Run process in background"
        conn = await self.get_conn()
        return await self.adapter.run_bg(
            conn, self.adapter.quote, command, *args, cwd=cwd, **kwargs
        )

    @my_backoff_exc()
    async def get_cpu_cores(self) -> int:
        "Get number of CPU cores"
        return await self.adapter.get_cpu_cores(self.run)

    async def list_processes(self) -> AsyncGenerator[PProcessInfo, None]:
        "Returns information about all running processes"
        conn = await self.get_conn()
        async for x in self.adapter.list_processes(conn, None):
            yield x

    async def pgrep(
        self, pattern: Union[str, Pattern], full: bool = True
    ) -> AsyncGenerator[PProcessInfo, None]:
        """
        Returns information about running processes, that name matches a pattern.
        If `full`, check match against name or full cmd.
        """
        conn = await self.get_conn()
        async for x in self.adapter.pgrep(conn, self.adapter.quote, pattern, full):
            yield x

    async def setup_node(self, engines: PEngineRepository):
        """
        Setup node for target engines.
        :raises NotImplemented: Not supported on platform.
        """
        self.log.info(f"CPUs count: {await self.get_cpu_cores()}")
        conn = await self.get_conn()
        retry = my_backoff_exc(exception=AllSSHRetryExc)
        await retry(self.adapter.setup_node)(
            conn=conn,
            run=self.run,
            quote=self.quote,
            engines=engines,
            engines_dir=self.engines_dir,
            log=self.log,
        )

    async def occupancy_check(self, engine: PEngine) -> bool:
        """
        Check node occupancy by task for target engine
        """
        if engine.check_pname:
            try:
                if [x async for x in self.pgrep(engine.check_pname)]:
                    return True
            except SSHRetryExc as e:
                self.log.info(f"Node {self.hostname} failed pgrep: {e}")
                await self.renew_conn()
        if engine.check_cmd:
            try:
                r = await self.run(engine.check_cmd)
                if r.returncode == engine.check_cmd_code:
                    return True
            except SSHRetryExc as e:
                self.log.info(f"Node {self.hostname} failed command: {e}")
                await self.renew_conn()
        return False

    async def start_occupancy_check(self, engine: PEngine) -> None:
        """
        Start occupancy checker for engine.
        """
        # remove old tasks
        self.jobs.difference_update([t for t in self.jobs if t.done()])

        async def occupancy_checker():
            while not self.cancellation_event.is_set() and self.meta.busy:
                try:
                    busy = await asyncio.wait_for(
                        self.occupancy_check(engine), timeout=engine.sleep_interval
                    )
                    if not busy:
                        self.meta.busy = False
                except asyncio.TimeoutError:
                    t = "Engine {} busy check timeouted on {}"
                    self.log.warning(t.format(engine.name, self.hostname))
                except Exception as err:
                    self.log.warning(err)
                await asyncio.sleep(engine.sleep_interval)

        self.jobs.add(asyncio.create_task(occupancy_checker()))
