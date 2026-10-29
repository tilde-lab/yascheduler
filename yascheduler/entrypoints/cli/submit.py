"""yasubmit CLI command — parse AiiDA script and submit task via DI."""
# region MODULE_CONTRACT
# PURPOSE: yasubmit CLI command — parse an AiiDA submission script, capture the engine input tree from the working directory, and submit the task with typed input entries via the DI layer.
# SCOPE: submit command — parse AiiDA script, capture inputs, and submit task via DI.
# KEYWORDS: submit, cli, aiida, script, task, capture
# endregion MODULE_CONTRACT

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from yascheduler.entrypoints import Config, make_cli_deps
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.logger import configure_cli_logger
from yascheduler.infra import capture_input_snapshot

from .args import (
    add_config_arg,
    add_log_level_arg,
    existing_path,
)

__all__ = ["submit"]


class EngineNotDefinedError(ValueError):
    """Script has not defined an engine."""

    def __init__(self) -> None:
        super().__init__("Script has not defined an engine")


class EngineNotSupportedError(ValueError):
    """Engine is not supported."""

    def __init__(self, engine_name: str) -> None:
        super().__init__(f"Engine {engine_name} is not supported")


# region FUNC__parse_submit_args
# PURPOSE: Declare the yasubmit argparse grammar — prog="yasubmit", one positional script validated by existing_path, plus the shared --config / --log-level flags — so the AiiDA plugin's command shape stays stable
# ENSURES: returns a Namespace whose script is a Path to an existing file or argparse exits 2 before this function returns
def _parse_submit_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="yasubmit",
        description="Submit task to yascheduler via AiiDA script",
    )
    parser.add_argument("script", type=existing_path)
    add_config_arg(parser)
    add_log_level_arg(parser, default="WARNING")
    # region BLOCK_parse_args
    return parser.parse_args(argv)
    # endregion BLOCK_parse_args


# endregion FUNC__parse_submit_args


# region FUNC__parse_script_metadata
# PURPOSE: Extract key=value metadata lines from an AiiDA submission script so the rest of the submit path can read them as a dict
# INVARIANTS: lines without exactly one = are silently skipped — no exception on malformed lines
def _parse_script_metadata(script_text: str) -> dict[str, str]:
    script_params: dict[str, str] = {}
    for line in script_text.splitlines():
        try:
            k, v = line.split("=")
            script_params[k.strip()] = v.strip()
        except ValueError:  # noqa: PERF203
            pass
    return script_params


# endregion FUNC__parse_script_metadata


# region FUNC__build_metadata
# PURPOSE: Assemble the task metadata dict — local_folder and webhook fields when applicable — so the submit use case receives a single ready payload; input files never enter metadata (the captured snapshot owns them).
# INVARIANTS: local_folder is always present; webhook fields present only when PARENT in script AND config.local.webhook_url is set
def _build_metadata(
    script_params: dict[str, str],
    config: Config,
    local_folder: str | Path,
) -> dict[str, Any]:
    # region BLOCK_build_metadata
    metadata: dict[str, Any] = {"local_folder": str(local_folder)}
    if "PARENT" in script_params and config.local.webhook_url:
        metadata["webhook_url"] = config.local.webhook_url
        metadata["webhook_custom_params"] = {"parent": script_params["PARENT"]}
    # endregion BLOCK_build_metadata
    return metadata


# endregion FUNC__build_metadata


# region FUNC__submit_async
# PURPOSE: Provide the async entry point for yasubmit that routes a parsed script through the DI layer to the submit use case, with structured exit codes so callers (CLI, AiiDA plugin) can distinguish success, runtime failure, and argparse error
# INVARIANTS: (a) success writes exactly str(task_id) to stdout; (b) failure writes Error: <message> to stderr and exits 1; (c) argparse failures (exit 2) propagate because SystemExit is not an Exception subclass
# RATIONALE:
# - Q: Why is there no --json / output-mode flag?
#   A: The AiiDA scheduler plugin parses int(stdout.strip()) of the subprocess; the success output is therefore fixed to str(task_id) and cannot be decorated.
async def _submit_async(argv: list[str] | None) -> None:
    args = _parse_submit_args(argv)
    script_file: Path = args.script

    # region BLOCK_handle_failure
    try:
        configure_cli_logger(logging.getLevelName(args.log_level))

        # region BLOCK_configure
        config = parse_config(args.config)
        deps = make_cli_deps(config)
        # endregion BLOCK_configure

        script_params = _parse_script_metadata(script_file.read_text())  # noqa: ASYNC240
        label = script_params.get("LABEL", "AiiDA job")

        # region BLOCK_validate_content
        engine_name = script_params.get("ENGINE")
        if not engine_name:
            raise EngineNotDefinedError  # noqa: TRY301
        engine = config.engines.get(engine_name)
        if not engine:
            raise EngineNotSupportedError(engine_name)  # noqa: TRY301
        # endregion BLOCK_validate_content

        # Capture the engine's input tree from the working directory (never
        # the script's directory) as typed entries; the snapshot replaces the
        # legacy text/base64 metadata values.
        input_entries = capture_input_snapshot(Path.cwd(), engine.input_files)

        metadata = _build_metadata(script_params, config, Path.cwd())

        # region BLOCK_submit
        task_id = await deps.submit(
            label, dict(metadata), engine.name, input_entries=input_entries
        )
        sys.stdout.write(f"{task_id!s}\n")
        # endregion BLOCK_submit
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    # endregion BLOCK_handle_failure


# endregion FUNC__submit_async


# region FUNC_submit
# PURPOSE: Bridge the sync CLI boundary to the async submit flow so the entry point does not force a sync implementation on the async subsystem
def submit(argv: list[str] | None = None) -> None:
    """Sync entry point — runs _submit_async via asyncio.run."""
    asyncio.run(_submit_async(argv))


# endregion FUNC_submit

if __name__ == "__main__":
    submit()
