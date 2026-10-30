# region MODULE_CONTRACT
# PURPOSE: Unit tests for yasetnode manage_node() host-spec grammar, argparse, exit codes, helpers, and add/remove paths.
# SCOPE: manage_node() host-spec grammar, argparse, exit codes, add/remove paths with mocked Config/CLIDeps/UoW/SSHMachineGateway.
# KEYWORDS: yasetnode, manage_node, host-spec grammar, add/remove
# endregion MODULE_CONTRACT

from __future__ import annotations

import argparse
import importlib
import logging
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, MagicMock

import pytest

from yascheduler.domain import Engine, EngineRepository
from yascheduler.domain.model import (
    Node,
    NodeId,
    Running,
    Task,
    TaskId,
    TaskStatus,
)
from yascheduler.entrypoints.di import CLIDeps

manage_node_mod = importlib.import_module("yascheduler.entrypoints.cli.manage_node")

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Mock helpers (duplicated across the CLI test modules)
# ---------------------------------------------------------------------------


def make_mock_config() -> MagicMock:
    """Return a MagicMock Config with a known g09 engine and remote.username='root'."""
    engine = Engine(name="g09", spawn="run.sh")

    engines = MagicMock(spec=EngineRepository)
    engines.get = MagicMock(return_value=engine)

    config = MagicMock()
    config.engines = engines
    config.clouds = []
    config.remote.username = "root"
    config.remote.engines_dir = PurePosixPath("/opt/engines")
    config.remote.jump_host = None
    config.remote.jump_username = None
    config.remote.jump_port = 22
    config.db = MagicMock()
    return config


def make_mock_uow() -> AsyncMock:
    """Return an AsyncMock UoW wired as its own async context manager."""
    uow = AsyncMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.tasks = AsyncMock()
    uow.nodes = AsyncMock()
    uow.nodes.insert = AsyncMock(
        return_value=Node(
            node_id=NodeId(1),
            hostname="10.0.0.1",
            ncpus=None,
            enabled=False,
            cloud=None,
            username="root",
            port=22,
        ),
    )
    uow.commit = AsyncMock()
    return uow


def make_mock_deps(config: MagicMock, uow: AsyncMock) -> MagicMock:
    """Return a MagicMock CLIDeps wired to the given uow."""
    deps = MagicMock(spec=CLIDeps)
    deps.uow_factory = MagicMock(return_value=uow)
    deps.engines = config.engines
    return deps


def make_mock_repository() -> AsyncMock:
    """Return an AsyncMock SSHMachineRepository with connect/disconnect."""
    repo = AsyncMock()
    repo.connect = AsyncMock()
    repo.disconnect = AsyncMock()
    return repo


@pytest.fixture
def stub_env(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, AsyncMock, MagicMock, AsyncMock]:
    """Patch Config/make_cli_deps/SSHMachineRepository; return (config, uow, deps, repo)."""
    config = make_mock_config()
    uow = make_mock_uow()
    deps = make_mock_deps(config, uow)
    repo = make_mock_repository()
    monkeypatch.setattr(manage_node_mod, "parse_config", MagicMock(return_value=config))
    monkeypatch.setattr(manage_node_mod, "make_cli_deps", MagicMock(return_value=deps))
    monkeypatch.setattr(
        manage_node_mod,
        "SSHMachineRepository",
        MagicMock(return_value=repo),
    )
    return config, uow, deps, repo


def _run(argv: list[str]) -> None:
    """Invoke manage_node(argv); raise if it swallows SystemExit unexpectedly."""
    manage_node_mod.manage_node(argv)


# ---------------------------------------------------------------------------
# _parse_host_spec grammar
# ---------------------------------------------------------------------------


