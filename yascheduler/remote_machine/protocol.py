#!/usr/bin/env python3

import asyncio
import logging
from abc import abstractmethod
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import PurePath
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Coroutine,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
    ValuesView,
)

from asyncssh.connection import SSHClientConnection
from asyncssh.misc import (
    ChannelListenError,
    ChannelOpenError,
    CompressionError,
    ConnectionLost,
    KeyExchangeFailed,
    MACError,
    ProtocolError,
    ServiceNotAvailable,
)
from asyncssh.process import SSHClientProcess, SSHCompletedProcess
from asyncssh.sftp import (
    SFTPBadMessage,
    SFTPByteRangeLockConflict,
    SFTPByteRangeLockRefused,
    SFTPClient,
    SFTPConnectionLost,
    SFTPDeletePending,
    SFTPEOFError,
    SFTPFailure,
    SFTPInvalidHandle,
    SFTPLockConflict,
    SFTPNoConnection,
    SFTPNoMatchingByteRangeLock,
)
from typing_extensions import Protocol, Self, TypedDict, Unpack

SFTPRetryExc = (
    asyncio.TimeoutError,
    SFTPEOFError,
    SFTPFailure,
    SFTPBadMessage,
    SFTPNoConnection,
    SFTPConnectionLost,
    SFTPInvalidHandle,
    SFTPLockConflict,
    SFTPByteRangeLockConflict,
    SFTPByteRangeLockRefused,
    SFTPDeletePending,
    SFTPNoMatchingByteRangeLock,
)
SSHRetryExc = (
    OSError,
    asyncio.TimeoutError,
    CompressionError,
    ConnectionLost,
    KeyExchangeFailed,
    MACError,
    ProtocolError,
    ServiceNotAvailable,
    ChannelOpenError,
    ChannelListenError,
)
AllSSHRetryExc = SSHRetryExc + SFTPRetryExc


class PProcessInfo(Protocol):
    pid: int
    name: str
    command: str


class PEngine(Protocol):
    name: str
    deployable: Tuple
    platforms: Tuple[str, ...]
    check_pname: Optional[str]
    check_cmd: Optional[str]
    check_cmd_code: int
    sleep_interval: int


class PEngineRepository(Protocol):
    @abstractmethod
    def get_platform_packages(self) -> Sequence[str]:
        raise NotImplementedError

    @abstractmethod
    def filter_platforms(self, platforms: Sequence[str]) -> "PEngineRepository":
        raise NotImplementedError

    @abstractmethod
    def values(self) -> ValuesView[PEngine]:
        raise NotImplementedError


class PNode(Protocol):
    ip: str
    username: str


SSHCheck = Callable[[SSHClientConnection], Coroutine[Any, Any, bool]]
QuoteCallable = Callable[[str], str]


class RunCallable(Protocol):
    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        command: str,
        *args,
        cwd: Optional[str] = None,
        **kwargs,
    ) -> Coroutine[Any, Any, SSHCompletedProcess]:
        pass


class RunBgCallable(Protocol):
    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        command: str,
        *args,
        cwd: Optional[str] = None,
        **kwargs,
    ) -> Coroutine[Any, Any, SSHClientProcess]:
        pass


class OuterRunCallable(Protocol):
    @abstractmethod
    def __call__(
        self, *args, cwd: Optional[str] = None, **kwargs
    ) -> Coroutine[Any, Any, SSHCompletedProcess]:
        pass


GetCPUCoresCallable = Callable[[OuterRunCallable], Coroutine[Any, Any, int]]


class ListProcessesCallable(Protocol):
    @abstractmethod
    def __call__(
        self, conn: SSHClientConnection, query: Optional[str] = None
    ) -> AsyncGenerator[PProcessInfo, None]:
        pass


class PgrepCallable(Protocol):
    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        pattern: Union[str, Pattern[str]],
        full=True,
    ) -> AsyncGenerator[PProcessInfo, None]:
        pass


class SetupNodeCallable(Protocol):
    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        run: OuterRunCallable,
        quote: QuoteCallable,
        engines: PEngineRepository,
        engines_dir: PurePath,
        log: Optional[logging.Logger] = None,
    ) -> Coroutine[Any, Any, None]:
        pass


