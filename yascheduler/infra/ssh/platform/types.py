"""Protocol definitions for process info, SSH checks, and adapters."""
# region MODULE_CONTRACT
# PURPOSE: Type aliases, exception tuples, and callable protocols for SSH remote machine operations.
# SCOPE:
# - Exception tuples: SFTPRetryExc, SSHRetryExc, AllSSHRetryExc
# - Data class: ProcessInfo
# - Protocols: RunCallable, RunBgCallable, PgrepCallable, SetupNodeCallable
# - Callable aliases: SSHCheck, QuoteCallable, GetCPUCoresCallable, OuterRunCallable, ListProcessesCallable
# KEYWORDS: type aliases, protocol, ssh, sftp, exceptions, callables
# DEPENDENCIES: USES API: asyncssh.
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import AsyncGenerator, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

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
from asyncssh.process import SSHCompletedProcess
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

if TYPE_CHECKING:
    from pathlib import PurePath
    from re import Pattern

    from asyncssh.process import SSHClientProcess

    from yascheduler.domain import EngineRepository

__all__ = [
    "AllSSHRetryExc",
    "GetCPUCoresCallable",
    "ListProcessesCallable",
    "PgrepCallable",
    "ProcessInfo",
    "QuoteCallable",
    "RunBgCallable",
    "RunCallable",
    "SFTPRetryExc",
    "SSHCheck",
    "SSHRetryExc",
    "SetupNodeCallable",
]

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


# region CLASS_ProcessInfo
# PURPOSE: Carry a single remote process's identity (pid, name, command line) out of pgrep/list_processes so occupancy checks and process listings branch on structured fields instead of re-parsing ps/Get-CimInstance output.
@dataclass(frozen=True)
class ProcessInfo:
    """Remote process information — PID, name, and command line."""

    pid: int
    name: str
    command: str


# endregion CLASS_ProcessInfo


SSHCheck = Callable[[SSHClientConnection], Coroutine[Any, Any, bool]]
QuoteCallable = Callable[[str], str]


# region CLASS_RunCallable
# PURPOSE: Type every platform-specific synchronous-SSH command-execution callable against a single structural contract so adapter wiring stays platform-agnostic and the type-checker rejects run callables that drop the quote parameter.
class RunCallable(Protocol):
    """Callable protocol for synchronous SSH command execution."""

    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        command: str,
        *args: object,
        cwd: str | None = None,
        **kwargs: dict[str, Any],
    ) -> Coroutine[Any, Any, SSHCompletedProcess]:
        """Call."""


# endregion CLASS_RunCallable


# region CLASS_RunBgCallable
# PURPOSE: Type every platform-specific background-process spawn callable against a single structural contract so the session's run_bg can delegate without per-platform branching.
class RunBgCallable(Protocol):
    """Callable protocol for background SSH command execution."""

    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        command: str,
        *args: object,
        cwd: str | None = None,
        **kwargs: object,
    ) -> Coroutine[Any, Any, SSHClientProcess[Any]]:
        """Call."""


# endregion CLASS_RunBgCallable


# region CLASS_OuterRunCallable
# PURPOSE: Type the closure that make_run_fn produces so adapter methods that need a run callable (get_cpu_cores, setup_node) accept a single typed callable instead of (conn, quote) plus a free function.
OuterRunCallable = Callable[..., Coroutine[Any, Any, SSHCompletedProcess]]

# endregion CLASS_OuterRunCallable


GetCPUCoresCallable = Callable[[OuterRunCallable], Coroutine[Any, Any, int]]


# region ALIAS_ListProcessesCallable
# PURPOSE: Type every platform-specific process-listing callable so SSHMachineSession.list_processes delegates without per-platform branching.
ListProcessesCallable = Callable[
    [SSHClientConnection, str | None],
    AsyncGenerator[ProcessInfo, None],
]

# endregion ALIAS_ListProcessesCallable


# region CLASS_PgrepCallable
# PURPOSE: Type every platform-specific pattern-matching process callable so SSHMachineSession.pgrep delegates without per-platform branching.
class PgrepCallable(Protocol):
    """Callable protocol for pattern-matching remote processes."""

    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        quote: QuoteCallable,
        pattern: str | Pattern[str],
        *,
        full: bool = True,
    ) -> AsyncGenerator[ProcessInfo, None]:
        """Call."""


# endregion CLASS_PgrepCallable


# region CLASS_SetupNodeCallable
# PURPOSE: Type every platform-specific node-setup callable so SSHMachineSession.setup_node delegates without per-platform branching.
class SetupNodeCallable(Protocol):
    """Callable protocol for node setup operations."""

    @abstractmethod
    def __call__(
        self,
        conn: SSHClientConnection,
        run: OuterRunCallable,
        quote: QuoteCallable,
        engines: EngineRepository,
        engines_dir: PurePath,
    ) -> Coroutine[Any, Any, None]:
        """Call."""


# endregion CLASS_SetupNodeCallable