class TestHostSpecParsing:
    """_parse_host_spec: IPv4, user@, :port, ~ncpus, IPv6 brackets, rejections, defaults."""

    @pytest.mark.parametrize(
        ("spec_str", "expected"),
        [
            pytest.param("10.0.0.1", ("10.0.0.1", None, 22, None), id="ipv4"),
            pytest.param(
                "deploy@10.0.0.1", ("10.0.0.1", "deploy", 22, None), id="user-at-host"
            ),
            pytest.param("10.0.0.1:2222", ("10.0.0.1", None, 2222, None), id="port"),
            pytest.param("10.0.0.1~4", ("10.0.0.1", None, 22, 4), id="ncpus"),
            pytest.param(
                "deploy@10.0.0.1:2222~4",
                ("10.0.0.1", "deploy", 2222, 4),
                id="full-spec",
            ),
            pytest.param("[::1]", ("::1", None, 22, None), id="bracketed-ipv6"),
            pytest.param(
                "[fe80::1]:2222",
                ("fe80::1", None, 2222, None),
                id="bracketed-ipv6-with-port",
            ),
            pytest.param(
                "compute-node-7",
                ("compute-node-7", None, 22, None),
                id="hostname",
            ),
        ],
    )
    def test_host_spec_parsed(
        self,
        spec_str: str,
        expected: tuple[str, str | None, int, int | None],
    ) -> None:
        """Rows compare the full (host, username, port, ncpus) tuple."""
        spec = manage_node_mod._parse_host_spec(spec_str)
        assert (spec.host, spec.username, spec.port, spec.ncpus) == expected

    def test_tilde_zero_maps_to_none(self) -> None:
        spec = manage_node_mod._parse_host_spec("10.0.0.1~0")
        assert spec.ncpus is None

    @pytest.mark.parametrize(
        "bad_spec",
        [
            pytest.param("::1", id="unbracketed-ipv6"),
            pytest.param("a@b@c", id="multiple-at-signs"),
            pytest.param("10.0.0.1~4~5", id="multiple-tildes"),
            pytest.param("10.0.0.1:", id="empty-port"),
            pytest.param("10.0.0.1:99999", id="port-out-of-range"),
            pytest.param("10.0.0.1:0", id="port-zero"),
            pytest.param("10.0.0.1~-5", id="negative-ncpus"),
            pytest.param("10.0.0.1:abc", id="non-integer-port"),
            pytest.param("", id="empty-string"),
            pytest.param("@10.0.0.1", id="empty-user"),
            pytest.param("10.0.0.1~", id="empty-ncpus"),
        ],
    )
    def test_malformed_spec_rejected(self, bad_spec: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            manage_node_mod._parse_host_spec(bad_spec)

    def test_default_port_22_when_absent(self) -> None:
        spec = manage_node_mod._parse_host_spec("10.0.0.1")
        assert spec.port == 22

    def test_default_username_none_when_absent(self) -> None:
        spec = manage_node_mod._parse_host_spec("10.0.0.1")
        assert spec.username is None


# ---------------------------------------------------------------------------
# argparse behavior
# ---------------------------------------------------------------------------


class TestManageNodeArgparse:
    """argparse: prog, --help, missing host, unknown flag, mutex, skip-setup × remove, value form."""

    def test_help_shows_prog_yasetnode(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["--help"])
        assert exc.value.code == 0
        out, _ = capsys.readouterr()
        assert "usage: yasetnode" in out
        assert "host" in out
        assert "--skip-setup" in out
        assert "--remove-soft" in out
        assert "--remove-hard" in out

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="missing-host"),
            pytest.param(["10.0.0.1", "--bogus"], id="unknown-flag"),
            pytest.param(
                ["10.0.0.1", "--remove-soft", "--remove-hard"],
                id="remove-mutex",
            ),
            pytest.param(
                ["10.0.0.1", "--skip-setup", "--remove-hard"],
                id="skip-setup-with-remove-hard",
            ),
            pytest.param(
                ["10.0.0.1", "--skip-setup", "--remove-soft"],
                id="skip-setup-with-remove-soft",
            ),
        ],
    )
    def test_invalid_argv_exits_two(
        self,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(argv)
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert err.strip()

    def test_skip_setup_does_not_accept_value(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # store_true flag takes no value; "true" is treated as an extra positional → exit 2.
        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1", "--skip-setup", "true"])
        assert exc.value.code == 2

    def test_argv_injection_no_sys_argv_patch(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        # Deliberately set sys.argv to something unrelated to prove argv wins.
        monkeypatch.setattr("sys.argv", ["python", "-c", "unrelated"])
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        _run(["10.0.0.1"])  # add path with mocked env
        out, _ = capsys.readouterr()
        assert "Added host" in out

    def test_unbracketed_ipv6_rejected_at_argparse(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["::1"])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Add path
# ---------------------------------------------------------------------------


class TestManageNodeAddPath:
    """add happy path, --skip-setup, resource-leak fix, already-in-DB, Node construction."""

    def test_add_happy_path(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        session = repo.connect.return_value

        _run(["[IP]"])  # no SystemExit on success

        repo.connect.assert_called_once()
        session.setup_node.assert_called_once()
        setup_call_args = session.setup_node.call_args.args
        assert len(setup_call_args) == 1
        assert setup_call_args[0] is _config.engines
        repo.disconnect.assert_called_once_with(NodeId(1))
        uow.nodes.insert.assert_called_once()
        assert uow.commit.call_count == 2
        out, _ = capsys.readouterr()
        assert "Setup host..." in out
        assert "Added host to yascheduler: IP:22" in out

    def test_add_with_skip_setup(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        session = repo.connect.return_value

        _run(["[IP]", "--skip-setup"])

        repo.connect.assert_called_once()
        session.setup_node.assert_not_called()
        repo.disconnect.assert_called_once_with(NodeId(1))
        assert uow.nodes.insert.call_count == 1
        assert uow.commit.call_count == 2
        out, _ = capsys.readouterr()
        assert "Setup host..." not in out
        assert "Added host to yascheduler: IP:22" in out

    def test_add_disconnects_when_setup_raises(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        session = repo.connect.return_value
        session.setup_node = AsyncMock(side_effect=RuntimeError("setup boom"))

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1"])
        assert exc.value.code == 1
        # Resource-leak fix: disconnect ran even though setup raised.
        repo.connect.assert_called_once()
        repo.disconnect.assert_called_once_with(NodeId(1))
        # tmp insert runs before connect (to obtain node_id); it persists even if setup fails.
        uow.nodes.insert.assert_called_once()
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_add_already_in_db_exits_one(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, repo = stub_env
        uow.nodes.list_all = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="IP", ncpus=4, enabled=True),
            ],
        )

        with pytest.raises(SystemExit) as exc:
            _run(["[IP]"])
        assert exc.value.code == 1
        uow.nodes.insert.assert_not_called()
        repo.connect.assert_not_called()
        out, err = capsys.readouterr()
        assert out == ""
        assert "already in DB" in err

    def test_node_uses_config_username_when_no_override(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        config, uow, _deps, _repo = stub_env
        config.remote.username = "opsuser"
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["deploy@10.0.0.1"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.username == "deploy"

    def test_node_default_ncpus_none_when_absent(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["10.0.0.1"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.ncpus is None
        assert added_node.hostname == "10.0.0.1"
        assert added_node.port == 22
        assert (
            added_node.enabled is False
        )  # tmp insert; enabled=True comes via later update

    def test_node_ncpus_when_explicit(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["10.0.0.1~4"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.ncpus == 4

    def test_node_construction_uses_port(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["10.0.0.1:2222"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.port == 2222
        out, _ = capsys.readouterr()
        assert "Added host to yascheduler: 10.0.0.1:2222" in out

    def test_add_stamps_jump_from_config_remote(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """Gherkin: yasetnode add-path stamps jump from config.remote before insert.
        Domain: Static node stamps jump from remote defaults at creation.
        """
        config, uow, _deps, repo = stub_env
        config.remote.jump_host = "bastion.example.com"
        config.remote.jump_username = "jumper"
        config.remote.jump_port = 2222
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["[IP]"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.jump_host == "bastion.example.com"
        assert added_node.jump_username == "jumper"
        assert added_node.jump_port == 2222
        # connect receives no jump_host/jump_username kwargs
        repo.connect.assert_called_once()
        connect_kwargs = repo.connect.call_args.kwargs
        assert "jump_host" not in connect_kwargs
        assert "jump_username" not in connect_kwargs

    def test_add_jump_host_none_when_not_configured(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """When config.remote.jump_host is None, NewNode.jump_host is None."""
        config, uow, _deps, _repo = stub_env
        config.remote.jump_host = None
        config.remote.jump_username = None
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["[IP]"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.jump_host is None

    def test_add_uses_default_jump_port_when_key_absent(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """Gherkin: yasetnode add-path uses default jump_port when [remote] key absent."""
        config, uow, _deps, _repo = stub_env
        config.remote.jump_port = 22
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["[IP]"])

        added_node = uow.nodes.insert.call_args[0][0]
        assert added_node.jump_port == 22

    def test_add_loopback_conflicting_data_dir_warns(
        self,
        caplog: pytest.LogCaptureFixture,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """Issue #115: loopback host + [local].data_dir == [remote].data_dir warns."""
        config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        shared = "/tmp/yasched-shared"
        config.local.data_dir = Path(shared)
        config.remote.data_dir = PurePosixPath(shared)

        with caplog.at_level(
            logging.WARNING, logger="yascheduler.entrypoints.cli.manage_node"
        ):
            _run(["127.0.0.1"])

        assert any(
            "data_dir" in r.getMessage() and "collide" in r.getMessage()
            for r in caplog.records
        ), (
            f"expected data_dir collision warning, got {[r.getMessage() for r in caplog.records]}"
        )

    def test_add_loopback_localhost_alias_warns(
        self,
        caplog: pytest.LogCaptureFixture,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """Issue #115: 'localhost' alias is treated as loopback too."""
        config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        shared = "/tmp/yasched-shared"
        config.local.data_dir = Path(shared)
        config.remote.data_dir = PurePosixPath(shared)

        with caplog.at_level(
            logging.WARNING, logger="yascheduler.entrypoints.cli.manage_node"
        ):
            _run(["localhost"])

        assert any("collide" in r.getMessage() for r in caplog.records), (
            f"expected collision warning for localhost, got {[r.getMessage() for r in caplog.records]}"
        )

    @pytest.mark.parametrize(
        ("host", "local_data_dir", "remote_data_dir"),
        [
            pytest.param(
                "10.0.0.1",
                "/tmp/yasched-shared",
                "/tmp/yasched-shared",
                id="non-loopback-matching-dirs",
            ),
            pytest.param(
                "127.0.0.1",
                "/tmp/yasched-local",
                "/tmp/yasched-remote",
                id="loopback-distinct-dirs",
            ),
        ],
    )
    def test_no_data_dir_collision_warning(
        self,
        caplog: pytest.LogCaptureFixture,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
        host: str,
        local_data_dir: str,
        remote_data_dir: str,
    ) -> None:
        config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        config.local.data_dir = Path(local_data_dir)
        config.remote.data_dir = PurePosixPath(remote_data_dir)

        with caplog.at_level(
            logging.WARNING, logger="yascheduler.entrypoints.cli.manage_node"
        ):
            _run([host])

        assert not any("collide" in r.getMessage() for r in caplog.records), (
            f"unexpected collision warning for {host}, "
            f"got {[r.getMessage() for r in caplog.records]}"
        )

    def test_add_constructs_repository_once(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """Gherkin: yasetnode constructs repository once and passes to add helper."""
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["[IP]"])

        manage_node_mod.SSHMachineRepository.assert_called_once_with()

    def test_add_removes_tmp_row_on_connect_failure(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """Gherkin: yasetnode add-path rolls back tmp row on connect failure."""
        _config, uow, _deps, repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        repo.connect = AsyncMock(side_effect=RuntimeError("connection refused"))

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1"])

        assert exc.value.code == 1
        # tmp row was removed best-effort after connect failure
        uow.nodes.remove.assert_called_once_with(NodeId(1))
        # two commits: one for the insert UoW, one for the remove UoW
        assert uow.commit.call_count == 2
        uow.nodes.insert.assert_called_once()
        _, err = capsys.readouterr()
        assert "connection refused" in err


# ---------------------------------------------------------------------------
# Remove path
# ---------------------------------------------------------------------------


class TestManageNodeRemovePath:
    """remove-hard, remove-soft with/without tasks, nonexistent, prints-after-commit."""

    def test_remove_hard_happy_path(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, repo = stub_env
        uow.nodes.list_all = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True),
            ],
        )
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[1, 2])

        def _fake_get(task_id: TaskId) -> Task | None:
            # Return a RUNNING task bound to node 1 — the hard-remove path
            # load→abandon→save loop reads the allocation from state.
            # The list_ids_by_node_id_and_status mock returns bare ints, so
            # accept both TaskId and int here.
            raw = task_id.value if isinstance(task_id, TaskId) else task_id
            tid = TaskId(int(raw))  # type: ignore[arg-type]
            return Task(
                task_id=tid,
                label=f"task-{tid.value}",
                engine="fleur",
                state=Running(allocated_node_id=NodeId(1), remote_folder="/r"),
            )

        uow.tasks.get_running = AsyncMock(side_effect=_fake_get)
        uow.tasks.save = AsyncMock()

        _run(["10.0.0.1", "--remove-hard"])

        # Each RUNNING task was loaded, abandoned (RUNNING→DONE with
        # TaskAbandoned), and saved — replacing the old update_status flip.
        assert uow.tasks.get_running.await_count == 2
        assert uow.tasks.save.await_count == 2
        # Each saved task reached DONE via abandon (carries error + event).
        for save_call in uow.tasks.save.await_args_list:
            saved = save_call.args[0]
            assert saved.status is TaskStatus.DONE
            assert saved.state.error == "node is gone"
            assert any(
                evt.__class__.__name__ == "TaskAbandoned" for evt in saved.events
            )
        uow.nodes.remove.assert_called_once_with(NodeId(1))
        uow.commit.assert_called_once()
        # add path not triggered
        repo.connect.assert_not_called()
        out, _ = capsys.readouterr()
        assert "An associated task 1 at 10.0.0.1 is now marked done!" in out
        assert "An associated task 2 at 10.0.0.1 is now marked done!" in out
        assert "Removed host from yascheduler: 10.0.0.1" in out

    def test_remove_hard_skips_task_that_raced_to_done(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """A task that transitioned to DONE between list and load is skipped, not abandoned.

        The list_ids_by_node_id_and_status snapshot matched RUNNING, but by the
        time get_running() loads the row a concurrent daemon or operator run
        already finalized it. get_running() returns None (absent OR wrong
        status), collapsing the post-list race into a None-skip — no
        isinstance guard, no abandon() on a terminal task. The hard-remove
        transaction still removes the node.
        """
        _config, uow, _deps, _repo = stub_env
        uow.nodes.list_all = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True),
            ],
        )
        # List returned the id (RUNNING at snapshot time), but get_running()
        # now returns None — the row transitioned to DONE in the race window.
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[1])
        uow.tasks.get_running = AsyncMock(return_value=None)
        uow.tasks.save = AsyncMock()

        _run(["10.0.0.1", "--remove-hard"])

        # The raced task was loaded (None) but NOT abandoned/saved (terminal).
        uow.tasks.get_running.assert_called_once_with(1)
        uow.tasks.save.assert_not_called()
        # The node was still removed; the transaction did not abort.
        uow.nodes.remove.assert_called_once_with(NodeId(1))
        uow.commit.assert_called_once()
        out, _ = capsys.readouterr()
        assert "An associated task 1 at 10.0.0.1 is now marked done!" in out
        assert "Removed host from yascheduler: 10.0.0.1" in out

    def test_remove_soft_with_tasks_disables(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.list_all = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True),
            ],
        )
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[1])

        _run(["10.0.0.1", "--remove-soft"])

        uow.nodes.disable.assert_called_once_with(NodeId(1))
        uow.nodes.remove.assert_not_called()
        uow.commit.assert_called_once()
        out, _ = capsys.readouterr()
        assert "A task associated, prevent from assigning the new tasks" in out
        assert "Prevented from assigning the new tasks: 10.0.0.1" in out

    def test_remove_soft_without_tasks_removes(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.list_all = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True),
            ],
        )
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[])

        _run(["10.0.0.1", "--remove-soft"])

        uow.nodes.remove.assert_called_once_with(NodeId(1))
        uow.nodes.disable.assert_not_called()
        uow.commit.assert_called_once()
        out, _ = capsys.readouterr()
        assert "No tasks associated, remove node immediately" in out
        assert "Removed host from yascheduler: 10.0.0.1" in out

    def test_remove_nonexistent_exits_one(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1", "--remove-hard"])
        assert exc.value.code == 1
        uow.nodes.remove.assert_not_called()
        out, err = capsys.readouterr()
        assert out == ""
        assert "NOT in DB" in err

    def test_remove_hard_prints_after_commit(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.list_all = AsyncMock(
            return_value=[
                Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True),
            ],
        )
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[1])
        uow.tasks.get_running = AsyncMock(
            return_value=Task(
                task_id=TaskId(1),
                label="t",
                engine="fleur",
                state=Running(allocated_node_id=NodeId(1), remote_folder="/r"),
            )
        )
        uow.tasks.save = AsyncMock()
        # Failing commit proves the success prints live AFTER commit, not before.
        uow.commit = AsyncMock(side_effect=RuntimeError("commit failed"))

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1", "--remove-hard"])
        assert exc.value.code == 1
        out, _ = capsys.readouterr()
        assert "now marked done" not in out
        assert "Removed host" not in out
        # The load→abandon→save and remove ran before commit; only the prints
        # are gated.
        uow.tasks.get_running.assert_called_once_with(1)
        uow.tasks.save.assert_called_once()
        uow.nodes.remove.assert_called_once_with(NodeId(1))

    def test_remove_by_host_resolves_node_and_passes_node_to_helper(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        """[9.4] remove-by-host: list_all() resolves Node by ip; helper receives Node, not str."""
        _config, uow, _deps, _repo = stub_env
        resolved = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4, enabled=True)
        uow.nodes.list_all = AsyncMock(return_value=[resolved])
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[])

        _run(["10.0.0.1", "--remove-soft"])

        # The validation UoW resolved the Node via list_all + ip filter.
        uow.nodes.list_all.assert_awaited_once()
        # The mutator received node.node_id (NodeId), not the ip string.
        uow.nodes.remove.assert_called_once_with(NodeId(1))


# ---------------------------------------------------------------------------
# Exit codes and output channels
# ---------------------------------------------------------------------------


class TestManageNodeExitCodesAndChannels:
    """0 success, 1 SSH/DB/config failure, stderr Error:, logging setup."""

    def test_add_success_exits_zero(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)

        _run(["10.0.0.1"])  # no SystemExit raised on success → implicit exit 0

    def test_ssh_failure_exits_one(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        repo.connect = AsyncMock(side_effect=RuntimeError("ssh down"))

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1"])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_db_error_exits_one(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        uow.nodes.insert = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1"])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_config_error_exits_one(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            manage_node_mod,
            "parse_config",
            MagicMock(side_effect=RuntimeError("bad config")),
        )

        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1"])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "Error:" in err

    def test_failure_messages_on_stderr_not_stdout(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        uow.nodes.insert = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(SystemExit):
            _run(["10.0.0.1"])
        out, err = capsys.readouterr()
        assert "Added host" not in out
        assert "Error:" in err

    def test_logging_captures_warnings(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        capture_spy = MagicMock()
        monkeypatch.setattr(logging, "captureWarnings", capture_spy)
        root = logging.getLogger()
        original_level = root.level
        try:
            _config, uow, _deps, _repo = stub_env
            uow.nodes.get = AsyncMock(return_value=None)

            _run(["10.0.0.1"])

            capture_spy.assert_called_once_with(True)
            assert root.level == logging.WARNING
        finally:
            # Restore process-global root logger level (avoid test-order side effects).
            root.setLevel(original_level)


# ---------------------------------------------------------------------------
# --config / --log-level scenarios (consolidate-daemon-entrypoints)
# ---------------------------------------------------------------------------


class TestManageNodeConfigLogLevel:
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
            _run(["10.0.0.1", "--config", "/nonexistent.conf"])
        assert exc.value.code == 2
        _, err = capsys.readouterr()
        assert "not a file" in err
        assert "/nonexistent.conf" in err

    def test_log_level_warn_rejected_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["10.0.0.1", "--log-level", "WARN"])
        assert exc.value.code == 2

    def test_log_level_debug_sets_root_to_debug(
        self,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging

        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        root = logging.getLogger()
        original_level = root.level
        try:
            _run(["10.0.0.1", "--log-level", "DEBUG"])
            assert root.level == logging.DEBUG
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
        monkeypatch.setattr(manage_node_mod, "parse_config", from_config_spy)
        uow = make_mock_uow()
        uow.nodes.get = AsyncMock(return_value=None)
        monkeypatch.setattr(
            manage_node_mod,
            "make_cli_deps",
            MagicMock(return_value=make_mock_deps(make_mock_config(), uow)),
        )
        monkeypatch.setattr(
            manage_node_mod,
            "SSHMachineRepository",
            MagicMock(return_value=make_mock_repository()),
        )

        _run(["10.0.0.1", "--config", str(custom_conf)])
        from_config_spy.assert_called_once_with(custom_conf)

    def test_default_config_is_config_file(
        self,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from yascheduler import CONFIG_FILE

        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        from_config_spy = MagicMock(return_value=make_mock_config())
        monkeypatch.setattr(manage_node_mod, "parse_config", from_config_spy)
        _run(["10.0.0.1"])
        assert str(from_config_spy.call_args.args[0]) == str(CONFIG_FILE)

    def test_default_log_level_is_warning(
        self,
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging

        _config, uow, _deps, _repo = stub_env
        uow.nodes.get = AsyncMock(return_value=None)
        root = logging.getLogger()
        original_level = root.level
        try:
            _run(["10.0.0.1"])
            assert root.level == logging.WARNING
        finally:
            root.setLevel(original_level)


# ---------------------------------------------------------------------------
# _parse_node_target (add-node-id-identity)
# ---------------------------------------------------------------------------


class TestParseNodeTarget:
    """_parse_node_target: digit→NodeId, non-digit→HostSpec, zero→ValueError, negative→host_spec."""

    def test_digit_returns_node_id_target(self) -> None:
        nt = manage_node_mod._parse_node_target("5")
        assert nt == manage_node_mod.NodeTarget(node_id=NodeId(5), host_spec=None)

    def test_non_digit_returns_host_spec_target(self) -> None:
        nt = manage_node_mod._parse_node_target("10.0.0.1")
        assert nt.node_id is None
        assert nt.host_spec == manage_node_mod.HostSpec(
            host="10.0.0.1",
            username=None,
            port=22,
            ncpus=None,
        )

    def test_zero_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="NodeId must be > 0"):
            manage_node_mod._parse_node_target("0")

    def test_negative_falls_through_to_host_spec(self) -> None:
        nt = manage_node_mod._parse_node_target("-5")
        assert nt.node_id is None
        assert nt.host_spec.host == "-5"


# ---------------------------------------------------------------------------
# node_id remove path (add-node-id-identity)
# ---------------------------------------------------------------------------


class TestManageNodeIdPath:
    """add-by-id→exit2, remove-by-id resolves via get_by_id, unknown id→exit1."""

    def test_add_by_id_exits_two(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _run(["5"])
        assert exc.value.code == 2

    def test_remove_by_id_soft_resolves_via_get_by_id(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get_by_id = AsyncMock(
            return_value=Node(
                node_id=NodeId(5),
                hostname="10.0.0.5",
                ncpus=4,
                enabled=True,
            ),
        )
        uow.tasks.list_ids_by_node_id_and_status = AsyncMock(return_value=[])

        _run(["5", "--remove-soft"])

        uow.nodes.get_by_id.assert_awaited_once_with(NodeId(5))
        uow.nodes.remove.assert_called_once_with(NodeId(5))
        out, _ = capsys.readouterr()
        assert "Removed host from yascheduler: 10.0.0.5" in out

    def test_remove_by_id_unknown_exits_one(
        self,
        capsys: pytest.CaptureFixture[str],
        stub_env: tuple[MagicMock, AsyncMock, MagicMock, AsyncMock],
    ) -> None:
        _config, uow, _deps, _repo = stub_env
        uow.nodes.get_by_id = AsyncMock(return_value=None)

        with pytest.raises(SystemExit) as exc:
            _run(["999", "--remove-hard"])
        assert exc.value.code == 1
        _, err = capsys.readouterr()
        assert "not in DB" in err
        assert "999" in err
