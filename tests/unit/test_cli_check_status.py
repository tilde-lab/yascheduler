# region MODULE_CONTRACT
# PURPOSE: Unit tests for yastatus check_status() — parsing, renderers, exit codes, view-mode SSH.
# SCOPE: check_status() parsing, renderers, exit codes, view-mode SSH with mocked Config/CLIDeps/UoW/SSHMachineGateway.
# KEYWORDS: check_status, renderers, exit codes, SSH view-mode
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib
import json as _json
import logging
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.log_assertions import extra_fields
from yascheduler.domain import Engine, EngineRepository
from yascheduler.domain.model import (
    Done,
    Node,
    NodeId,
    Running,
    Task,
    TaskId,
    TaskStatus,
    Todo,
)
from yascheduler.entrypoints.di import CLIDeps

check_status_mod = importlib.import_module("yascheduler.entrypoints.cli.check_status")

pytestmark = pytest.mark.unit


# Mock helpers (duplicated across the CLI test modules)


def make_cloud(
    prefix: str,
    username: str = "root",
    jump_host: str | None = None,
    jump_username: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        prefix=prefix,
        username=username,
        jump_host=jump_host,
        jump_username=jump_username,
    )


def make_mock_config(
    clouds: list[SimpleNamespace] | None = None,
    remote_username: str = "root",
    remote_jump_host: str | None = None,
    remote_jump_username: str | None = None,
) -> MagicMock:
    """Return a MagicMock Config with all expected attributes."""
    engine = Engine(
        name="g09",
        spawn="run.sh",
        input_files=("input",),
        output_files=("OUTPUT",),
        platforms=("linux",),
        check_cmd="echo",
        check_pname=None,
    )

    engines = MagicMock(spec=EngineRepository)
    engines.get = MagicMock(return_value=engine)

    config = MagicMock()
    config.engines = engines
    config.clouds = clouds if clouds is not None else []
    config.remote.username = remote_username
    config.remote.jump_host = remote_jump_host
    config.remote.jump_username = remote_jump_username
    config.remote.engines_dir = "/opt/engines"
    config.remote.tasks_dir = PurePosixPath("/tmp/tasks")
    config.local.webhook_url = None
    config.local.data_dir = "/tmp"
    config.db = MagicMock()
    return config


def make_mock_uow() -> AsyncMock:
    """Return an AsyncMock UoW with .tasks and .nodes sub-mocks."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.tasks = AsyncMock()
    uow.nodes = AsyncMock()
    uow.commit = AsyncMock()
    return uow


def make_mock_deps(config: MagicMock, uow: AsyncMock) -> MagicMock:
    """Return a MagicMock CLIDeps wired to the given uow."""
    deps = MagicMock(spec=CLIDeps)
    deps.uow_factory = MagicMock(return_value=uow)
    deps.submit = AsyncMock(return_value=TaskId(42))
    deps.engines = config.engines
    return deps


def make_task(
    task_id: int = 1,
    status: TaskStatus = TaskStatus.RUNNING,
    label: str = "test",
    engine: str = "g09",
    allocated_node_id: NodeId | None = None,
    output_selections: tuple[str, ...] = (),
) -> Task:
    """Return a Task domain object with sensible defaults."""
    from datetime import datetime

    if status is TaskStatus.TO_DO:
        state: Todo | Running | Done = Todo(remote_folder="/tmp/remote")
    elif status is TaskStatus.RUNNING:
        state = Running(
            allocated_node_id=allocated_node_id or NodeId(1),
            remote_folder="/tmp/remote",
        )
    else:
        state = Done(
            error=None,
            allocated_node_id=allocated_node_id,
            remote_folder="/tmp/remote",
        )
    return Task(
        task_id=TaskId(task_id),
        label=label,
        engine=engine,
        state=state,
        local_folder="/tmp/local",
        webhook_url=None,
        webhook_custom_params={},
        extra={},
        output_selections=output_selections,
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


def make_mock_repository(
    stdout: str = "remote output",
    returncode: int = 0,
) -> MagicMock:
    """Return a MagicMock SSHMachineRepository with async connect/disconnect.

    repo.connect returns a session-like mock with the minimal MachineSession
    surface used by _display_remote_output / _download_convergence_snippet.
    """
    session = MagicMock()
    session.is_closed = False
    session.path = PurePosixPath
    session.quote = lambda s: s
    session.run_full = AsyncMock(
        return_value=MagicMock(returncode=returncode, stdout=stdout),
    )
    session.hostname = "10.0.0.1"
    session.machine = MagicMock(node_id=NodeId(1))
    session.open_sftp = AsyncMock()

    repo = MagicMock()
    repo.connect = AsyncMock(return_value=session)
    repo.disconnect = AsyncMock()
    return repo


@pytest.fixture
def stub_config_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, AsyncMock, MagicMock]:
    """Patch Config.from_config_parser and make_cli_deps for the module under test.

    Returns (config, uow, deps) so each test can wire its own node/task data.
    """
    config = make_mock_config()
    uow = make_mock_uow()
    deps = make_mock_deps(config, uow)
    monkeypatch.setattr(
        check_status_mod,
        "parse_config",
        MagicMock(return_value=config),
    )
    monkeypatch.setattr(check_status_mod, "make_cli_deps", MagicMock(return_value=deps))
    return config, uow, deps


def _run(argv: list[str]) -> None:
    """Invoke check_status_mod.check_status(argv)."""
    check_status_mod.check_status(argv)


# Parsing / exit codes


class TestCheckStatusParsing:
    """Flag parsing and argparse exit codes."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "usage: yastatus" in out
        assert "-j" in out and "--jobs" in out
        assert "-v" in out and "--view" in out
        assert "-i" in out and "--info" in out
        assert "-o" in out and "--convergence" in out
        assert "--json" in out

    def test_bogus_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--bogus"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert err.strip()

    def test_view_info_mutex_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["-v", "-i"])
        assert exc.value.code == 2

    def test_json_view_mutex_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--json", "-v"])
        assert exc.value.code == 2

    def test_json_info_mutex_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--json", "-i"])
        assert exc.value.code == 2

    def test_convergence_without_view_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["-o"])
        assert exc.value.code == 2

    def test_convergence_with_view_is_valid(self) -> None:
        args = check_status_mod._parse_status_args(["-o", "-v"])
        assert args.view is True
        assert args.convergence is True


