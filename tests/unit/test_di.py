# region MODULE_CONTRACT
# PURPOSE: Unit tests for di.py — dependency injection composition root.
# SCOPE: CLIDeps dataclass, make_cli_deps, make_daemon factories.
# KEYWORDS: dependency injection, CLIDeps, make_cli_deps, make_daemon
# endregion MODULE_CONTRACT

import asyncio
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yascheduler.application.allocation_tracker import AllocationTracker
from yascheduler.domain import (
    Engine,
    EngineRepository,
    InvalidTaskSnapshotError,
    LocalSettings,
    RemoteDefaults,
    TaskInputDirectory,
    TaskInputFile,
    UnsupportedEngineError,
)
from yascheduler.domain.events import (
    TaskAbandoned,
    TaskAllocated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
)
from yascheduler.domain.model import TaskId
from yascheduler.entrypoints import Config
from yascheduler.entrypoints.di import CLIDeps, make_cli_deps, make_daemon
from yascheduler.infra.persistence import PostgresDbConfig
from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

# =============================================================================
# Helpers
# =============================================================================


def create_mock_config() -> MagicMock:
    """Create a mocked Config with sub-config mocks needed by DI factories."""
    engines = MagicMock(spec=EngineRepository)
    db = MagicMock(spec=PostgresDbConfig)
    remote = MagicMock(spec=RemoteDefaults)
    remote.tasks_dir = PurePosixPath("/tmp/tasks")
    local = MagicMock(spec=LocalSettings)
    local.tasks_dir = Path("/tmp")

    config = MagicMock(spec=Config)
    config.engines = engines
    config.db = db
    config.remote = remote
    config.clouds = []
    config.local = local
    return config


# =============================================================================
# Tests
# =============================================================================


