"""yainit CLI command — install service unit files and/or apply DB schema + migrations, with --schema/--daemon subset-selector flags."""
# region MODULE_CONTRACT
# PURPOSE: Bootstrap the yascheduler environment — install service unit files for the detected init system and/or ensure the database schema is current — so the daemon can run and the CLI can operate.
# SCOPE: init command — service install and/or DB schema+migrations application.
# KEYWORDS: init, schema, migration, systemd, sysv, cli
# endregion MODULE_CONTRACT

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pg8000 import DatabaseError

from yascheduler.entrypoints import CONFIG_FILE
from yascheduler.entrypoints.config_parser import parse_config
from yascheduler.entrypoints.logger import configure_cli_logger
from yascheduler.infra import apply_migrations, apply_schema

from .args import add_config_arg, add_log_level_arg

__all__ = ["init"]


# region FUNC__init_systemd
# PURPOSE: Install/reinstall the systemd service unit so systemd can start, stop, and supervise the yascheduler daemon.
def _init_systemd(
    install_path: Path,
    unit_file: Path = Path("/etc/systemd/system/yascheduler.service"),
) -> None:
    sys.stdout.write("Installing systemd service\n")
    src_unit_file = install_path / "data/yascheduler.service"
    daemon_file = install_path / "entrypoints/cli/daemon_systemd.py"
    systemd_script = src_unit_file.read_text("utf-8").replace(
        "%YASCHEDULER_DAEMON_FILE%",
        str(daemon_file),
    )
    try:
        unit_file.write_text(systemd_script, "utf-8")
    except OSError as e:
        sys.stderr.write(f"Error: cannot write to {unit_file}: {e}\n")
        sys.exit(1)


# endregion FUNC__init_systemd


# region FUNC__init_sysv
# PURPOSE: Install/reinstall the SysV init script so the service manager can start, stop, and supervise the yascheduler daemon.
def _init_sysv(
    install_path: Path,
    startup_file: Path = Path("/etc/init.d/yascheduler"),
) -> None:
    sys.stdout.write("Installing SysV service\n")
    src_startup_file = install_path / "data/yascheduler.sh"
    daemon_file = install_path / "entrypoints/cli/daemon_sysv.py"
    sysv_script = src_startup_file.read_text("utf-8").replace(
        "%YASCHEDULER_DAEMON_FILE%",
        str(daemon_file),
    )
    try:
        startup_file.write_text(sysv_script, "utf-8")
        startup_file.chmod(0o755)
    except OSError as e:
        sys.stderr.write(f"Error: cannot write to {startup_file}: {e}\n")
        sys.exit(1)


# endregion FUNC__init_sysv


# region FUNC__init_schema
# PURPOSE: Ensure the database has the required schema and up-to-date migrations so the daemon and CLI can operate against a valid database.
# INVARIANTS: Engine input declarations are derived from the parsed engines config as plain values and passed to migrations — infra never imports the entrypoints config parser.
def _init_schema(config_path: str | Path = CONFIG_FILE) -> None:
    config = parse_config(config_path)
    try:
        # region BLOCK_apply_schema
        apply_schema(config.db)
        # endregion BLOCK_apply_schema
        # region BLOCK_apply_migrations
        engine_inputs = {
            name: engine.input_files for name, engine in config.engines.items()
        }
        apply_migrations(config.db, engine_inputs=engine_inputs)
        # endregion BLOCK_apply_migrations
    except DatabaseError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


# endregion FUNC__init_schema


# region FUNC_init
# PURPOSE: Parse --schema/--daemon flags and dispatch to subset initializers so the operator can install services, apply schema, or both without running the full bootstrap.
# INVARIANTS:
# - Systemd-vs-sysv detection is /run/systemd/system directory.
# - try/except Exception prints Error: <message>, exits 1; SystemExit propagates.
# RATIONALE:
# - Q: Why does yainit skip DI and call apply_schema/apply_migrations directly?
#   A: Bootstrapping — the database may not yet exist or may not yet have the schema that DI's repository adapters assume.
def init(argv: list[str] | None = None) -> None:
    """Parse --schema/--daemon flags, install service and/or apply schema per the selected subset, exit 0/1/2."""
    parser = argparse.ArgumentParser(
        prog="yainit",
        description="Install yascheduler service and initialize the database schema",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Apply only the database schema",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Install only the service unit file",
    )
    add_config_arg(parser)
    add_log_level_arg(parser, default="WARNING")

    # region BLOCK_validate_flags
    args = parser.parse_args(argv)
    # endregion BLOCK_validate_flags

    install_path = Path(__file__).parent.parent.parent  # yascheduler/

    # region BLOCK_handle_failure
    try:
        # region BLOCK_configure_cli_logger
        configure_cli_logger(logging.getLevelName(args.log_level))
        # endregion BLOCK_configure_cli_logger

        # region BLOCK_dispatch
        run_daemon = not args.schema or args.daemon
        run_schema = not args.daemon or args.schema
        # endregion BLOCK_dispatch

        if run_daemon:
            # region BLOCK_detect_init_system
            has_systemd = Path("/run/systemd/system").is_dir()
            # endregion BLOCK_detect_init_system
            if has_systemd:
                _init_systemd(install_path)
            else:
                _init_sysv(install_path)

        if run_schema:
            _init_schema(args.config)

        sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)
    # endregion BLOCK_handle_failure


# endregion FUNC_init

if __name__ == "__main__":
    init()
