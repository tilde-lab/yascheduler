"""Linux-specific remote commands: engine deployment, process listing, CPU detection, package installation."""
# region MODULE_CONTRACT
# PURPOSE: Linux-specific remote machine operations — setup_node, get_cpu_cores, process listing, pgrep, engine deployment helpers.
# SCOPE: Linux setup_node, get_cpu_cores, list_processes, pgrep, deploy helpers, linux_deploy_engines, log_mpi_version, linux_setup_node, linux_setup_deb_node.
# DEPENDENCIES: USES API: asyncssh (SSHClientConnection, SFTPClient)
# KEYWORDS: linux, ssh, remote, deploy, engines, cpu, processes, pgrep
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import re
from re import Pattern
from typing import TYPE_CHECKING

from asyncssh.process import ProcessError

from yascheduler.domain import (
    EngineRepository,
    LocalArchiveDeploy,
    LocalFilesDeploy,
    RemoteArchiveDeploy,
)

from .types import OuterRunCallable, ProcessInfo, QuoteCallable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
    from pathlib import PurePath

    from asyncssh.connection import SSHClientConnection
    from asyncssh.sftp import SFTPClient

__all__ = [
    "deploy_local_archive",
    "deploy_local_files",
    "deploy_remote_archive",
    "linux_deploy_engines",
    "linux_get_cpu_cores",
    "linux_list_processes",
    "linux_pgrep",
    "linux_setup_deb_node",
    "linux_setup_node",
    "log_mpi_version",
]

logger = logging.getLogger(__name__)

# apt repository problem = exit 100
_APT_TRANSIENT_EXIT = 100


# region FUNC_linux_get_cpu_cores
# PURPOSE: Get number of CPU cores on remote Linux via getconf.
async def linux_get_cpu_cores(run: OuterRunCallable) -> int:
    """Get number of CPU cores.

    :raises asyncssh.Error: An SSH error has occurred.
    """
    r = await run("getconf NPROCESSORS_ONLN 2> /dev/null || getconf _NPROCESSORS_ONLN")
    try:
        return int((r.stdout and r.stdout.strip()) or "1")
    except ValueError:
        return 1


# endregion FUNC_linux_get_cpu_cores


# region FUNC_linux_list_processes
# PURPOSE: Yield running process info from remote Linux via ps.
async def linux_list_processes(
    conn: SSHClientConnection,
    query: str | None = None,
) -> AsyncGenerator[ProcessInfo, None]:
    """Return information about all running processes.

    :raises asyncssh.Error: An SSH error has occurred.
    """
    columns = ["pid", "comm", "args"]
    # NOTE: the GNU ":255" column-width suffix is rejected by BSD ps on
    # macOS ("keyword not found", exit 1, EMPTY stdout), so the occupancy
    # pgrep silently reported every Darwin node as idle while an engine
    # task was running -> premature consume + remote dir rmtree -> the
    # engine died with ENOENT on its next file open (silent exit 2 at
    # ~15 s). Plain keywords plus -ww are portable across procps and BSD
    # ps, and the parser no longer relies on padded column gaps.
    if query:
        ps_cmd = " ".join([query, "| xargs --no-run-if-empty ps -o", ",".join(columns), "-ww"])
    else:
        ps_cmd = " ".join(["ps -axo", ",".join(columns), "-ww"])
    logger.debug("LIST_PROCESSES", extra={"cmd": ps_cmd})
    async with conn.create_process(ps_cmd) as proc:
        await proc.stdout.readline()  # skip headers
        line_count = 0
        skipped_broken = 0
        skipped_self = 0
        async for line in proc.stdout:
            raw = str(line).strip()
            if not raw:
                continue
            line_count += 1
            parts = [x.strip() for x in raw.split(None, 2)]
            min_parts = 3
            if len(parts) < min_parts:
                skipped_broken += 1
                logger.debug(
                    "BROKEN_LINE",
                    extra={"line": line_count, "parts": len(parts), "raw": line},
                )
                continue
            # The wrapper process executing ps_cmd appears as
            # `<login-shell> -c <ps_cmd>` in ps output. Match the ps_cmd
            # substring instead of hardcoding `bash -c` — the login shell
            # may be sh/dash/zsh, in which case the self-process leaked
            # through, pgrep self-matched, and occupancy stuck BUSY.
            if ps_cmd in parts[2]:
                skipped_self += 1
                continue
            logger.debug(
                "YIELD",
                extra={"pid": parts[0], "comm": parts[1], "proc_args": parts[2][:80]},
            )
            yield ProcessInfo(int(parts[0]), *parts[1:3])
        logger.debug(
            "DONE",
            extra={
                "lines": line_count,
                "broken": skipped_broken,
                "self_skip": skipped_self,
            },
        )


