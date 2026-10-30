# region MODULE_CONTRACT
# PURPOSE: Unit tests for yanodes show_nodes() flag parsing, filtering, table/JSON rendering, and exit codes.
# SCOPE: show_nodes() flag parsing, filtering, table/JSON rendering, exit codes with mocked Config/CLIDeps/UoW.
# KEYWORDS: yanodes, show_nodes, filtering, table/JSON rendering
# endregion MODULE_CONTRACT

from __future__ import annotations

import importlib
import logging
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock

import pytest

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

show_nodes_mod = importlib.import_module("yascheduler.entrypoints.cli.show_nodes")

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Mock helpers (duplicated across the CLI test modules)
# ---------------------------------------------------------------------------


def make_mock_config() -> MagicMock:
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
    config.clouds = []
    config.remote.username = "root"
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
    allocated_node_id: NodeId | None = None,
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
        engine="g09",
        state=state,
        local_folder="/tmp/local",
        webhook_url=None,
        webhook_custom_params={},
        extra={},
        created_at=datetime(2025, 1, 1),
        updated_at=datetime(2025, 1, 1),
    )


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
    monkeypatch.setattr(show_nodes_mod, "parse_config", MagicMock(return_value=config))
    monkeypatch.setattr(show_nodes_mod, "make_cli_deps", MagicMock(return_value=deps))
    return config, uow, deps


def _run(argv: list[str]) -> None:
    """Invoke show_nodes_mod.show_nodes(argv); raise if it swallows SystemExit unexpectedly."""
    show_nodes_mod.show_nodes(argv)


# ---------------------------------------------------------------------------
# Parsing / exit codes
# ---------------------------------------------------------------------------


