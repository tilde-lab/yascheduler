#!/usr/bin/env python3

from typing import Optional

from asyncssh.connection import SSHClientConnection
from asyncssh.process import SSHCompletedProcess
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
        command = "cd {}; {}".format(quote(cwd), command)
    return await conn.run(command, *args, **kwargs)