# endregion FUNC_linux_list_processes


# region FUNC_linux_pgrep
# PURPOSE: Find processes matching a pattern via pgrep and yield their info.
async def linux_pgrep(
    conn: SSHClientConnection,
    quote: QuoteCallable,
    pattern: str | Pattern[str],
    *,
    full: bool = True,
) -> AsyncGenerator[ProcessInfo, None]:
    """Return information about running processes matching a name pattern.

    If `full`, check match against name or full cmd.
    :raises asyncssh.Error: An SSH error has occurred.
    """
    str_pattern = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    pgrep_query = " ".join(
        filter(None, ["pgrep", "-f" if full else None, quote(str_pattern)]),
    )
    async for x in linux_list_processes(conn, query=pgrep_query):
        yield x


# endregion FUNC_linux_pgrep


# region FUNC_deploy_local_files
# PURPOSE: Upload local binary files to remote via SFTP.
async def deploy_local_files(
    sftp: SFTPClient,
    engine_dir: PurePath,
    files: Sequence[PurePath],
) -> None:
    """Upload binary from local; requires broadband connection."""
    lpaths = list(map(str, files))
    logger.debug("UPLOAD", extra={"dir": engine_dir, "files": ", ".join(lpaths)})
    await sftp.put(lpaths, engine_dir, preserve=True)


# endregion FUNC_deploy_local_files


# region FUNC_deploy_local_archive
# PURPOSE: Upload local archive via SFTP and extract via tar on remote.
async def deploy_local_archive(
    run: OuterRunCallable,
    quote: QuoteCallable,
    sftp: SFTPClient,
    engine_dir: PurePath,
    archive: PurePath,
) -> None:
    """Upload local archive.

    Binary may be gzipped, without subfolders, with an arbitrary archive name.
    """
    rpath = engine_dir / archive.name
    logger.debug("UPLOAD", extra={"archive": archive.name, "path": str(rpath)})
    await sftp.put([str(archive)], engine_dir)
    logger.debug("EXTRACT", extra={"archive": archive.name})
    await run(f"tar xfv {quote(str(archive.name))}", cwd=str(engine_dir), check=True)
    await sftp.remove(rpath)


# endregion FUNC_deploy_local_archive


# region FUNC_deploy_remote_archive
# PURPOSE: Download remote archive via wget and extract via tar on remote.
async def deploy_remote_archive(
    run: OuterRunCallable,
    quote: QuoteCallable,
    sftp: SFTPClient,
    engine_dir: PurePath,
    url: str,
) -> None:
    """Download a binary from a trusted non-public address.

    Binary may be gzipped, without subfolders, with an arbitrary archive name.
    """
    name = "archive.tar.gz"
    rpath = engine_dir / name
    logger.debug("DOWNLOAD", extra={"url": url, "path": str(rpath)})
    await run(f"wget {quote(url)} -O {quote(name)}", cwd=str(engine_dir), check=True)
    logger.debug("EXTRACT", extra={"archive": name})
    await run(f"tar xfv {quote(str(name))}", cwd=str(engine_dir), check=True)
    await sftp.remove(rpath)


# endregion FUNC_deploy_remote_archive


# region FUNC_linux_deploy_engines
# PURPOSE: Deploy all engines for a node by iterating engine repository and dispatching deploy strategies.
async def linux_deploy_engines(
    run: OuterRunCallable,
    quote: QuoteCallable,
    sftp: SFTPClient,
    engines: EngineRepository,
    engines_dir: PurePath,
) -> None:
    """Set up node for target engines."""
    for engine in engines.values():
        logger.info("Setup %s engine...", engine.name)
        engine_dir = engines_dir / engine.name
        await sftp.makedirs(engine_dir, exist_ok=True)
        for deployment in engine.deployable:
            if isinstance(deployment, LocalFilesDeploy):
                await deploy_local_files(sftp, engine_dir, deployment.files)

            if isinstance(deployment, LocalArchiveDeploy):
                await deploy_local_archive(
                    run,
                    quote,
                    sftp,
                    engine_dir,
                    deployment.file,
                )

            if isinstance(deployment, RemoteArchiveDeploy):
                await deploy_remote_archive(
                    run,
                    quote,
                    sftp,
                    engine_dir,
                    deployment.url,
                )
        logger.info("Setup of %s engine is done...", engine.name)


