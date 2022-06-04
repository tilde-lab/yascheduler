#!/usr/bin/env python3

import asyncio
import json
import logging
import re
from pathlib import PurePath, PureWindowsPath
from typing import AsyncGenerator, Optional, Pattern, Sequence, Union

from asyncssh.connection import SSHClientConnection
from asyncssh.sftp import SFTPClient

from .common import ProcessInfo
from .protocol import OuterRunCallable, PEngineRepository, PProcessInfo, QuoteCallable
from ..config import LocalArchiveDeploy, LocalFilesDeploy, RemoteArchiveDeploy


class MyPureWindowsPath(PureWindowsPath):
    @classmethod
    def _parse_args(cls, args):
        drv, root, parts = PureWindowsPath._parse_args(args)
        # prevent leading slash like \C:\Users\user
        if not drv and root == "\\" and len(parts) > 2 and parts[0] == "\\":
            drv = parts[1]
            parts = parts[1:]
        # prevent eating first part when parsing PurePath instance
        if len(parts) > 1 and drv == parts[0] and not root:
            root = "\\"

        return drv, root, parts


def windows_quote(s: str) -> str:
    return "'{}'".format(str(s).replace("'", "''"))


async def windows_get_cpu_cores(run: OuterRunCallable) -> int:
    """
    Get number of CPU cores
    :raises asyncssh.Error: An SSH error has occurred.
    """
    res = await run("[environment]::ProcessorCount")
    try:
        return int(res.stdout and res.stdout.strip() or "1")
    except:
        return 1


async def windows_list_processes(
    conn: SSHClientConnection, query: Optional[str] = None
) -> AsyncGenerator[PProcessInfo, None]:
    """
    Returns information about all running processes
    :raises asyncssh.Error: An SSH error has occurred.
    """
    where_pipe_cmd = "| ?{{ {cmd} }}".format(cmd=query) if query else ""
    get_process_cmd = f"Get-CimInstance Win32_Process {where_pipe_cmd}"
    inline_obj = "@{'pid' = $_.ProcessId; 'name' = $_.Name; 'command' = $_.CommandLine}"
    for_each_cmd = f"%{{ {inline_obj} | ConvertTo-Json -compress }}"
    ps_cmd = f"{get_process_cmd} | {for_each_cmd}"
    async with conn.create_process(ps_cmd) as proc:
        async for line in proc.stdout:
            try:
                data = json.loads(line)
                if not data["command"]:
                    data["command"] = data["name"]
                assert isinstance(data["pid"], int)
                assert isinstance(data["name"], str)
                assert isinstance(data["command"], str)
                # skip self
                if (
                    data["name"] == "powershell.exe"
                    and "Get-CimInstance Win32_Process" in data["command"]
                ):
                    continue
                yield ProcessInfo(**data)
            except:
                continue


async def windows_pgrep(
    conn: SSHClientConnection,
    quote: QuoteCallable,
    pattern: Union[str, Pattern[str]],
    full=True,
) -> AsyncGenerator[PProcessInfo, None]:
    """
    Returns information about running processes, that name matches a pattern.
    If `full`, check match against name or full cmd.
    :raises asyncssh.Error: An SSH error has occurred.
    """
    str_pattern = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    match_tail = ["-match", quote(str_pattern)]
    name_expr = ["$_.Name", *match_tail]
    cmd_expr = ["$_.CommandLine", *match_tail]
    if full:
        where_expr = " ".join([*name_expr, "-or", *cmd_expr])
    else:
        where_expr = " ".join(name_expr)
    async for x in windows_list_processes(conn, query=where_expr):
        yield x


async def deploy_local_files(
    sftp: SFTPClient,
    engine_dir: PurePath,
    files: Sequence[PurePath],
    log: Optional[logging.Logger] = None,
):
    "Uploading binary from local; requires broadband connection"

    async def upload(src: PurePath, dst: PurePath):
        if log:
            log.info(f"Uploading file {str(src)} to {str(dst)}")
        await sftp.put([str(src)], str(dst))

    await asyncio.gather(*map(lambda x: upload(x, engine_dir / x.name), files))


async def deploy_local_archive(
    run: OuterRunCallable,
    quote: QuoteCallable,
    sftp: SFTPClient,
    engine_dir: PurePath,
    archive: PurePath,
    log: Optional[logging.Logger] = None,
):
    """
    Upload local archive.
    Binary may be gzipped, without subfolders, with an arbitrary archive name.
    """
    rpath = engine_dir / archive.name
    if log:
        log.info(f"Uploading {archive.name} to {str(rpath)}...")
    await sftp.put([str(archive)], engine_dir)
    if log:
        log.info(f"Unarchiving {archive.name}...")
    await run(
        f"""Expand-Archive {quote(str(rpath))} `
            -DestinationPath {quote(str(engine_dir))} `
            -Force"""
    )
    await sftp.remove(rpath)


async def deploy_remote_archive(
    run: OuterRunCallable,
    quote: QuoteCallable,
    sftp: SFTPClient,
    engine_dir: PurePath,
    url: str,
    log: Optional[logging.Logger] = None,
):
    """
    Downloading binary from a trusted non-public address.
    Binary may be gzipped, without subfolders, with an arbitrary archive name.
    """
    name = "archive.zip"
    rpath = engine_dir / name
    if log:
        log.info(f"Downloading {url} to {str(rpath)}...")
    await run(
        f"""Invoke-WebRequest -Uri {quote(url)} `
            -OutFile {quote(str(rpath))} -Force"""
    )
    if log:
        log.info(f"Unarchiving {name}...")
    await run(
        f"""Expand-Archive {quote(str(rpath))} `
            -DestinationPath {quote(str(engine_dir))} `
            -Force"""
    )
    await sftp.remove(rpath)


async def windows_deploy_engines(
    run: OuterRunCallable,
    quote: QuoteCallable,
    sftp: SFTPClient,
    engines: PEngineRepository,
    engines_dir: PurePath,
    log: Optional[logging.Logger] = None,
) -> None:
    """
    Setup node for target engines.
    """
    for engine in engines.values():
        if log:
            log.info(f"Setup {engine.name} engine...")
        engine_dir = PureWindowsPath(
            (await sftp.realpath(engines_dir / engine.name))[1:]
        )
        # sftp.makedirs is broken for PureWindowsPath
        await sftp.makedirs(PurePath(engine_dir), exist_ok=True)
        for deployment in engine.deployable:
            if isinstance(deployment, LocalFilesDeploy):
                await deploy_local_files(sftp, engine_dir, deployment.files, log)

            if isinstance(deployment, LocalArchiveDeploy):
                await deploy_local_archive(
                    run, quote, sftp, engine_dir, deployment.file
                )

            if isinstance(deployment, RemoteArchiveDeploy):
                await deploy_remote_archive(
                    run, quote, sftp, engine_dir, deployment.url
                )


async def windows_setup_node(
    conn: SSHClientConnection,
    run: OuterRunCallable,
    quote: QuoteCallable,
    engines: PEngineRepository,
    engines_dir: PurePath,
    log: Optional[logging.Logger] = None,
):
    "Setup generic linux node"
    async with conn.start_sftp_client() as sftp:
        await windows_deploy_engines(run, quote, sftp, engines, engines_dir, log)