class TestCLIDeps:
    """CLIDeps dataclass: constructor, submit."""

    def test_constructor_stores_fields(self) -> None:
        """CLIDeps stores engines, uow_factory."""
        engines = MagicMock(spec=EngineRepository)
        uow_factory = MagicMock()

        deps = CLIDeps(
            engines=engines,
            uow_factory=uow_factory,
        )

        assert deps.engines is engines
        assert deps.uow_factory is uow_factory

    @pytest.mark.asyncio
    async def test_submit_delegates_to_submit_task(self) -> None:
        """submit() delegates to submit_task with all positional args."""
        engines = MagicMock(spec=EngineRepository)
        uow_factory = MagicMock()
        deps = CLIDeps(
            engines=engines,
            uow_factory=uow_factory,
        )

        with patch(
            "yascheduler.entrypoints.di.submit_task",
            new=AsyncMock(return_value=TaskId(42)),
        ) as mock_submit:
            result = await deps.submit("my-label", {"key": "val"}, "g09")

        assert result == TaskId(42)
        mock_submit.assert_awaited_once_with(
            "my-label",
            {"key": "val"},
            "g09",
            engines,
            uow_factory,
            input_snapshot=(),
            output_selections=None,
        )

    @pytest.mark.asyncio
    async def test_submit_legacy_path_forwards_snapshot_and_strips_claimed_metadata(
        self,
    ) -> None:
        """Known-engine legacy payload adds a snapshot and strips claimed input keys."""
        engine = Engine(name="g09", spawn="run", input_files=("INPUT",))
        engines = EngineRepository(data={"g09": engine})
        uow_factory = MagicMock()
        deps = CLIDeps(engines=engines, uow_factory=uow_factory)
        metadata: dict[str, object] = {
            "INPUT": "legacy-body",
            "unrelated": 42,
            "local_folder": "local",
        }

        with patch(
            "yascheduler.entrypoints.di.submit_task",
            new=AsyncMock(return_value=TaskId(42)),
        ) as mock_submit:
            result = await deps.submit("my-label", metadata, "g09")

        assert result == TaskId(42)
        mock_submit.assert_awaited_once_with(
            "my-label",
            {"unrelated": 42, "local_folder": "local"},
            "g09",
            engines,
            uow_factory,
            input_snapshot=(TaskInputFile(path="INPUT", content=b"legacy-body"),),
            output_selections=None,
        )

    @pytest.mark.asyncio
    async def test_submit_explicit_path_forwards_normalized_entries(self) -> None:
        """Explicit entries are forwarded as a normalized snapshot."""
        engine = Engine(name="g09", spawn="run", input_files=("INPUT",))
        engines = EngineRepository(data={"g09": engine})
        uow_factory = MagicMock()
        deps = CLIDeps(engines=engines, uow_factory=uow_factory)
        entries = (TaskInputFile(path="basis/x", content=b"x"),)

        with patch(
            "yascheduler.entrypoints.di.submit_task",
            new=AsyncMock(return_value=TaskId(42)),
        ) as mock_submit:
            result = await deps.submit(
                "my-label", {"unrelated": 42}, "g09", input_entries=entries
            )

        assert result == TaskId(42)
        mock_submit.assert_awaited_once_with(
            "my-label",
            {"unrelated": 42},
            "g09",
            engines,
            uow_factory,
            input_snapshot=(
                TaskInputDirectory(path="basis"),
                TaskInputFile(path="basis/x", content=b"x"),
            ),
            output_selections=None,
        )

    @pytest.mark.asyncio
    async def test_submit_unknown_engine_forwards_empty_snapshot(
        self,
    ) -> None:
        """Unknown engines pass metadata through unnormalized so the use case can reject them."""
        engines = EngineRepository(data={})
        uow_factory = MagicMock()
        deps = CLIDeps(engines=engines, uow_factory=uow_factory)
        metadata: dict[str, object] = {"INPUT": "legacy-body"}
        selections = ["result.dat", "OUT/**/*"]

        with (
            patch(
                "yascheduler.entrypoints.di.submit_task",
                new=AsyncMock(side_effect=UnsupportedEngineError("missing-engine")),
            ) as mock_submit,
            pytest.raises(UnsupportedEngineError),
        ):
            await deps.submit(
                "my-label",
                metadata,
                "missing-engine",
                output_selections=selections,
            )

        mock_submit.assert_awaited_once_with(
            "my-label",
            metadata,
            "missing-engine",
            engines,
            uow_factory,
            input_snapshot=(),
            output_selections=selections,
        )

    @pytest.mark.asyncio
    async def test_submit_forwards_supplied_output_selections(self) -> None:
        """Known-engine submissions forward supplied output selections verbatim."""
        engine = Engine(name="g09", spawn="run")
        engines = EngineRepository(data={"g09": engine})
        uow_factory = MagicMock()
        deps = CLIDeps(engines=engines, uow_factory=uow_factory)
        selections = ["result.dat", "OUT/**/*"]

        with patch(
            "yascheduler.entrypoints.di.submit_task",
            new=AsyncMock(return_value=TaskId(42)),
        ) as mock_submit:
            result = await deps.submit(
                "my-label",
                {"unrelated": 42},
                "g09",
                output_selections=selections,
            )

        assert result == TaskId(42)
        mock_submit.assert_awaited_once_with(
            "my-label",
            {"unrelated": 42},
            "g09",
            engines,
            uow_factory,
            input_snapshot=(),
            output_selections=selections,
        )

    @pytest.mark.asyncio
    async def test_submit_ambiguous_explicit_payload_fails_before_use_case(
        self,
    ) -> None:
        """Explicit entries plus a declared metadata key are rejected before submit_task."""
        engine = Engine(name="g09", spawn="run", input_files=("INPUT",))
        engines = EngineRepository(data={"g09": engine})
        uow_factory = MagicMock()
        deps = CLIDeps(engines=engines, uow_factory=uow_factory)

        with (
            patch(
                "yascheduler.entrypoints.di.submit_task",
                new=AsyncMock(return_value=TaskId(42)),
            ) as mock_submit,
            pytest.raises(InvalidTaskSnapshotError) as info,
        ):
            await deps.submit(
                "my-label",
                {"INPUT": "legacy-body"},
                "g09",
                input_entries=(TaskInputFile(path="explicit", content=b"x"),),
            )

        assert info.value.path == "INPUT"
        mock_submit.assert_not_awaited()


