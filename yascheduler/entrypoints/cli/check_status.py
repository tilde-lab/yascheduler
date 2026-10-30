"""yastatus CLI command — query and display task status."""
# region MODULE_CONTRACT
# PURPOSE: yastatus CLI command — query and display task status with optional remote output tailing and CRYSTAL convergence parsing.
# SCOPE: check_status command — query and display task status with multiple renderers (default, info, json, view).
# KEYWORDS: status, cli, query, display, render, convergence
# endregion MODULE_CONTRACT

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from yascheduler.domain import (
    NodeId,
    TaskId,
    TaskStatus,
    allocated_node_id_of,
    is_glob_pattern,
    remote_folder_of,
)
from yascheduler.entrypoints import Config, make_cli_deps
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.logger import configure_cli_logger
from yascheduler.infra import SSHMachineRepository, list_private_keys

from .args import add_config_arg, add_log_level_arg

if TYPE_CHECKING:
    from yascheduler.application import AbstractUnitOfWork
    from yascheduler.domain import AnyTask, MachineSession, Node

__all__ = ["check_status"]

logger = logging.getLogger(__name__)


# region FUNC__parse_status_args
# PURPOSE: Declare the yastatus argparse grammar — prog="yastatus", the -j filter, the three-renderer mutex group, and the -o convergence modifier — so the flag matrix is observable in one place.
# ENSURES: returns a Namespace whose convergence flag is never active without view; argparse exits 2 on -v -i, and the body-level parser.error exits 2 on -o without -v.
def _parse_status_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="yastatus",
        description="Show status of tasks",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        nargs="*",
        type=int,
        default=None,
        help="Filter to the given job ids (composes with any renderer)",
    )
    # The three renderers are mutually exclusive; none means the default AiiDA-compatible renderer.
    mutex = parser.add_mutually_exclusive_group()
    mutex.add_argument(
        "-v",
        "--view",
        action="store_true",
        default=False,
        help="Verbose renderer: tail remote OUTPUT (and optional convergence)",
    )
    mutex.add_argument(
        "-i",
        "--info",
        action="store_true",
        default=False,
        help="Tab-separated one-line-per-task renderer",
    )
    mutex.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit JSON with raw domain values (no display transformations)",
    )
    # -o modifies -v, so it is OUTSIDE the mutex group; the body-check below enforces the dependency.
    parser.add_argument(
        "-o",
        "--convergence",
        action="store_true",
        default=False,
        help="Download + parse a CRYSTAL convergence snippet (requires --view)",
    )
    add_config_arg(parser)
    add_log_level_arg(parser, default="WARNING")
    # region BLOCK_parse_args
    args = parser.parse_args(argv)
    if args.convergence and not args.view:
        parser.error("--convergence requires --view")
    # endregion BLOCK_parse_args
    return args


# endregion FUNC__parse_status_args


# region FUNC__query_tasks
# PURPOSE: Pick the right task query based on whether the operator passed -j so the renderer always receives a task list.
async def _query_tasks(
    uow: AbstractUnitOfWork, args: argparse.Namespace
) -> list[AnyTask]:
    # region BLOCK_query
    if args.jobs:
        # argparse yields list[int]; wrap to TaskId before crossing into the
        # repository (same int→TaskId marshalling pattern as the facade).
        return await uow.tasks.list_by_jobs(job_ids=[TaskId(j) for j in args.jobs])
    return await uow.tasks.list_by_status(
        statuses={TaskStatus.RUNNING, TaskStatus.TO_DO},
    )
    # endregion BLOCK_query


# endregion FUNC__query_tasks


# region FUNC__render_default
# PURPOSE: Emit task status lines in the format the AiiDA scheduler plugin's joblist parser expects — bare <task_id>   <STATUS> with no header, footer, or summary — so phantom jobs are never introduced by decoration.
def _render_default(tasks: list[AnyTask]) -> None:
    for task in tasks:
        sys.stdout.write(f"{task.task_id}   {task.status.name}\n")


# endregion FUNC__render_default


# region FUNC__render_info
# PURPOSE: Render task metadata as parseable tab-separated lines so operators and scripts can consume structured status without depending on JSON or the AiiDA format.
def _render_info(tasks: list[AnyTask]) -> None:
    for task in tasks:
        sys.stdout.write(
            "task_id={}\tstatus={}\tlabel={}\tnode_id={}\n".format(
                task.task_id,
                task.status.name,
                task.label,
                allocated_node_id_of(task) or "-",
            ),
        )


# endregion FUNC__render_info


