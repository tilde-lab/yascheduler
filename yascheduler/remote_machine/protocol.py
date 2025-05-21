#!/usr/bin/env python3

import asyncio
import logging
from abc import abstractmethod
from collections.abc import AsyncGenerator, Callable, Coroutine, Sequence, ValuesView
from pathlib import PurePath
from re import Pattern
from typing import Any, Optional, Protocol, Union

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
    SFTPConnectionLost,
    SFTPDeletePending,
    SFTPEOFError,
    SFTPFailure,
    SFTPInvalidHandle,
    SFTPLockConflict,
    SFTPNoConnection,
    SFTPNoMatchingByteRangeLock,
)

from ..config.engine import LocalArchiveDeploy, LocalFilesDeploy, RemoteArchiveDeploy

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
    deployable: tuple[
        Union[LocalFilesDeploy, LocalArchiveDeploy, RemoteArchiveDeploy], ...
    ]
    platforms: tuple[str, ...]
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
        *args: object,
        cwd: Optional[str] = None,
        **kwargs: dict[str, Any],
    ) -> Coroutine[Any, Any, SSHCompletedProcess]:
        pass


class RunBgCallable(Protocol):
    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        command: str,
        *args: object,
        cwd: Optional[str] = None,
        **kwargs: object,
    ) -> Coroutine[Any, Any, SSHClientProcess[Any]]:
        pass


class OuterRunCallable(Protocol):
    @abstractmethod
    def __call__(
        self, *args: object, cwd: Optional[str] = None, **kwargs: Any
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
        full: bool = True,
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