class PRemoteMachineAdapter(Protocol):
    platform: str
    path: Type[PurePath]
    quote: QuoteCallable
    run: RunCallable
    run_bg: RunBgCallable
    checks: Sequence[SSHCheck]
    get_cpu_cores: GetCPUCoresCallable
    list_processes: ListProcessesCallable
    pgrep: PgrepCallable
    setup_node: SetupNodeCallable


class PRemoteMachineMetadata(Protocol):
    busy: Optional[bool]

    @abstractmethod
    def is_free_longer_than(self, delta: timedelta) -> bool:
        raise NotImplementedError


class PRemoteMachineCreateKwargsCommon(TypedDict):
    client_keys: Optional[Union[Sequence[bytes], Sequence[str]]]
    logger: Optional[logging.Logger]
    connect_timeout: Optional[float]
    data_dir: Optional[PurePath]
    engines_dir: Optional[PurePath]
    tasks_dir: Optional[PurePath]
    jump_host: Optional[str]
    jump_username: Optional[str]


class PRemoteMachineCreateKwargs(PRemoteMachineCreateKwargsCommon):
    host: str
    username: str


class PRemoteMachine(Protocol):
    "Remote SSH machine"

    meta: PRemoteMachineMetadata
    path: Type[PurePath]

    # supported platforms (like debian-10, debian, debian-like and linux)
    platforms: Sequence[str]

    data_dir: PurePath
    engines_dir: PurePath
    tasks_dir: PurePath

    jobs: Set[asyncio.Task]

    @abstractmethod
    def __le__(self, other: Self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def __gt__(self, other: Self) -> bool:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    async def create(
        cls, **kwargs: Unpack[PRemoteMachineCreateKwargs]
    ) -> "PRemoteMachine":
        """
        Async init of remote machine.
        If adapter is not set, then platform will be guessed.
        :raises PlatformGuessFailed: Not supported platform.
        :raises asyncssh.Error: An SSH error has occurred.
        """
        raise NotImplementedError

    @classmethod
    @abstractmethod
    @asynccontextmanager
    def create_ctx(cls, *args, **kwargs) -> AsyncGenerator["PRemoteMachine", None]:
        """
        Create async context.
        :raises asyncssh.Error: An SSH error has occurred.
        """
        raise NotImplementedError

    async def close(self) -> None:
        "Close connections and free resources"
        raise NotImplementedError

    @property
    @abstractmethod
    def hostname(self) -> str:
        raise NotImplementedError

    @abstractmethod
    @asynccontextmanager
    def sftp(self, **kwargs) -> AsyncGenerator[SFTPClient, None]:
        """
        Open SFTP connection
        :raises asyncssh.SFTPError: An SFTP error has occurred.
        """

    @abstractmethod
    def quote(self, s: str) -> str:
        "Platform-specific shell quoting"
        raise NotImplementedError

    @abstractmethod
    async def run(
        self, *args, cwd: Optional[str] = None, **kwargs
    ) -> SSHCompletedProcess:
        "Run process and wait for exit"
        raise NotImplementedError

    @abstractmethod
    async def run_bg(
        self, command: str, *args, cwd: Optional[str] = None, **kwargs
    ) -> SSHCompletedProcess:
        "Run process in background"

    @abstractmethod
    async def get_cpu_cores(self) -> int:
        "Get number of CPU cores"
        raise NotImplementedError

    @abstractmethod
    def list_processes(self) -> AsyncGenerator[PProcessInfo, None]:
        "Returns information about all running processes"
        raise NotImplementedError

    @abstractmethod
    def pgrep(
        self, pattern: Union[str, Pattern], full: bool = True
    ) -> AsyncGenerator[PProcessInfo, None]:
        """
        Returns information about running processes, that name matches a pattern.
        If `full`, check match against name or full cmd.
        """
        raise NotImplementedError

    @abstractmethod
    async def setup_node(self, engines: PEngineRepository):
        """
        Setup node for target engines.
        :raises NotImplemented: Not supported on platform.
        """
        raise NotImplementedError("Not implemented for this platform")

    @abstractmethod
    async def occupancy_check(self, engine: PEngine) -> bool:
        """
        Check node occupancy by task for target engine
        """
        raise NotImplementedError

    @abstractmethod
    async def start_occupancy_check(self, engine: PEngine) -> None:
        """
        Start occupancy checker for engine.
        """
        raise NotImplementedError
