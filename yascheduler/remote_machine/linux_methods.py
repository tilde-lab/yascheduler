#!/usr/bin/env python3

import logging
import re
from pathlib import PurePath
from typing import AsyncGenerator, Optional, Pattern, Sequence, Union

from asyncssh.connection import SSHClientConnection
from asyncssh.sftp import SFTPClient

from ..config import LocalArchiveDeploy, LocalFilesDeploy, RemoteArchiveDeploy
from .common import ProcessInfo
from .protocol import OuterRunCallable, PEngineRepository, PProcessInfo, QuoteCallable


async def linux_get_cpu_cores(run: OuterRunCallable) -> int:
    """
    Get number of CPU cores
    :raises asyncssh.Error: An SSH error has occurred.
    """
    r = await run("getconf NPROCESSORS_ONLN 2> /dev/null || getconf _NPROCESSORS_ONLN")
    try:
        return int(r.stdout and r.stdout.strip() or "1")
    except ValueError:
        return 1


async def linux_list_processes(
    conn: SSHClientConnection, query: Optional[str] = None
) -> AsyncGenerator[PProcessInfo, None]:
    """
    Returns information about all running processes
    :raises asyncssh.Error: An SSH error has occurred.
    """
    columns = ["pid", "comm", "args"]
    columns_part = ",".join([f"{x}:255" for x in columns])
    if query:
        ps_cmd = " ".join([query, "| xargs --no-run-if-empty ps -o", columns_part])
    else:
        ps_cmd = f"ps -eo {columns_part}"
    async with conn.create_process(ps_cmd) as proc:
        await proc.stdout.readline()  # skip headers
        async for line in proc.stdout:
            parts = list(
                map(lambda x: x.strip(), filter(None, str(line).split(" " * 10)))
            )
            # skip broken
            if len(parts) < 3:
                continue
            # skip parent of self
            if parts[2].startswith(f"bash -c {ps_cmd}"):
                continue
            yield ProcessInfo(int(parts[0]), *parts[1:3])


async def linux_pgrep(
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
    pgrep_query = " ".join(
        filter(None, ["pgrep", "-f" if full else None, quote(str_pattern)])
    )
    async for x in linux_list_processes(conn, query=pgrep_query):
        yield x


async def deploy_local_files(
    sftp: SFTPClient,
    engine_dir: PurePath,
    files: Sequence[PurePath],
    log: Optional[logging.Logger] = None,
):
    "Uploading binary from local; requires broadband connection"
    lpaths = list(map(str, files))
    if log:
        log.debug(f"Uploading files ({', '.join(lpaths)}) to {engine_dir}")
    await sftp.put(lpaths, engine_dir, preserve=True)


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
        log.debug(f"Uploading {archive.name} to {str(rpath)}...")
    await sftp.put([str(archive)], engine_dir)
    if log:
        log.debug(f"Unarchiving {archive.name}...")
    await run(f"tar xfv {quote(str(archive.name))}", cwd=str(engine_dir), check=True)
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
    name = "archive.tar.gz"
    rpath = engine_dir / name
    if log:
        log.debug(f"Downloading {url} to {str(rpath)}...")
    await run(f"wget {quote(url)} -O {quote(name)}", cwd=str(engine_dir), check=True)
    if log:
        log.debug(f"Unarchiving {name}...")
    await run(f"tar xfv {quote(str(name))}", cwd=str(engine_dir), check=True)
    await sftp.remove(rpath)


async def linux_deploy_engines(
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
        engine_dir = engines_dir / engine.name
        await sftp.makedirs(engine_dir, exist_ok=True)
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
        if log:
            log.info(f"Setup of {engine.name} engine is done...")


async def log_mpi_version(run: OuterRunCallable, log: Optional[logging.Logger] = None):
    r = await run("mpirun --allow-run-as-root -V", check=True)
    if not r.returncode and log:
        log.debug(str(r.stdout or "").split("\n")[0])


async def linux_setup_node(
    conn: SSHClientConnection,
    run: OuterRunCallable,
    quote: QuoteCallable,
    engines: PEngineRepository,
    engines_dir: PurePath,
    log: Optional[logging.Logger] = None,
):
    "Setup generic linux node"
    async with conn.start_sftp_client() as sftp:
        await linux_deploy_engines(run, quote, sftp, engines, engines_dir, log)


async def linux_setup_deb_node(
    conn: SSHClientConnection,
    run: OuterRunCallable,
    quote: QuoteCallable,
    engines: PEngineRepository,
    engines_dir: PurePath,
    log: Optional[logging.Logger] = None,
):
    "Setup debian-like node"
    is_root = conn._username == "root"
    sudo_prefix = "" if is_root else "sudo "
    apt_cmd = f"{sudo_prefix}apt-get -o DPkg::Lock::Timeout=600 -y"
    pkgs = engines.get_platform_packages()

    if log:
        log.debug("Upgrade packages...")
    await run(f"{apt_cmd} update", check=True)
    await run(f"{apt_cmd} upgrade", check=True)
    if pkgs:
        if log:
            log.debug("Install packages: {} ...".format(" ".join(pkgs)))
        await run(f"{apt_cmd} install {' '.join(pkgs)}", check=True)
    if [x for x in pkgs if "mpi" in x]:
        await log_mpi_version(run, log)

    async with conn.start_sftp_client() as sftp:
        await linux_deploy_engines(run, quote, sftp, engines, engines_dir, log)
