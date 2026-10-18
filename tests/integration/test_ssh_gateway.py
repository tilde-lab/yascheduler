# region MODULE_CONTRACT
# PURPOSE: Integration tests for SSHMachineRepository + SSHMachineOperations against a Docker SSH server via testcontainers.
# SCOPE: Connection lifecycle, command execution, SFTP upload/download, machine state transitions.
# DEPENDENCIES: USES API: testcontainers (Docker SSH server)
# KEYWORDS: SSHMachineRepository, SFTP, Docker SSH server, machine state
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import asyncssh
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from yascheduler.domain import Engine
from yascheduler.domain.model import MachineState, Node, NodeId
from yascheduler.infra.ssh.operations import (
    OccupancyChecker,
    OutputDownloader,
    TaskDeployer,
)
from yascheduler.infra.ssh.repository import SSHMachineRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from yascheduler.domain import MachineSession
    from yascheduler.infra.ssh.session import SSHMachineSession


@pytest.fixture(scope="session")
async def ssh_container(tmp_path_factory: Any) -> AsyncGenerator[dict[str, Any], None]:
    """Start Docker SSH container, generate key pair, yield connection info."""
    key_dir = tmp_path_factory.mktemp("ssh_keys")
    key_path = key_dir / "id_rsa"

    key = asyncssh.generate_private_key("ssh-rsa")
    public_key_str = key.export_public_key("openssh").decode().strip()
    key.write_private_key(str(key_path))

    container = DockerContainer("serversideup/docker-ssh")
    container.with_env("SSH_USER", "testuser")
    container.with_env("AUTHORIZED_KEYS", public_key_str)
    container.with_env("ALLOWED_IPS", "AllowUsers testuser")
    container.with_exposed_ports(2222)
    container.waiting_for(LogMessageWaitStrategy("Server listening on"))

    container.start()
    try:
        await asyncio.sleep(1)

        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(2222))

        yield {
            "host": host,
            "port": port,
            "username": "testuser",
            "key_path": PurePosixPath(str(key_path)),
        }
    finally:
        container.stop()


@pytest.fixture(scope="session")
async def ssh_container_2(
    tmp_path_factory: Any,
) -> AsyncGenerator[dict[str, Any], None]:
    """Start a second Docker SSH container for multi-machine regression tests.

    Yields ``127.0.0.2`` plus the container's published SSH port, so the
    gateway sees a genuinely distinct endpoint from the first container.
    ``get_container_host_ip`` returns ``localhost`` for both testcontainers,
    which would collide with ``ssh_container`` because SSHMachineRepository
    keys machines by IP only. Published ports listen on the whole
    127.0.0.0/8 range on Docker and rootless podman; container bridge IPs
    are avoided because on rootless podman the bridge lives in a dedicated
    network namespace unreachable from the host.
    """
    key_dir = tmp_path_factory.mktemp("ssh_keys_2")
    key_path = key_dir / "id_rsa"

    key = asyncssh.generate_private_key("ssh-rsa")
    public_key_str = key.export_public_key("openssh").decode().strip()
    key.write_private_key(str(key_path))

    container = DockerContainer("serversideup/docker-ssh")
    container.with_env("SSH_USER", "testuser")
    container.with_env("AUTHORIZED_KEYS", public_key_str)
    container.with_env("ALLOWED_IPS", "AllowUsers testuser")
    container.with_exposed_ports(2222)
    container.waiting_for(LogMessageWaitStrategy("Server listening on"))

    container.start()
    try:
        await asyncio.sleep(1)

        yield {
            "host": "127.0.0.2",
            "port": int(container.get_exposed_port(2222)),
            "username": "testuser",
            "key_path": PurePosixPath(str(key_path)),
        }
    finally:
        container.stop()


@pytest.fixture
async def repository(
    ssh_container: dict[str, Any],  # type: ignore[type-arg]
) -> AsyncGenerator[SSHMachineRepository, None]:
    """Create SSHMachineRepository connected to test container."""
    repo = SSHMachineRepository()
    await repo.connect(
        node=_container_node(1, ssh_container),
        client_keys=[ssh_container["key_path"]],
    )
    yield repo
    await repo.disconnect_all()


@pytest.fixture
def task_deployer() -> TaskDeployer:
    """Create TaskDeployer collaborator."""
    return TaskDeployer()


@pytest.fixture
def output_downloader() -> OutputDownloader:
    """Create OutputDownloader collaborator."""
    return OutputDownloader()