# region FUNC__render_json
# PURPOSE: Serialize task and node state as JSON with raw domain values so automation tools can consume the full status programmatically without parsing human-oriented text.
def _render_json(tasks: list[AnyTask], nodes_by_id: dict[NodeId, Node]) -> str:
    # region BLOCK_render_json
    objects = []
    for task in tasks:
        allocated_node_id = allocated_node_id_of(task)
        node = nodes_by_id.get(allocated_node_id) if allocated_node_id else None
        objects.append(
            {
                "task_id": task.task_id.value,
                "status": task.status.name,
                "label": task.label,
                "engine": task.engine,
                "local_folder": task.local_folder,
                "remote_folder": remote_folder_of(task),
                "created_at": task.created_at.isoformat(),
                "updated_at": task.updated_at.isoformat(),
                "node": (
                    {
                        "hostname": node.hostname,
                        "port": node.port,
                        "username": node.username,
                        "cloud": node.cloud,
                        "jump_host": node.jump_host,
                        "jump_port": node.jump_port,
                        "jump_username": node.jump_username,
                        "external_id": node.external_id,
                        "status": node.status.name,
                        "created_at": node.created_at.isoformat(),
                        "updated_at": node.updated_at.isoformat(),
                    }
                    if node is not None
                    else None
                ),
            },
        )
    return json.dumps(objects)
    # endregion BLOCK_render_json


# endregion FUNC__render_json


# region FUNC__download_convergence_snippet
# PURPOSE: Fetch a remote CRYSTAL OUTPUT file via SFTP to a local temp path so _parse_convergence can parse it without monkey-patching pycrystal's I/O.
# ENSURES: returns True on successful SFTP transfer; returns False on OSError so the caller can skip convergence display rather than crash.
async def _download_convergence_snippet(
    session: MachineSession,
    remote_folder: str,
    local_path: Path,
    output_file: str = "OUTPUT",
) -> bool:
    """Download OUTPUT file via SFTP for convergence parsing. Returns True on success."""
    try:
        r_output = session.path(remote_folder) / output_file
        async with session.open_sftp() as sftp:
            await sftp.get([str(r_output)], local_path)
    except OSError:
        return False
    else:
        return True


# endregion FUNC__download_convergence_snippet


# region FUNC__parse_convergence
# PURPOSE: Convert a CRYSTAL output file into the human-readable convergence + optgeom text block the operator expects.
# INVARIANTS: returns the CRYSTOUT_Error message verbatim on parse failure instead of raising — the verbose renderer needs to print SOMETHING for a corrupt file.
# SCOPE: numerical formatting of optgeom cycles; NOT: file fetching or session lifecycle.
def _parse_convergence(filepath: Path) -> str:
    """Parse CRYSTAL output file for convergence and geometry optimization info."""
    from numpy import nan  # noqa: PLC0415
    from pycrystal import (  # noqa: PLC0415
        CRYSTOUT,
        CRYSTOUT_Error,
    )

    try:
        calc = CRYSTOUT(filepath)
    except CRYSTOUT_Error as err:
        return str(err)

    output_lines = ""
    if calc.info["convergence"]:
        output_lines += str(calc.info["convergence"]) + "\n"
    if calc.info["optgeom"]:
        for n in range(len(calc.info["optgeom"])):
            try:
                ncycles = calc.info["ncycles"][n]
            except IndexError:
                ncycles = "^"
            output_lines += (
                "{:8f}".format(calc.info["optgeom"][n][0] or nan)
                + "  "
                + "{:8f}".format(calc.info["optgeom"][n][1] or nan)
                + "  "
                + "{:8f}".format(calc.info["optgeom"][n][2] or nan)
                + "  "
                + "{:8f}".format(calc.info["optgeom"][n][3] or nan)
                + "  "
                + "E={:12f}".format(calc.info["optgeom"][n][4] or nan)
                + " eV"
                + "  "
                + f"({ncycles})"
                + "\n"
            )
    return output_lines


# endregion FUNC__parse_convergence


# region FUNC__display_remote_output
# PURPOSE: Connect to the remote machine and tail the last lines of the task's output file so the operator can inspect running-job progress without logging into the remote host separately.
# INVARIANTS:
# - connects via repository.connect(node=node, ...) reading login user/port/jump-leg from the Node, never passing jump_host/jump_username separately
# - tails the task's first recorded output selection, falling back to the engine's first configured selection for legacy tasks
# - a pattern selection is named and skipped before any SSH connection is opened
async def _display_remote_output(
    task: AnyTask,
    node: Node | None,
    config: Config,
) -> tuple[MachineSession, str, SSHMachineRepository, str] | None:
    """Tail the task's first output selection, or the configured fallback."""
    if node is None:
        sys.stdout.write("NO ALLOCATED HOSTNAME\n")
        return None
    selections = task.output_selections
    if selections:
        output_file = selections[0]
    else:
        logger.warning(
            "task_id=%s has no recorded output selections; using engine '%s' configuration",
            task.task_id,
            task.engine,
            extra={"task_id": task.task_id.value, "engine": task.engine},
        )
        engine = config.engines.get(task.engine)
        output_file = (
            engine.output_files[0] if engine and engine.output_files else "OUTPUT"
        )
    if is_glob_pattern(output_file):
        sys.stdout.write(
            f"OUTPUT SELECTION IS A PATTERN, SKIPPING REMOTE TAIL: {output_file}\n",
        )
        return None
    repository = SSHMachineRepository()
    try:
        session = await repository.connect(
            node=node,
            client_keys=list_private_keys(config.local.keys_dir),
        )
    except Exception:
        sys.stdout.write("CAN'T CONNECT\n")
        return None
    remote_folder = remote_folder_of(task)
    if not remote_folder:
        sys.stdout.write("OUTDATED TASK, SKIPPING\n")
        await repository.disconnect(session.machine.node_id)
        return None
    if session.is_closed:
        sys.stdout.write("CAN'T CONNECT\n")
        return None
    # NB master output is given first
    r_output = session.path(remote_folder) / output_file
    result = await session.run_full(
        f"tail -n15 {session.quote(str(r_output))}",
    )
    if result.returncode:
        sys.stdout.write("OUTDATED TASK, SKIPPING\n")
    else:
        sys.stdout.write(f"{result.stdout}\n")
    return session, remote_folder, repository, output_file