# AiiDA-contract golden regression


class TestCheckStatusAiiDAContract:
    """The default renderer's output must parse via the AiiDA plugin's exact logic."""

    def test_default_output_parses_like_aiida_plugin(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        tasks = [
            make_task(task_id=1, status=TaskStatus.TO_DO),
            make_task(task_id=2, status=TaskStatus.RUNNING),
            make_task(task_id=3, status=TaskStatus.DONE),
        ]
        check_status_mod._render_default(tasks)
        out, _ = capsys.readouterr()
        job_list = [job.split() for job in out.split("\n") if job]
        parsed: dict[str, str] = dict(job_list)
        assert set(parsed.values()) <= {"TO_DO", "RUNNING", "DONE"}
        assert parsed == {"1": "TO_DO", "2": "RUNNING", "3": "DONE"}


# Default renderer + -j filter


class TestCheckStatusDefault:
    """Default invocation: list_by_status({RUNNING, TO_DO}); -j calls list_by_jobs."""

    def test_default_lists_running_and_todo(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(task_id=1, status=TaskStatus.RUNNING, label="job_a"),
                make_task(task_id=2, status=TaskStatus.TO_DO, label="job_b"),
            ],
        )
        _run([])
        uow.tasks.list_by_status.assert_called_once_with(
            statuses={TaskStatus.RUNNING, TaskStatus.TO_DO},
        )
        out, _ = capsys.readouterr()
        assert "1   RUNNING" in out
        assert "2   TO_DO" in out

    def test_default_excludes_done(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[make_task(task_id=1, status=TaskStatus.RUNNING)],
        )
        _run([])
        out, _ = capsys.readouterr()
        assert "DONE" not in out

    def test_jobs_filter_calls_list_by_jobs(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_jobs = AsyncMock(
            return_value=[make_task(task_id=1, status=TaskStatus.RUNNING, label="x")],
        )
        _run(["-j", "1", "2"])
        uow.tasks.list_by_jobs.assert_called_once_with(job_ids=[TaskId(1), TaskId(2)])
        out, _ = capsys.readouterr()
        assert "1   RUNNING" in out


# Info renderer


class TestCheckStatusInfo:
    """-i prints tab-separated task_id/status/label/ip."""

    def test_info_tab_separated(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    label="job_a",
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        _run(["-i"])
        out, _ = capsys.readouterr()
        assert "task_id=1" in out
        assert "status=RUNNING" in out
        assert "label=job_a" in out
        assert "node_id=1" in out
        assert "\t" in out


# JSON renderer


class TestCheckStatusJson:
    """--json emits a list of 9-field objects with raw domain values."""

    def test_json_emits_list_with_nine_fields(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=4,
            enabled=True,
            port=22,
            cloud="hetzner",
        )
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(return_value={NodeId(1): node})
        _run(["--json"])
        out, _ = capsys.readouterr()
        data = _json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 1
        obj = data[0]
        assert set(obj.keys()) == {
            "task_id",
            "status",
            "label",
            "engine",
            "local_folder",
            "remote_folder",
            "created_at",
            "updated_at",
            "node",
        }
        assert obj["status"] == "RUNNING"
        assert obj["node"]["hostname"] == "10.0.0.1"
        assert obj["node"]["port"] == 22
        assert obj["node"]["username"] == "root"
        assert obj["node"]["cloud"] == "hetzner"
        assert obj["node"]["jump_host"] is None
        assert obj["node"]["jump_port"] == 22
        assert obj["node"]["jump_username"] == "root"
        assert obj["node"]["external_id"] is None
        assert obj["node"]["status"] == "OTHER"
        assert isinstance(obj["node"]["created_at"], str)
        assert isinstance(obj["node"]["updated_at"], str)
        assert obj["engine"] == "g09"
        # local_folder is a status-independent Task field (D6): the make_task
        # helper sets it to "/tmp/local" for all statuses, so a RUNNING task
        # carries the submit intent through to the consume step.
        assert obj["local_folder"] == "/tmp/local"
        assert obj["remote_folder"] == "/tmp/remote"
        assert "allocated_ip" not in obj
        assert "port" not in obj
        assert "cloud" not in obj
        uow.nodes.get_by_ids.assert_called_once()

    def test_json_fetches_nodes_by_id(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(
                    node_id=NodeId(1),
                    hostname="10.0.0.1",
                    ncpus=4,
                    port=22,
                ),
            },
        )
        _run(["--json"])
        uow.nodes.get_by_ids.assert_called_once()

    def test_json_todo_task_has_null_placement(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[make_task(task_id=5, status=TaskStatus.TO_DO)],
        )
        _run(["--json"])
        out, _ = capsys.readouterr()
        data = _json.loads(out)
        obj = data[0]
        assert obj["node"] is None
        assert "allocated_ip" not in obj
        assert "port" not in obj
        assert "cloud" not in obj
        assert obj["engine"] == "g09"

    def test_json_empty_result_is_empty_list(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        _run(["--json"])
        out, _ = capsys.readouterr()
        assert _json.loads(out) == []

    def test_json_composes_with_jobs(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_jobs = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(
                    node_id=NodeId(1),
                    hostname="10.0.0.1",
                    ncpus=4,
                    port=22,
                ),
            },
        )
        _run(["--json", "-j", "1"])
        uow.tasks.list_by_jobs.assert_called_once_with(job_ids=[TaskId(1)])
        out, _ = capsys.readouterr()
        assert _json.loads(out)


# Exit codes


class TestCheckStatusExitCodes:
    """0/1/2 exit-code contract."""

    def test_exit_zero_on_success(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        _run([])

    def test_exit_one_on_db_error(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(side_effect=RuntimeError("db down"))
        with pytest.raises(SystemExit) as exc:
            _run([])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    @pytest.mark.parametrize(
        ("attr", "error"),
        [
            pytest.param("parse_config", RuntimeError("bad config"), id="config-error"),
            pytest.param(
                "make_cli_deps", ValueError("unexpected"), id="unexpected-exception"
            ),
        ],
    )
    def test_exit_one_on_startup_failure(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        attr: str,
        error: Exception,
    ) -> None:
        monkeypatch.setattr(check_status_mod, attr, MagicMock(side_effect=error))
        with pytest.raises(SystemExit) as exc:
            _run([])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err


# argv injection


class TestCheckStatusArgvInjection:
    """The argv parameter is threaded through to argparse (no patch sys.argv)."""

    def test_argv_threaded_to_argparse(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        _run(["--json"])
        out, _ = capsys.readouterr()
        assert _json.loads(out) == []


# View mode happy path + lazy nodes lookup


class TestCheckStatusViewAcceptance:
    """3 Gherkin scenarios for node-owns-connection-identity."""

    @pytest.mark.parametrize(
        "node_kwargs,cloud_kwargs_list,connect_asserts",
        [
            (
                {"cloud": "hetzner", "username": "yascheduler", "port": 22},
                [{"username": "hcloud-user"}],
                lambda k: "username" not in k and k["node"].username == "yascheduler",
            ),
            (
                {
                    "cloud": "hetzner",
                    "username": "yascheduler",
                    "port": 22,
                    "jump_host": "jump.example.com",
                    "jump_username": "jumper",
                },
                [{"jump_host": "jump.example.com", "jump_username": "jumper"}],
                lambda k: (
                    "jump_host" not in k
                    and "jump_username" not in k
                    and k["node"].jump_host == "jump.example.com"
                    and k["node"].jump_username == "jumper"
                ),
            ),
            (
                {
                    "cloud": "hetzner",
                    "username": "yascheduler",
                    "port": 22,
                    "jump_host": "old-bastion.example.com",
                    "jump_username": "old-jumper",
                },
                [
                    {
                        "jump_host": "new-bastion.example.com",
                        "jump_username": "new-jumper",
                    },
                ],
                lambda k: (
                    "jump_host" not in k
                    and "jump_username" not in k
                    and k["node"].jump_host == "old-bastion.example.com"
                    and k["node"].jump_username == "old-jumper"
                ),
            ),
        ],
        ids=[
            "uses_node_username_not_cloud_username",
            "reads_jump_from_node_not_cloud",
            "follows_node_jump_when_cloud_changed",
        ],
    )
    def test_scenarios(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
        node_kwargs: dict,
        cloud_kwargs_list: list[dict],
        connect_asserts: Callable[[dict], bool],
    ) -> None:
        config, uow, _deps = stub_config_deps
        config.clouds = [make_cloud("hetzner", **ck) for ck in cloud_kwargs_list]
        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, **node_kwargs)
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(return_value={NodeId(1): node})
        repo = make_mock_repository()
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=repo),
        )
        _run(["-v"])
        assert connect_asserts(repo.connect.call_args.kwargs)


class TestCheckStatusViewHappyPath:
    """-v SSH tail path; default/-i do NOT fetch nodes (lazy lookup invariant)."""

    def test_view_connects_with_resolved_params_and_disconnects(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config, uow, _deps = stub_config_deps
        config.clouds = [make_cloud("hetzner")]
        node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=4,
            cloud="hetzner",
            username="yascheduler",
            port=2222,
            jump_host="jump.example.com",
            jump_username="jumper",
        )
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(return_value={NodeId(1): node})
        repo = make_mock_repository(stdout="OUTPUT TAIL")
        session = repo.connect.return_value
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=repo),
        )
        _run(["-v"])
        repo.connect.assert_called_once()
        kwargs = repo.connect.call_args.kwargs
        assert "username" not in kwargs
        assert "port" not in kwargs
        assert "jump_host" not in kwargs
        assert "jump_username" not in kwargs
        assert kwargs["node"].username == "yascheduler"
        assert kwargs["node"].port == 2222
        assert kwargs["node"].jump_host == "jump.example.com"
        assert kwargs["node"].jump_username == "jumper"
        assert session.run_full.await_count == 1
        repo.disconnect.assert_awaited_with(session.machine.node_id)
        out, _ = capsys.readouterr()
        assert "OUTPUT TAIL" in out

    def test_view_fetches_nodes_by_id(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4),
            },
        )
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=make_mock_repository()),
        )
        _run(["-v"])
        uow.nodes.get_by_ids.assert_called_once()

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="default"),
            pytest.param(["-i"], id="info"),
        ],
    )
    def test_non_view_modes_do_not_fetch_nodes(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        argv: list[str],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        _run(argv)
        uow.nodes.get_by_ids.assert_not_called()


class TestCheckStatusViewOutputSelections:
    """View tails the task-owned selection, with a logged engine fallback for legacy tasks."""

    def test_view_tails_recorded_selection_over_engine_config(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                    output_selections=("recorded.out",),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4),
            },
        )
        repo = make_mock_repository(stdout="RECORDED TAIL")
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=repo),
        )
        _run(["-v"])
        session = repo.connect.return_value
        command = session.run_full.await_args.args[0]
        assert "recorded.out" in command
        assert "OUTPUT" not in command
        out, _ = capsys.readouterr()
        assert "RECORDED TAIL" in out

    def test_view_legacy_task_uses_engine_fallback_and_warns(
        self,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=7,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4),
            },
        )
        repo = make_mock_repository(stdout="FALLBACK TAIL")
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=repo),
        )
        with caplog.at_level(logging.WARNING, logger=check_status_mod.__name__):
            _run(["-v"])
        session = repo.connect.return_value
        command = session.run_full.await_args.args[0]
        assert "OUTPUT" in command
        out, _ = capsys.readouterr()
        assert "FALLBACK TAIL" in out
        warnings = [
            record for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "task_id=7" in message
        assert "engine 'g09'" in message
        fields = extra_fields(warnings[0])
        fields.pop("message", None)
        assert fields == {"task_id": 7, "engine": "g09"}

    def test_view_pattern_selection_is_named_without_ssh(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _config, uow, _deps = stub_config_deps
        pattern = "results/**/*.dat"
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                    output_selections=(pattern,),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4),
            },
        )
        repo = make_mock_repository()
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=repo),
        )
        _run(["-v"])
        repo.connect.assert_not_called()
        repo.connect.return_value.run_full.assert_not_called()
        out, _ = capsys.readouterr()
        assert pattern in out
        assert "PATTERN" in out