# endregion FUNC_linux_deploy_engines


# region FUNC_log_mpi_version
# PURPOSE: Log MPI version info from remote via mpirun.
# INVARIANTS: Runs mpirun --allow-run-as-root -V with check=True; logs VERSION with the first stdout line on success; silently no-ops on non-zero exit — MPI may not be installed.
async def log_mpi_version(run: OuterRunCallable) -> None:
    """Log MPI version info from remote via mpirun."""
    r = await run("mpirun --allow-run-as-root -V", check=True)
    if not r.returncode:
        logger.debug("VERSION", extra={"version": str(r.stdout or "").split("\n")[0]})


# endregion FUNC_log_mpi_version


# region FUNC_linux_setup_node
# PURPOSE: Setup generic Linux node by deploying engines via SFTP.
# INVARIANTS: Opens a fresh SFTP client via conn.start_sftp_client() and delegates to linux_deploy_engines(run, quote, sftp, engines, engines_dir).
async def linux_setup_node(
    conn: SSHClientConnection,
    run: OuterRunCallable,
    quote: QuoteCallable,
    engines: EngineRepository,
    engines_dir: PurePath,
) -> None:
    """Set up generic linux node."""
    async with conn.start_sftp_client() as sftp:
        await linux_deploy_engines(run, quote, sftp, engines, engines_dir)


# endregion FUNC_linux_setup_node


# region FUNC__run_apt_with_retry
# PURPOSE: Run an apt-get command with retries on transient apt failures (exit 100) so a flaky repository mirror does not abort node setup.
# INVARIANTS:
# - Retries only on ProcessError with exit_status == 100 (transient apt repo failure); other non-zero exits propagate.
async def _run_apt_with_retry(
    run: OuterRunCallable, cmd: str, *, attempts: int = 3
) -> None:
    """Run an apt-get command, retrying on transient apt exit 100."""
    last_exc: ProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            await run(cmd, check=True)
        except ProcessError as exc:  # noqa: PERF203
            if exc.exit_status != _APT_TRANSIENT_EXIT:
                raise
            last_exc = exc
            logger.debug(
                "APT_TRANSIENT",
                extra={"cmd": cmd, "exit": exc.exit_status, "attempt": attempt},
            )
            if attempt < attempts:
                await asyncio.sleep(5 * attempt)
        else:
            return
    if last_exc is not None:
        raise last_exc


# endregion FUNC__run_apt_with_retry


# region FUNC_linux_setup_deb_node
# PURPOSE: Setup Debian-like node with apt package installation and engine deployment.
async def linux_setup_deb_node(
    conn: SSHClientConnection,
    run: OuterRunCallable,
    quote: QuoteCallable,
    engines: EngineRepository,
    engines_dir: PurePath,
) -> None:
    """Set up debian-like node."""
    is_root = conn.get_extra_info("username") == "root"
    sudo_prefix = "" if is_root else "sudo "
    apt_cmd = f"{sudo_prefix}apt-get -o DPkg::Lock::Timeout=600 -y"
    pkgs = engines.get_platform_packages()

    await _run_apt_with_retry(run, f"{apt_cmd} update")
    await _run_apt_with_retry(run, f"{apt_cmd} upgrade")
    if pkgs:
        logger.debug("INSTALL", extra={"packages": " ".join(pkgs)})
        await _run_apt_with_retry(run, f"{apt_cmd} install {' '.join(pkgs)}")
    if [x for x in pkgs if "mpi" in x]:
        await log_mpi_version(run)

    async with conn.start_sftp_client() as sftp:
        await linux_deploy_engines(run, quote, sftp, engines, engines_dir)


# endregion FUNC_linux_setup_deb_node