class TestMakeCliDeps:
    """make_cli_deps factory for lightweight CLI dependencies."""

    @pytest.mark.asyncio
    async def test_returns_cli_deps_with_correct_fields(self) -> None:
        """make_cli_deps returns CLIDeps with config-derived engines."""
        config = create_mock_config()

        with patch("yascheduler.entrypoints.di.aiohttp.ClientSession"):
            deps = make_cli_deps(config)

        assert isinstance(deps, CLIDeps)
        assert deps.engines is config.engines

    @pytest.mark.asyncio
    async def test_uow_factory_creates_postgres_unit_of_work(self) -> None:
        """uow_factory callable returns a PostgresUnitOfWork initialized with config.db and bus."""
        config = create_mock_config()

        with patch("yascheduler.entrypoints.di.aiohttp.ClientSession"):
            deps = make_cli_deps(config)
        uow = cast("PostgresUnitOfWork", deps.uow_factory())

        assert isinstance(uow, PostgresUnitOfWork)
        assert uow._config is config.db

    @pytest.mark.asyncio
    async def test_no_webhook_handlers_in_cli_mode(self) -> None:
        """CLI mode registers no webhook handlers — bus has empty handler registry."""
        config = create_mock_config()

        with patch("yascheduler.entrypoints.di.aiohttp.ClientSession"):
            deps = make_cli_deps(config)

        # Access the bus via the UoW factory to verify no handlers registered
        uow = cast("PostgresUnitOfWork", deps.uow_factory())
        bus = uow._bus
        for event_type in (
            TaskCreated,
            TaskAllocated,
            TaskCompleted,
            TaskFailed,
            TaskAbandoned,
        ):
            assert event_type not in bus._handlers