@pytest.fixture
def occupancy_checker() -> OccupancyChecker:
    """Create OccupancyChecker collaborator."""
    return OccupancyChecker()


class TestSSHGatewayIntegration:
    """Integration tests for SSHMachineRepository + SSHMachineOperations against real Docker SSH server."""

    async def _get_session(self, repository: SSHMachineRepository) -> MachineSession:
        sessions = repository.list_free(None)
        assert len(sessions) > 0
        return sessions[0]

    async def test_connect_returns_session(
        self,
        repository: SSHMachineRepository,
        ssh_container: dict[str, Any],  # type: ignore[type-arg]
    ) -> None:
        """connect() returns a session with correct ip, platform, and FREE state."""
        session = await self._get_session(repository)
        assert session.hostname == ssh_container["host"]
        assert "linux" in session.machine.platforms
        assert session.machine.state == MachineState.FREE
        # ncpus was dropped from ConnectedMachine in connected-machine-runtime-only.
        # A caplog assertion for [SSHRepository][connect][CPUS] is not feasible here
        # because connect() runs in the repository fixture before this test body starts,
        # so caplog would not have captured it regardless of set_level timing.

    async def test_run_echo(self, repository: SSHMachineRepository) -> None:
        """session.run() executes command and returns output."""
        session = await self._get_session(repository)
        result = await session.run("echo hello_world")
        assert result.exit_code == 0
        assert "hello_world" in result.stdout

    async def test_run_stderr(self, repository: SSHMachineRepository) -> None:
        """session.run() captures stderr."""
        session = await self._get_session(repository)
        result = await session.run("echo error_msg >&2")
        assert "error_msg" in result.stderr

    async def test_run_exit_code_nonzero(
        self,
        repository: SSHMachineRepository,
    ) -> None:
        """session.run() returns non-zero exit code on failure."""
        session = await self._get_session(repository)
        result = await session.run("exit 42")
        assert result.exit_code == 42

    async def test_run_multiline_output(self, repository: SSHMachineRepository) -> None:
        """session.run() handles multiline stdout."""
        session = await self._get_session(repository)
        result = await session.run("echo line1; echo line2")
        assert "line1" in result.stdout
        assert "line2" in result.stdout

    async def test_upload_download_roundtrip(
        self,
        repository: SSHMachineRepository,
        ssh_container: dict[str, Any],  # type: ignore[type-arg]
        tmp_path: Path,
    ) -> None:
        """Upload then download returns original content."""
        session = await self._get_session(repository)

        local_upload = tmp_path / "upload.txt"
        local_upload.write_text("test content")

        remote = "/tmp/test_upload.txt"
        await session.upload(local_upload, remote)

        local_download = tmp_path / "downloaded.txt"
        async with session.open_sftp() as sftp:
            await sftp.get(remote, str(local_download))

        assert local_download.read_text() == "test content"

    async def test_list_free_after_connect(
        self,
        repository: SSHMachineRepository,
    ) -> None:
        """Connected machine appears in list_free with matching platform."""
        sessions_all = repository.list_free(None)
        assert len(sessions_all) >= 1
        assert "linux" in sessions_all[0].machine.platforms

        sessions_linux = repository.list_free(["linux"])
        assert len(sessions_linux) >= 1

        sessions_windows = repository.list_free(["windows"])
        assert len(sessions_windows) == 0

    async def test_list_free_excludes_busy(
        self,
        repository: SSHMachineRepository,
    ) -> None:
        """list_free excludes BUSY machines."""
        session = await self._get_session(repository)
        session.occupy()

        assert len(repository.list_free(None)) == 0

    async def test_disconnect_removes_machine(
        self,
        ssh_container: dict[str, Any],  # type: ignore[type-arg]
    ) -> None:
        """disconnect() removes machine from repository registry."""
        repo = SSHMachineRepository()
        await repo.connect(
            node=_container_node(2, ssh_container),
            client_keys=[ssh_container["key_path"]],
        )
        assert NodeId(2) in repo

        await repo.disconnect(NodeId(2))
        assert NodeId(2) not in repo

    async def test_run_multiple_commands(
        self,
        repository: SSHMachineRepository,
    ) -> None:
        """Multiple sequential run() calls work on same connection."""
        session = await self._get_session(repository)

        r1 = await session.run("echo first")
        assert "first" in r1.stdout
        assert r1.exit_code == 0

        r2 = await session.run("echo second")
        assert "second" in r2.stdout
        assert r2.exit_code == 0

        r3 = await session.run("echo third")
        assert "third" in r3.stdout
        assert r3.exit_code == 0

    async def test_connect_with_env_variable(
        self,
        repository: SSHMachineRepository,
    ) -> None:
        """Run command that reads env variable to verify shell works."""
        session = await self._get_session(repository)
        result = await session.run("echo $HOME")
        assert result.exit_code == 0
        assert result.stdout.strip() != ""

    async def test_upload_and_check_via_run(
        self,
        repository: SSHMachineRepository,
        tmp_path: Path,
    ) -> None:
        """Upload a file, then run cat to verify content remotely."""
        session = await self._get_session(repository)

        local = tmp_path / "verify.txt"
        local.write_text("verify me")

        remote = "/tmp/verify_test.txt"
        await session.upload(local, remote)

        result = await session.run("cat /tmp/verify_test.txt")
        assert "verify me" in result.stdout

    async def test_disconnect_all(
        self,
        ssh_container: dict[str, Any],  # type: ignore[type-arg]
    ) -> None:
        """disconnect_all() removes all machines."""
        repo = SSHMachineRepository()
        await repo.connect(
            node=_container_node(3, ssh_container),
            client_keys=[ssh_container["key_path"]],
        )
        assert len(repo) > 0

        await repo.disconnect_all()
        assert len(repo) == 0