# endregion FUNC__display_remote_output


# region FUNC__render_view
# PURPOSE: Display a per-task detailed view of running jobs — header, remote OUTPUT tail, optional convergence snippet — so the operator can assess running-job health from one command.
# INVARIANTS:
# - creates convergence snippet temp file ONCE, reuses across all RUNNING tasks
# - unlinks in finally clause — no leak on success, exception, or early-return
async def _render_view(
    tasks: list[AnyTask],
    nodes_by_id: dict[NodeId, Node],
    config: Config,
    fetch_convergence: bool,
) -> Path | None:
    running = [t for t in tasks if t.status == TaskStatus.RUNNING]
    snippet: Path | None = None
    # region BLOCK_create_snippet
    if fetch_convergence:
        fd, name = tempfile.mkstemp(suffix=".tmp")
        os.close(fd)
        snippet = Path(name)
    # endregion BLOCK_create_snippet
    try:
        # region BLOCK_iterate_running
        for task in running:
            allocated_node_id = allocated_node_id_of(task)
            node = nodes_by_id.get(allocated_node_id) if allocated_node_id else None
            username = node.username if node is not None else config.remote.username
            cloud_str = node.cloud if node and node.cloud else ""
            sys.stdout.write(
                "." * 50
                + "ID{} {} at {}@{}:{}:{}\n".format(
                    task.task_id,
                    task.label,
                    username,
                    node.hostname if node else "",
                    cloud_str,
                    remote_folder_of(task) or "",
                ),
            )
            conn = await _display_remote_output(task, node, config)
            if conn is None:
                continue
            session, remote_folder, repository, output_file = conn
            try:
                if fetch_convergence and snippet is not None:
                    success = await _download_convergence_snippet(
                        session,
                        remote_folder,
                        snippet,
                        output_file,
                    )
                    if success:
                        output = _parse_convergence(snippet)
                        if output:
                            sys.stdout.write(f"{output}\n")
            finally:
                await repository.disconnect(session.machine.node_id)
        # endregion BLOCK_iterate_running
    except Exception:
        # Self-clean the snippet on exception so the temp file never leaks; re-raise to the caller's
        # top-level handler (which prints "Error: ..." and exits 1).
        if snippet is not None and snippet.exists():
            snippet.unlink()
        raise
    return snippet


# endregion FUNC__render_view


# region FUNC__check_status_async
# PURPOSE: Orchestrate the full check_status lifecycle — parse args, query tasks, fetch nodes, render with the chosen formatter, and cleanup the temp snippet — with a single exit-1 catch-all so the operator never sees an unhandled traceback.
async def _check_status_async(argv: list[str] | None) -> None:
    snippet: Path | None = None
    # region BLOCK_handle_failure
    try:
        args = _parse_status_args(argv)
        # region BLOCK_configure_cli_logger
        configure_cli_logger(logging.getLevelName(args.log_level))
        # endregion BLOCK_configure_cli_logger

        config = parse_config(args.config)
        deps = make_cli_deps(config)
        # QUERY PHASE — one short UoW, closed before any SSH work.
        async with deps.uow_factory() as uow:
            tasks = await _query_tasks(uow, args)
            nodes_by_id: dict[NodeId, Node] = {}
            if args.view or args.json:
                node_ids = [
                    nid for t in tasks if (nid := allocated_node_id_of(t)) is not None
                ]
                if node_ids:
                    nodes_by_id = await uow.nodes.get_by_ids(node_ids)
        # RENDER PHASE — no DB connection held during SSH.
        if args.view:
            snippet = await _render_view(
                tasks,
                nodes_by_id,
                config,
                bool(args.convergence),
            )
        elif args.info:
            _render_info(tasks)
        elif args.json:
            sys.stdout.write(f"{_render_json(tasks, nodes_by_id)}\n")
        else:
            _render_default(tasks)
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    # endregion BLOCK_handle_failure
    finally:
        if snippet is not None and snippet.exists():
            snippet.unlink()


# endregion FUNC__check_status_async


# region FUNC_check_status
# PURPOSE: Provide a synchronous CLI entry point by wrapping the async status query in asyncio.run so setuptools console_scripts can invoke it without async plumbing.
def check_status(argv: list[str] | None = None) -> None:
    """Sync entry point — runs _check_status_async via asyncio.run."""
    asyncio.run(_check_status_async(argv))


# endregion FUNC_check_status

if __name__ == "__main__":
    check_status()
