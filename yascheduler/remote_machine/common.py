#!/usr/bin/env python3

from subprocess import DEVNULL
from typing import Optional

from asyncssh.connection import SSHClientConnection
from asyncssh.process import SSHClientProcess, SSHCompletedProcess
from attrs import define

from .protocol import QuoteCallable


@define(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    command: str


async def run(
    conn: SSHClientConnection,
    quote: QuoteCallable,
    command: str,
    *args,
    cwd: Optional[str] = None,
    **kwargs,
) -> SSHCompletedProcess:
    """
    Run process and wait for exit
    :raises asyncssh.Error: An SSH error has occurred.
    """
    if cwd:
        command = f"cd {quote(cwd)}; {command}"
    return await conn.run(command, *args, **kwargs)


async def run_bg(
    conn: SSHClientConnection,
    quote: QuoteCallable,
    command: str,
    *args,
    cwd: Optional[str] = None,
    **kwargs,
) -> SSHClientProcess:
    """
    Create background process.
    :raises asyncssh.ChannelOpenError: An SSH error has occurred.
    """
    if cwd:
        command = f"cd {quote(cwd)}; {command}"
    return await conn.create_process(
        command, *args, **kwargs, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL
    )