class TestOccupancyRunBgLeak:
    """Reproduce bug: run_bg process killed when SSHClientProcess is not stored."""

    async def _get_session(self, repository: SSHMachineRepository) -> MachineSession:
        sessions = repository.list_free(None)
        assert len(sessions) > 0
        return sessions[0]

    async def test_run_bg_process_survives_without_handle(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """run_bg process must survive even when returned handle is discarded.

        This reproduces the real-world bug: Orchestrator._exec_spawn_command
        calls `await session.run_bg(cmd)` without storing the SSHClientProcess.
        When the handle is GC'd, the SSH channel closes and kills the remote
        process. The occupancy check then finds no process and marks the
        machine free, even though the task should still be running.
        """
        engine = _make_pengine(check_pname="sleep")
        session = await self._get_session(repository)

        # Simulate what _exec_spawn_command does: run_bg without storing result
        await session.run_bg("sleep 60")
        # (Handle intentionally not stored — simulating not storing it)
        import gc

        gc.collect()

        # Wait for the sleep_interval the daemon would use
        await asyncio.sleep(1.5)

        # The sleep process must still be running
        result = await occupancy_checker.occupancy_check(session, engine)
        assert result is True, "sleep 60 process was killed after handle was discarded"

        # Cleanup
        await session.run("killall sleep 2>/dev/null || true")


def _container_node(node_id: int, ssh_container: dict[str, Any]) -> Node:  # type: ignore[type-arg]
    """Build a Node from a testcontainers SSH fixture (node_id-first identity)."""
    return Node(
        node_id=NodeId(node_id),
        hostname=ssh_container["host"],
        ncpus=4,
        enabled=True,
        cloud=None,
        username=ssh_container["username"],
        port=ssh_container["port"],
    )


def _make_pengine(
    *,
    check_pname: str | None = None,
    check_cmd: str | None = None,
    check_cmd_code: int = 0,
    sleep_interval: int = 1,
) -> MagicMock:
    """Create an Engine-like mock for occupancy checks."""
    engine = MagicMock(spec=Engine)
    engine.name = "test_engine"
    engine.check_pname = check_pname
    engine.check_cmd = check_cmd
    engine.check_cmd_code = check_cmd_code
    engine.sleep_interval = sleep_interval
    engine.deployable = ()
    engine.platforms = ("linux",)
    return engine


class TestOccupancyIntegration:
    """Integration tests for occupancy_check against real Docker SSH server."""

    async def _get_session(self, repository: SSHMachineRepository) -> MachineSession:
        sessions = repository.list_free(None)
        assert len(sessions) > 0
        return sessions[0]

    async def _start_bg_process(
        self,
        session: MachineSession,
        cmd: str,
    ) -> None:
        """Start a detached background process on remote via nohup."""
        result = await session.run(f"nohup {cmd} >/dev/null 2>&1 &")
        assert result.exit_code == 0

    async def _kill_bg(
        self,
        session: MachineSession,
        name: str,
    ) -> None:
        """Kill all processes with given name on remote."""
        await session.run(f"killall {name} 2>/dev/null || true")

    async def test_occupancy_check_pname_detects_sleep(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_pname='sleep' finds running sleep via pgrep."""
        engine = _make_pengine(check_pname="sleep")
        session = await self._get_session(repository)

        try:
            await self._start_bg_process(session, "sleep 60")
            await asyncio.sleep(0.5)
            result = await occupancy_checker.occupancy_check(session, engine)
            assert result is True
        finally:
            await self._kill_bg(session, "sleep")

    async def test_occupancy_check_pname_no_match_after_kill(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_pname='sleep' returns False after sleep is killed."""
        engine = _make_pengine(check_pname="sleep")
        session = await self._get_session(repository)

        await self._start_bg_process(session, "sleep 60")
        await asyncio.sleep(0.5)

        busy = await occupancy_checker.occupancy_check(session, engine)
        assert busy is True

        await self._kill_bg(session, "sleep")
        await asyncio.sleep(0.5)

        busy = await occupancy_checker.occupancy_check(session, engine)
        assert busy is False

    async def test_occupancy_check_pname_nonexistent(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_pname with nonexistent process returns False."""
        engine = _make_pengine(check_pname="yascheduler_nonexistent_test_proc")
        session = await self._get_session(repository)

        result = await occupancy_checker.occupancy_check(session, engine)
        assert result is False

    async def test_occupancy_check_cmd_pgrep_detects_sleep(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd='pgrep -x sleep' with code 0 detects running sleep."""
        engine = _make_pengine(check_cmd="pgrep -x sleep", check_cmd_code=0)
        session = await self._get_session(repository)

        try:
            await self._start_bg_process(session, "sleep 60")
            await asyncio.sleep(0.5)
            result = await occupancy_checker.occupancy_check(session, engine)
            assert result is True
        finally:
            await self._kill_bg(session, "sleep")

    async def test_occupancy_check_cmd_pgrep_no_match_after_kill(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd='pgrep -x sleep' returns False after sleep is killed."""
        engine = _make_pengine(check_cmd="pgrep -x sleep", check_cmd_code=0)
        session = await self._get_session(repository)

        await self._start_bg_process(session, "sleep 60")
        await asyncio.sleep(0.5)

        busy = await occupancy_checker.occupancy_check(session, engine)
        assert busy is True

        await self._kill_bg(session, "sleep")
        await asyncio.sleep(0.5)

        busy = await occupancy_checker.occupancy_check(session, engine)
        assert busy is False

    async def test_occupancy_check_cmd_grep_q_detects_sleep(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd='ps -eocomm= | grep -q sleep' detects running sleep."""
        engine = _make_pengine(
            check_cmd="ps -eocomm= | grep -q sleep",
            check_cmd_code=0,
        )
        session = await self._get_session(repository)

        try:
            await self._start_bg_process(session, "sleep 60")
            await asyncio.sleep(0.5)
            result = await occupancy_checker.occupancy_check(session, engine)
            assert result is True
        finally:
            await self._kill_bg(session, "sleep")

    async def test_occupancy_check_cmd_grep_q_no_match_after_kill(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd='ps -eocomm= | grep -q sleep' returns False after kill."""
        engine = _make_pengine(
            check_cmd="ps -eocomm= | grep -q sleep",
            check_cmd_code=0,
        )
        session = await self._get_session(repository)

        await self._start_bg_process(session, "sleep 60")
        await asyncio.sleep(0.5)

        busy = await occupancy_checker.occupancy_check(session, engine)
        assert busy is True

        await self._kill_bg(session, "sleep")
        await asyncio.sleep(0.5)

        busy = await occupancy_checker.occupancy_check(session, engine)
        assert busy is False

    async def test_occupancy_check_pname_priority_over_cmd(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """When both check_pname and check_cmd are set, pgrep takes priority."""
        engine = _make_pengine(
            check_pname="sleep",
            check_cmd="pgrep -x nonexistent_process_xyz",
            check_cmd_code=0,
        )
        session = await self._get_session(repository)

        try:
            await self._start_bg_process(session, "sleep 60")
            await asyncio.sleep(0.5)
            result = await occupancy_checker.occupancy_check(session, engine)
            assert result is True
        finally:
            await self._kill_bg(session, "sleep")

    async def test_occupancy_check_cmd_nonzero_code(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd with non-zero expected code (inverted logic)."""
        engine = _make_pengine(check_cmd="pgrep -x sleep", check_cmd_code=1)
        session = await self._get_session(repository)

        # No sleep running: pgrep returns 1, which matches check_cmd_code=1
        result = await occupancy_checker.occupancy_check(session, engine)
        assert result is True

    async def test_occupancy_check_cmd_nonzero_code_no_match(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd_code=1 does NOT match when process IS running (pgrep returns 0)."""
        engine = _make_pengine(check_cmd="pgrep -x sleep", check_cmd_code=1)
        session = await self._get_session(repository)

        try:
            await self._start_bg_process(session, "sleep 60")
            await asyncio.sleep(0.5)
            result = await occupancy_checker.occupancy_check(session, engine)
            assert result is False
        finally:
            await self._kill_bg(session, "sleep")

    async def test_start_occupancy_check_releases_on_short_process(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """start_occupancy_check releases machine when short-lived process exits."""
        engine = _make_pengine(check_pname="sleep", sleep_interval=1)
        session = await self._get_session(repository)

        # Occupy machine
        session.occupy()
        assert session.machine.state == MachineState.BUSY

        try:
            # Start short-lived process (2 seconds)
            result = await session.run(
                "nohup sleep 2 >/dev/null 2>&1 &",
            )
            assert result.exit_code == 0
            await asyncio.sleep(0.5)

            occupancy_checker.start_occupancy_check(session, engine)
            # Wait for checker to detect completion (sleep_interval + buffer)
            task = cast("SSHMachineSession", session)._monitor_task
            assert task is not None, "monitor should be installed"
            await asyncio.wait_for(task, timeout=5.0)

            # Machine should be released
            assert session.machine.state == MachineState.FREE
        finally:
            await session.run(
                "killall sleep 2>/dev/null || true",
            )


class TestOccupancyRaceCondition:
    """Test the two-level state sync between session and RemoteMachineMetadata.

    Regression test for the bug where start_occupancy_check did NOT occupy
    the session at the repository level. The _meta_sync background task
    (in remote_machine.py) polls repository state every second and mirrors it
    to meta.busy. Without the fix, session.machine.state stayed FREE,
    causing _meta_sync to set meta.busy=False after 1 second, making the
    task consumer immediately consume (and fail) the running task.

    The fix: start_occupancy_check calls session.occupy() before
    starting the _checker background task.
    """

    async def _get_session(self, repository: SSHMachineRepository) -> MachineSession:
        sessions = repository.list_free(None)
        assert len(sessions) > 0
        return sessions[0]

    async def test_start_occupancy_check_sets_session_busy(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """start_occupancy_check must occupy the session (BUSY) at repository level.

        Before fix: session.machine.state stayed FREE → _meta_sync saw FREE →
        set meta.busy=False → consumer consumed running task immediately.
        """
        engine = _make_pengine(check_pname="sleep", sleep_interval=1)
        session = await self._get_session(repository)

        # Machine starts FREE
        assert session.machine.state == MachineState.FREE

        # Start a background process (simulates run_bg spawn)
        await session.run("nohup sleep 5 >/dev/null 2>&1 &")
        await asyncio.sleep(0.5)

        try:
            # THE FIX: start_occupancy_check must set the session to BUSY
            occupancy_checker.start_occupancy_check(session, engine)

            assert session.machine.state == MachineState.BUSY, (
                "start_occupancy_check must occupy session at repository level"
            )

            # Simulate _meta_sync: it polls repository state and would mirror to meta.busy
            # With the fix, it sees BUSY (not FREE), so meta.busy stays True
            # type: ignore[unreachable]: pyright narrows from the FREE assert above; it can't see that start_occupancy_check mutated the snapshot.
            repo_session = repository.get_session(NodeId(1))  # type: ignore[unreachable]
            assert repo_session is not None
            assert repo_session.machine.state == MachineState.BUSY, (
                "_meta_sync would see FREE without the fix, causing premature task consumption"
            )
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_meta_sync_pattern_does_not_prematurely_free(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """Simulating _meta_sync polling: must see BUSY while process runs."""
        engine = _make_pengine(check_pname="sleep", sleep_interval=1)
        session = await self._get_session(repository)

        # Start process, then start occupancy check
        await session.run("nohup sleep 3 >/dev/null 2>&1 &")
        await asyncio.sleep(0.5)

        try:
            occupancy_checker.start_occupancy_check(session, engine)

            # Simulate _meta_sync: poll repository state for 2 seconds
            # Without the fix, at least one poll would see FREE
            for _ in range(4):
                await asyncio.sleep(0.5)
                repo_session = repository.get_session(NodeId(1))
                assert repo_session is not None
                assert repo_session.machine.state == MachineState.BUSY, (
                    "_meta_sync must consistently see BUSY while process is running"
                )
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_session_released_after_process_exits(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """After process exits, checker detects it and releases the session."""
        engine = _make_pengine(check_pname="sleep", sleep_interval=1)
        session = await self._get_session(repository)

        # Short-lived process (2 seconds)
        await session.run("nohup sleep 2 >/dev/null 2>&1 &")
        await asyncio.sleep(0.5)

        try:
            occupancy_checker.start_occupancy_check(session, engine)
            assert session.machine.state == MachineState.BUSY

            # Wait for checker to detect exit (sleep_interval + process time + buffer)
            task = cast("SSHMachineSession", session)._monitor_task
            assert task is not None, (
                "monitor should be installed after start_occupancy_check"
            )
            await asyncio.wait_for(task, timeout=5.0)

            # Checker should have released the machine
            assert session.machine.state == MachineState.FREE, (
                "session must be FREE after process exits and checker releases it"
            )
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_already_busy_machine_stays_busy_on_occupancy_start(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """start_occupancy_check on already-BUSY machine is a no-op (idempotent)."""
        engine = _make_pengine(check_pname="sleep", sleep_interval=1)
        session = await self._get_session(repository)

        # Manually occupy
        session.occupy()
        assert session.machine.state == MachineState.BUSY

        await session.run("nohup sleep 3 >/dev/null 2>&1 &")
        await asyncio.sleep(0.5)

        try:
            # start_occupancy_check on already-BUSY machine should not crash
            occupancy_checker.start_occupancy_check(session, engine)
            assert session.machine.state == MachineState.BUSY
        finally:
            # Wait for checker to finish (if it was registered for this session)
            task = cast("SSHMachineSession", session)._monitor_task
            if task is not None:
                await asyncio.wait_for(task, timeout=5.0)
            await session.run("killall sleep 2>/dev/null || true")


class TestOccupancySpawnScenario:
    """Integration tests simulating the real spawn → occupancy_check flow.

    Uses run_bg (like orchestrator._exec_spawn_command) to start processes,
    not nohup. This catches bugs where the SSH channel lifecycle affects
    the remote process.
    """

    async def _get_session(self, repository: SSHMachineRepository) -> MachineSession:
        sessions = repository.list_free(None)
        assert len(sessions) > 0
        return sessions[0]

    async def test_pgrep_detects_run_bg_process(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """Pgrep finds a process started via run_bg (like spawn does)."""
        engine = _make_pengine(check_pname="sleep")
        session = await self._get_session(repository)

        await session.run_bg("sleep 60", cwd="/tmp")
        try:
            await asyncio.sleep(1)
            busy = await occupancy_checker.occupancy_check(session, engine)
            assert busy is True, "pgrep should find sleep process started via run_bg"
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_pname_detects_spawn_like_command_via_run_bg(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_pname finds process from spawn-like command via run_bg.

        Simulates: spawn = sleep 60 && cat 1.input > 1.input.out
        """
        engine = _make_pengine(check_pname="sleep")
        session = await self._get_session(repository)

        # Simulate spawn command: cd to dir, then run sleep
        await session.run_bg("sleep 60", cwd="/tmp")
        try:
            await asyncio.sleep(1)
            busy = await occupancy_checker.occupancy_check(session, engine)
            assert busy is True, "sleep process should be found by pgrep after run_bg"
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_pname_still_detects_after_handle_discarded(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """Process must survive even if run_bg handle is not stored.

        In real orchestrator, _exec_spawn_command calls
        `await session.run_bg(cmd)` without storing the handle.
        When the handle is GC'd, the SSH channel should NOT close
        (asyncssh does not close on __del__). This test verifies that.
        """
        engine = _make_pengine(check_pname="sleep")
        session = await self._get_session(repository)

        await session.run_bg("sleep 60", cwd="/tmp")
        import gc

        gc.collect()

        try:
            await asyncio.sleep(1)
            busy = await occupancy_checker.occupancy_check(session, engine)
            assert busy is True, "sleep must survive SSHClientProcess handle GC"
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_cmd_grep_detects_run_bg_process(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """check_cmd with grep detects process started via run_bg."""
        engine = _make_pengine(
            check_cmd="ps -eocomm= | grep -q sleep",
            check_cmd_code=0,
        )
        session = await self._get_session(repository)

        await session.run_bg("sleep 60", cwd="/tmp")
        try:
            await asyncio.sleep(1)
            busy = await occupancy_checker.occupancy_check(session, engine)
            assert busy is True, "check_cmd should detect sleep via ps|grep"
        finally:
            await session.run("killall sleep 2>/dev/null || true")

    async def test_occupancy_check_via_pgrep_raw_output(
        self,
        repository: SSHMachineRepository,
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """Diagnostic: show what pgrep -f actually finds on the remote machine."""
        session = await self._get_session(repository)

        # Start sleep via run_bg
        await session.run_bg("sleep 60", cwd="/tmp")
        try:
            await asyncio.sleep(0.5)

            # List processes via pgrep -f sleep
            procs = [p async for p in session.pgrep("sleep")]

            # Must find at least the sleep process
            assert len(procs) >= 1, (
                f"pgrep should find at least 1 process, found: {procs}"
            )

            # At least one process should have 'sleep' in its command
            found_sleep = any(
                "sleep" in p.command.lower() or "sleep" in p.name.lower() for p in procs
            )
            assert found_sleep, (
                f"No process with 'sleep' in name/command found: {procs}"
            )
        finally:
            await session.run("killall sleep 2>/dev/null || true")


# =============================================================================
# Multi-machine regression: disconnect must not cancel other machines' monitors
# =============================================================================
#
# Real-asyncssh counterpart to the unit test
# ``test_disconnect_does_not_cancel_other_machines_monitors``. Uses two SSH
# testcontainers reached via genuinely distinct endpoints: machine A via
# ``ssh_container`` (host IP, mapped port) and machine B via ``ssh_container_2``
# (127.0.0.2, mapped port). A distinct loopback address is required because
# ``get_container_host_ip`` returns ``localhost`` for both testcontainers, which
# would collide since SSHMachineRepository keys ``_sessions`` by IP
# only — that collision is exactly what made the original YASCHED_MULTI_CONTAINER
# variant always fail.


class TestMultiMachineBgTaskLeak:
    """Regression: disconnecting one machine must not cancel another's monitor."""

    async def test_disconnect_does_not_cancel_other_machines_monitors(
        self,
        ssh_container: dict[str, Any],  # type: ignore[type-arg]
        ssh_container_2: dict[str, Any],  # type: ignore[type-arg]
        occupancy_checker: OccupancyChecker,
    ) -> None:
        """Disconnect A cancels only A's monitor; B's monitor stays alive."""
        repository = SSHMachineRepository()
        engine = _make_pengine(check_pname="sleep", sleep_interval=1)

        session_a = cast(
            "SSHMachineSession",
            await repository.connect(
                node=_container_node(4, ssh_container),
                client_keys=[ssh_container["key_path"]],
            ),
        )
        session_b = cast(
            "SSHMachineSession",
            await repository.connect(
                node=_container_node(5, ssh_container_2),
                client_keys=[ssh_container_2["key_path"]],
            ),
        )

        ip_a = ssh_container["host"]
        ip_b = ssh_container_2["host"]
        assert ip_a != ip_b, (
            f"test requires two distinct IPs, got ip_a={ip_a!r} ip_b={ip_b!r}"
        )

        try:
            # Start a long-running sleep on each so the monitors stay BUSY
            for s in (session_a, session_b):
                await s.run(
                    "nohup sleep 300 >/dev/null 2>&1 &",
                )
            await asyncio.sleep(0.5)

            # Start occupancy monitors on each
            for s in (session_a, session_b):
                occupancy_checker.start_occupancy_check(s, engine)

            assert session_a._monitor_task is not None
            assert session_b._monitor_task is not None
            task_b = session_b._monitor_task

            # Disconnect A — must cancel only A's monitor
            await repository.disconnect(NodeId(4))

            assert NodeId(4) not in repository._sessions
            # B's monitor and session are untouched
            assert not task_b.done(), "B monitor must survive disconnect(A)"
            assert session_b._monitor_task is task_b
            assert NodeId(5) in repository._sessions

            await repository.disconnect(NodeId(5))
        finally:
            # Best-effort cleanup of any surviving monitor / remote process
            await repository.disconnect_all()