# Query/render separation


class TestCheckStatusQueryRenderSeparation:
    """The query-phase UoW is closed before any SSH operation begins."""

    def test_uow_closed_before_ssh_connect(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    status=TaskStatus.RUNNING,
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.get_by_ids = AsyncMock(
            return_value={
                NodeId(1): Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4),
            },
        )
        repo = make_mock_repository()
        monkeypatch.setattr(
            check_status_mod,
            "SSHMachineRepository",
            MagicMock(return_value=repo),
        )
        manager = MagicMock()
        manager.attach_mock(uow.__aexit__, "uow_aexit")
        manager.attach_mock(repo.connect, "repo_connect")
        _run(["-v"])
        names = [call[0] for call in manager.mock_calls]
        assert "uow_aexit" in names
        assert "repo_connect" in names
        assert names.index("uow_aexit") < names.index("repo_connect")


# --config / --log-level scenarios (consolidate-daemon-entrypoints)


class TestCheckStatusConfigLogLevel:
    """--config and --log-level argparse + behavior scenarios (defaults WARNING)."""

    def test_help_lists_config_and_log_level(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "--config" in out
        assert "--log-level" in out

    def test_config_nonexistent_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--config", "/nonexistent.conf"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert "not a file" in err
        assert "/nonexistent.conf" in err

    def test_log_level_warn_rejected_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            check_status_mod._parse_status_args(["--log-level", "WARN"])
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        ("argv", "expected_level"),
        [
            pytest.param([], logging.WARNING, id="default-warning"),
            pytest.param(["--log-level", "DEBUG"], logging.DEBUG, id="debug-flag"),
        ],
    )
    def test_root_log_level(
        self,
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        argv: list[str],
        expected_level: int,
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        root = logging.getLogger()
        original_level = root.level
        try:
            _run(argv)
            assert root.level == expected_level
        finally:
            root.setLevel(original_level)

    def test_config_custom_passed_to_from_config_parser(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        custom_conf = tmp_path / "custom.conf"
        custom_conf.write_text("[local]")
        from_config_spy = MagicMock(return_value=make_mock_config())
        monkeypatch.setattr(check_status_mod, "parse_config", from_config_spy)
        uow = make_mock_uow()
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        monkeypatch.setattr(
            check_status_mod,
            "make_cli_deps",
            MagicMock(return_value=make_mock_deps(make_mock_config(), uow)),
        )
        _run(["--config", str(custom_conf)])
        from_config_spy.assert_called_once_with(custom_conf)

    def test_default_config_is_config_file(
        self,
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from yascheduler import CONFIG_FILE

        _config, uow, _deps = stub_config_deps
        uow.tasks.list_by_status = AsyncMock(return_value=[])
        from_config_spy = MagicMock(return_value=make_mock_config())
        monkeypatch.setattr(check_status_mod, "parse_config", from_config_spy)
        _run([])
        assert str(from_config_spy.call_args.args[0]) == str(CONFIG_FILE)