class TestMakeDaemon:
    """make_daemon async factory — creates full daemon dependency graph."""

    @pytest.fixture(autouse=True)
    def _stub_http_session(self) -> Iterator[MagicMock]:
        """Stub aiohttp.ClientSession so no real session is created.

        make_daemon hands the session to Orchestrator, which closes it on
        stop(). These tests mock Orchestrator, so stop() never runs — a real
        session would leak and emit "Unclosed client session" on GC.
        """
        session = MagicMock()
        session.close = AsyncMock()
        with patch(
            "yascheduler.entrypoints.di.aiohttp.ClientSession",
            return_value=session,
        ):
            yield session

    @pytest.mark.asyncio
    async def test_creates_dependencies_no_db(self) -> None:
        """make_daemon returns Orchestrator without creating DB."""
        config = create_mock_config()
        mock_orch_instance = MagicMock()

        with (
            patch(
                "yascheduler.entrypoints.di.resolve_adapter",
                return_value=None,
            ) as mock_resolve,
            patch("yascheduler.entrypoints.di.SSHMachineRepository") as mock_repo_ctor,
            patch("yascheduler.entrypoints.di.TaskDeployer") as mock_deploy_ctor,
            patch("yascheduler.entrypoints.di.OutputDownloader") as mock_dl_ctor,
            patch("yascheduler.entrypoints.di.OccupancyChecker") as mock_occ_ctor,
            patch(
                "yascheduler.entrypoints.di.CloudProvisionerImpl",
            ) as mock_clouds_ctor,
            patch(
                "yascheduler.entrypoints.di.Orchestrator",
                return_value=mock_orch_instance,
            ) as mock_orch,
        ):
            mock_repo = MagicMock()
            mock_repo_ctor.return_value = mock_repo
            mock_deploy = MagicMock()
            mock_deploy_ctor.return_value = mock_deploy
            mock_dl = MagicMock()
            mock_dl_ctor.return_value = mock_dl
            mock_occ = MagicMock()
            mock_occ_ctor.return_value = mock_occ

            result = await make_daemon(config)

        assert result is mock_orch_instance

        mock_resolve.assert_not_called()
        # share-ssh-gateway: exactly one SSHMachineRepository + one of each
        # collaborator (TaskDeployer/OutputDownloader/OccupancyChecker).
        # CloudProvisionerImpl gets machine_repository only (no machine_operations);
        # Orchestrator gets repository + the three collaborators.
        mock_repo_ctor.assert_called_once()
        mock_deploy_ctor.assert_called_once()
        mock_dl_ctor.assert_called_once()
        mock_occ_ctor.assert_called_once()
        clouds_kwargs = mock_clouds_ctor.call_args.kwargs
        orch_kwargs = mock_orch.call_args.kwargs
        assert clouds_kwargs["machine_repository"] is mock_repo
        assert "machine_operations" not in clouds_kwargs
        assert orch_kwargs["repository"] is mock_repo
        assert orch_kwargs["task_deployer"] is mock_deploy
        assert orch_kwargs["output_downloader"] is mock_dl
        assert orch_kwargs["occupancy_checker"] is mock_occ
        assert orch_kwargs["repository"] is clouds_kwargs["machine_repository"]
        assert "clouds" in orch_kwargs
        assert orch_kwargs["clouds"] is not None
        assert orch_kwargs["local_settings"] is config.local
        assert orch_kwargs["remote_defaults"] is config.remote
        assert "uow_factory" in orch_kwargs
        assert "log" not in orch_kwargs
        assert "log" not in clouds_kwargs
        assert callable(orch_kwargs["uow_factory"])
        # New: allocation_tracker, allocation_lock, active_clouds
        assert "allocation_tracker" in orch_kwargs
        assert isinstance(orch_kwargs["allocation_tracker"], AllocationTracker)
        assert "allocation_lock" in orch_kwargs
        assert isinstance(orch_kwargs["allocation_lock"], asyncio.Lock)
        assert "active_clouds" in orch_kwargs
        assert isinstance(orch_kwargs["active_clouds"], list)
        # Negative: adapters/configs not passed to Orchestrator
        assert "adapters" not in orch_kwargs
        assert "configs" not in orch_kwargs

    @pytest.mark.asyncio
    async def test_uses_provided_clouds(self) -> None:
        """When clouds= keyword is passed, adapter building is skipped."""
        config = create_mock_config()
        custom_clouds = MagicMock()

        with (
            patch("yascheduler.entrypoints.di.resolve_adapter") as mock_resolve,
            patch("yascheduler.entrypoints.di.SSHMachineRepository") as mock_repo_ctor,
            patch("yascheduler.entrypoints.di.TaskDeployer") as mock_deploy_ctor,
            patch("yascheduler.entrypoints.di.OutputDownloader") as mock_dl_ctor,
            patch("yascheduler.entrypoints.di.OccupancyChecker") as mock_occ_ctor,
            patch("yascheduler.entrypoints.di.Orchestrator") as mock_orch,
        ):
            mock_repo = MagicMock()
            mock_repo_ctor.return_value = mock_repo
            mock_deploy = MagicMock()
            mock_deploy_ctor.return_value = mock_deploy
            mock_dl = MagicMock()
            mock_dl_ctor.return_value = mock_dl
            mock_occ = MagicMock()
            mock_occ_ctor.return_value = mock_occ

            await make_daemon(config, clouds=custom_clouds)

        mock_resolve.assert_not_called()
        # pre-built-clouds path keeps its own repository + collaborators —
        # the orchestrator gets a fresh SSHMachineRepository + fresh
        # TaskDeployer/OutputDownloader/OccupancyChecker, NOT the ones on
        # custom_clouds.
        orch_repo = mock_orch.call_args.kwargs["repository"]
        assert orch_repo is mock_repo
        assert orch_repo is not custom_clouds.machine_repository

    @pytest.mark.asyncio
    async def test_prebuilt_clouds_active_clouds_filter_verifies_adapter_resolution(
        self,
    ) -> None:
        """On the pre-built-clouds path, active_clouds must filter by both max_nodes>0 AND adapter resolved (clouds.configs key) — otherwise _clouds_get_capacity over-counts for unresolved providers (review-hardening)."""
        # Two configured clouds: both max_nodes>0, but only 'hetzner' has a
        # resolved adapter in the pre-built clouds.
        hetzner_cfg = MagicMock(prefix="hetzner", max_nodes=5)
        azure_cfg = MagicMock(prefix="az", max_nodes=3)
        config = create_mock_config()
        config.clouds = [hetzner_cfg, azure_cfg]

        # Pre-built clouds whose configs dict has only 'hetzner' resolved.
        custom_clouds = MagicMock()
        custom_clouds.configs = {"hetzner": hetzner_cfg}

        with (
            patch("yascheduler.entrypoints.di.resolve_adapter") as mock_resolve,
            patch("yascheduler.entrypoints.di.Orchestrator") as mock_orch,
        ):
            await make_daemon(config, clouds=custom_clouds)

        # resolve_adapter must NOT be called on the pre-built-clouds path.
        mock_resolve.assert_not_called()
        # active_clouds must exclude 'az' (no resolved adapter) even though
        # its max_nodes > 0.
        orch_kwargs = mock_orch.call_args.kwargs
        active = orch_kwargs["active_clouds"]
        assert [c.prefix for c in active] == ["hetzner"]

    @pytest.mark.asyncio
    async def test_make_daemon_does_not_import_db(self) -> None:
        """make_daemon must not import or reference DB."""
        import yascheduler.entrypoints.di as di_module

        assert not hasattr(di_module, "DB"), "di.py still imports DB"