class TestShowNodesParsing:
    """Flag parsing and argparse exit codes."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "--json" in out
        assert "--enabled" in out
        assert "--disabled" in out
        assert "--busy" in out
        assert "--free" in out
        assert "--cloud" in out
        assert "--no-cloud" in out

    def test_unknown_flag_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--bogus"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert err.strip()  # argparse usage error

    def test_cloud_no_cloud_mutex_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--cloud", "hetzner", "--no-cloud"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestShowNodesRendering:
    """Table and JSON rendering with display transformations / raw values."""

    def test_default_lists_all_nodes_table(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        node1 = Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=4,
            enabled=True,
            port=22,
        )
        node2 = Node(
            node_id=NodeId(2),
            hostname="10.0.0.2",
            ncpus=None,
            enabled=False,
            port=2222,
            cloud="hetzner",
        )
        uow.tasks.list_running = AsyncMock(
            return_value=[
                make_task(
                    task_id=1,
                    label="my_job",
                    allocated_node_id=NodeId(1),
                ),
            ],
        )
        uow.nodes.list_all = AsyncMock(return_value=[node1, node2])

        _run([])  # no SystemExit on success

        out, _ = capsys.readouterr()
        lines = out.splitlines()
        # Header
        assert "NODE_ID" in lines[0]
        assert "HOSTNAME" in lines[0]
        assert "PORT" in lines[0]
        assert "NCPUS" in lines[0]
        assert "ENABLED" in lines[0]
        assert "CLOUD" in lines[0]
        assert "TASK_ID" in lines[0]
        assert "LABEL" in lines[0]
        # Two data rows
        assert len(lines) == 3
        # Row 1: NODE_ID=1, busy node, port 22 -> '-', ncpus=4, enabled yes, cloud None -> '-',
        # task_id=1, label=my_job
        assert "1" in lines[1]
        assert "10.0.0.1" in lines[1]
        assert "-" in lines[1]  # port rendered as '-'
        assert "yes" in lines[1]
        assert "my_job" in lines[1]
        # Row 2: free node, port 2222, ncpus=None -> MAX, enabled no, cloud hetzner,
        # task_id/label -> '-'
        assert "10.0.0.2" in lines[2]
        assert "2222" in lines[2]
        assert "MAX" in lines[2]
        assert "no" in lines[2]
        assert "hetzner" in lines[2]

    def test_table_shows_max_for_none_ncpus(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        """Gherkin: yanodes table shows MAX for None ncpus."""
        _config, uow, _deps = stub_config_deps
        node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=None,
            enabled=True,
            port=22,
        )
        uow.tasks.list_running = AsyncMock(return_value=[])
        uow.nodes.list_all = AsyncMock(return_value=[node])

        _run([])

        out, _ = capsys.readouterr()
        lines = out.splitlines()
        assert len(lines) == 2  # header + one data row
        assert "MAX" in lines[1]

    def test_json_output_raw_values(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        import json as _json

        _config, uow, _deps = stub_config_deps
        node1 = Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=None,
            enabled=True,
            port=22,
            cloud=None,
        )
        node2 = Node(
            node_id=NodeId(2),
            hostname="10.0.0.2",
            ncpus=4,
            enabled=False,
            port=2222,
            cloud="hetzner",
        )
        uow.tasks.list_running = AsyncMock(
            return_value=[
                make_task(task_id=7, label="job7", allocated_node_id=NodeId(1)),
            ],
        )
        uow.nodes.list_all = AsyncMock(return_value=[node1, node2])

        _run(["--json"])

        out, _ = capsys.readouterr()
        data = _json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 2
        assert next(iter(data[0])) == "node_id"
        assert data[0]["node_id"] == 1
        # Busy node: raw port=22 (not "-" or null), ncpus=None (null in JSON), enabled bool,
        # cloud null, occupied_by object.
        assert data[0]["hostname"] == "10.0.0.1"
        assert data[0]["port"] == 22
        assert data[0]["ncpus"] is None
        assert data[0]["enabled"] is True
        assert data[0]["cloud"] is None
        assert data[0]["jump_host"] is None
        assert data[0]["jump_port"] == 22
        assert data[0]["jump_username"] == "root"
        assert data[0]["external_id"] is None
        assert data[0]["status"] == "OTHER"
        assert data[0]["created_at"] is not None
        assert data[0]["updated_at"] is not None
        assert data[0]["occupied_by"] == {"task_id": 7, "label": "job7"}
        # Free node: occupied_by null.
        assert next(iter(data[1])) == "node_id"
        assert data[1]["node_id"] == 2
        assert data[1]["hostname"] == "10.0.0.2"
        assert data[1]["port"] == 2222
        assert data[1]["ncpus"] == 4
        assert data[1]["enabled"] is False
        assert data[1]["cloud"] == "hetzner"
        assert data[1]["jump_host"] is None
        assert data[1]["jump_port"] == 22
        assert data[1]["jump_username"] == "root"
        assert data[1]["external_id"] is None
        assert data[1]["status"] == "OTHER"
        assert data[1]["created_at"] is not None
        assert data[1]["updated_at"] is not None
        assert data[1]["occupied_by"] is None

    def test_json_emits_null_ncpus_for_none(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        """Gherkin: yanodes --json emits null ncpus for None."""
        import json as _json

        _config, uow, _deps = stub_config_deps
        node = Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=None,
            enabled=True,
            port=22,
        )
        uow.tasks.list_running = AsyncMock(return_value=[])
        uow.nodes.list_all = AsyncMock(return_value=[node])

        _run(["--json"])

        out, _ = capsys.readouterr()
        data = _json.loads(out)
        assert len(data) == 1
        assert data[0]["ncpus"] is None

    def test_json_empty_is_empty_list(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        import json as _json

        _config, uow, _deps = stub_config_deps
        uow.tasks.list_running = AsyncMock(return_value=[])
        uow.nodes.list_all = AsyncMock(return_value=[])

        _run(["--json"])  # no SystemExit

        out, _ = capsys.readouterr()
        assert _json.loads(out) == []

    def test_empty_nodes_exits_zero(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.tasks.list_running = AsyncMock(return_value=[])
        uow.nodes.list_all = AsyncMock(return_value=[])

        _run([])  # no SystemExit on success

        out, _ = capsys.readouterr()
        lines = out.splitlines()
        # Header only, no data rows.
        assert len(lines) == 1
        assert "NODE_ID" in lines[0]
        assert "HOSTNAME" in lines[0]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _three_mixed_nodes() -> list[Node]:
    """Return nodes varying on enabled/busy/cloud for filter tests."""
    return [
        Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=4,
            enabled=True,
            port=22,
            cloud="hetzner",
        ),
        Node(
            node_id=NodeId(2),
            hostname="10.0.0.2",
            ncpus=4,
            enabled=False,
            port=22,
            cloud="hetzner",
        ),
        Node(
            node_id=NodeId(3),
            hostname="10.0.0.3",
            ncpus=4,
            enabled=True,
            port=22,
            cloud=None,
        ),
        Node(
            node_id=NodeId(4),
            hostname="10.0.0.4",
            ncpus=4,
            enabled=False,
            port=22,
            cloud="exoscale",
        ),
    ]


def _wire(uow: AsyncMock, nodes: list[Node], tasks: list[Task] | None = None) -> None:
    uow.nodes.list_all = AsyncMock(return_value=nodes)
    uow.tasks.list_running = AsyncMock(return_value=tasks or [])


def _hostnames_from_table(out: str) -> list[str]:
    """Extract the HOSTNAME column from a table output (skip header)."""
    lines = out.splitlines()
    hostnames: list[str] = []
    for line in lines[1:]:
        # NODE_ID is column 0, HOSTNAME is column 1; strip and split.
        hostname = line.split()[1]
        hostnames.append(hostname)
    return hostnames


class TestShowNodesFiltering:
    """Filter flag behavior and AND composition."""

    def test_enabled_disabled_equals_default(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        nodes = _three_mixed_nodes()
        _wire(uow, nodes)
        _run(["--enabled", "--disabled"])
        out, _ = capsys.readouterr()
        assert len(out.splitlines()) == len(nodes) + 1  # header + all rows

    def test_busy_free_equals_default(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        nodes = _three_mixed_nodes()
        # One busy node (10.0.0.1), rest free.
        _wire(
            uow,
            nodes,
            [make_task(task_id=1, label="j1", allocated_node_id=NodeId(1))],
        )
        _run(["--busy", "--free"])
        out, _ = capsys.readouterr()
        assert len(out.splitlines()) == len(nodes) + 1

    @pytest.mark.parametrize(
        ("argv", "busy_node", "expected"),
        [
            # 10.0.0.1 is enabled, busy, cloud=hetzner — the only match.
            pytest.param(
                ["--enabled", "--busy", "--cloud", "hetzner"],
                NodeId(1),
                ["10.0.0.1"],
                id="and-composition",
            ),
            pytest.param(["--busy"], NodeId(2), ["10.0.0.2"], id="busy-only"),
            pytest.param(
                ["--free"],
                NodeId(2),
                ["10.0.0.1", "10.0.0.3", "10.0.0.4"],
                id="free-only",
            ),
        ],
    )
    def test_filter_selects_nodes_with_one_busy_task(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        argv: list[str],
        busy_node: NodeId,
        expected: list[str],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        nodes = _three_mixed_nodes()
        _wire(
            uow,
            nodes,
            [make_task(task_id=1, label="j1", allocated_node_id=busy_node)],
        )
        _run(argv)
        out, _ = capsys.readouterr()
        assert _hostnames_from_table(out) == expected

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(["--enabled"], ["10.0.0.1", "10.0.0.3"], id="enabled"),
            pytest.param(["--disabled"], ["10.0.0.2", "10.0.0.4"], id="disabled"),
            pytest.param(["--cloud", "hetzner"], ["10.0.0.1", "10.0.0.2"], id="cloud"),
            pytest.param(["--no-cloud"], ["10.0.0.3"], id="no-cloud"),
        ],
    )
    def test_single_filter_selects_matching_nodes(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
        argv: list[str],
        expected: list[str],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        _wire(uow, _three_mixed_nodes())
        _run(argv)
        out, _ = capsys.readouterr()
        assert _hostnames_from_table(out) == expected


# ---------------------------------------------------------------------------
# Order preservation
# ---------------------------------------------------------------------------


class TestShowNodesOrder:
    """list_all() order preserved (no sorting)."""

    def test_preserves_list_all_order(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        nodes = [
            Node(
                node_id=NodeId(3),
                hostname="10.0.0.3",
                ncpus=4,
                enabled=True,
                port=22,
            ),
            Node(
                node_id=NodeId(1),
                hostname="10.0.0.1",
                ncpus=4,
                enabled=True,
                port=22,
            ),
            Node(
                node_id=NodeId(2),
                hostname="10.0.0.2",
                ncpus=4,
                enabled=True,
                port=22,
            ),
        ]
        _wire(uow, nodes)
        _run([])
        out, _ = capsys.readouterr()
        assert _hostnames_from_table(out) == ["10.0.0.3", "10.0.0.1", "10.0.0.2"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestShowNodesErrors:
    """Exit-code contract: exit 1 on runtime failure."""

    def test_exit_one_on_db_error(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        uow.nodes.list_all = AsyncMock(side_effect=RuntimeError("db down"))
        uow.tasks.list_running = AsyncMock(return_value=[])
        with pytest.raises(SystemExit) as exc:
            _run([])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_exit_one_on_config_error(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            show_nodes_mod,
            "parse_config",
            MagicMock(side_effect=RuntimeError("bad config")),
        )
        with pytest.raises(SystemExit) as exc:
            _run([])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


class TestShowNodesStructure:
    """Structural guard: each source is read once, not re-queried per node."""

    def test_fetch_nodes_view_reads_each_source_once(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_config_deps: tuple[MagicMock, AsyncMock, MagicMock],
    ) -> None:
        _config, uow, _deps = stub_config_deps
        _wire(
            uow,
            [
                Node(
                    node_id=NodeId(1),
                    hostname="10.0.0.1",
                    ncpus=4,
                    enabled=True,
                    port=22,
                ),
            ],
            [make_task(task_id=1, label="j1", allocated_node_id=NodeId(1))],
        )
        _run([])
        uow.tasks.list_running.assert_called_once()
        uow.nodes.list_all.assert_called_once()


# ---------------------------------------------------------------------------
# --config / --log-level scenarios (consolidate-daemon-entrypoints)
# ---------------------------------------------------------------------------


class TestShowNodesConfigLogLevel:
    """--config and --log-level argparse + behavior scenarios (defaults WARNING)."""

    def test_help_lists_config_and_log_level(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "--config" in out
        assert "--log-level" in out

    def test_config_nonexistent_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--config", "/nonexistent.conf"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert "not a file" in err
        assert "/nonexistent.conf" in err

    def test_log_level_warn_rejected_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--log-level", "WARN"])
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
        _wire(uow, [])
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
        monkeypatch.setattr(show_nodes_mod, "parse_config", from_config_spy)
        uow = make_mock_uow()
        _wire(uow, [])
        deps = make_mock_deps(make_mock_config(), uow)
        monkeypatch.setattr(
            show_nodes_mod,
            "make_cli_deps",
            MagicMock(return_value=deps),
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
        _wire(uow, [])
        from_config_spy = MagicMock(return_value=make_mock_config())
        monkeypatch.setattr(show_nodes_mod, "parse_config", from_config_spy)
        fresh_uow = make_mock_uow()
        _wire(fresh_uow, [])
        monkeypatch.setattr(
            show_nodes_mod,
            "make_cli_deps",
            MagicMock(return_value=make_mock_deps(make_mock_config(), fresh_uow)),
        )
        _run([])
        assert str(from_config_spy.call_args.args[0]) == str(CONFIG_FILE)
