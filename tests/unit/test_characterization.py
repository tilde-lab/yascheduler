"""Characterization tests: verify Client.queue_submit_task_async delegates to deps.submit via deps_factory."""
# region MODULE_CONTRACT
# PURPOSE: Characterization tests — verify Yascheduler client queue-submit behaviour.
# SCOPE: Client sync/async queue-submit forwarding through every public facade and the deps_factory seam.
# KEYWORDS: client queue, submit, deps_factory, output selections, public facade
# endregion MODULE_CONTRACT

from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yascheduler.domain import TaskId, TaskInputFile, ValidationError


class TestClientQueueSubmitTaskAsync:
    """Client.queue_submit_task_async delegates to deps.submit via the deps_factory seam."""

    @pytest.mark.asyncio
    @patch("yascheduler.entrypoints.client.parse_config")
    async def test_queue_submit_task_async_uses_cli_deps(
        self,
        mock_from_cfg: MagicMock,
    ) -> None:
        """queue_submit_task_async calls deps.submit() via the injected deps_factory, not Scheduler."""
        from yascheduler.entrypoints.client import Yascheduler

        # Arrange — inject fake deps via the constructor seam.
        # deps.submit returns TaskId; the facade extracts .value → public int contract.
        mock_deps = MagicMock()
        mock_deps.submit = AsyncMock(return_value=TaskId(99))
        factory_calls: list = []
        mock_from_cfg.return_value = MagicMock()

        def counting_factory(cfg: object) -> MagicMock:
            factory_calls.append(cfg)
            return mock_deps

        client = Yascheduler(deps_factory=counting_factory)  # type: ignore[arg-type]

        # Act
        result = await client.queue_submit_task_async(
            label="test-job",
            metadata={"key": "val"},
            engine_name="fleur",
        )

        # Assert
        assert result == 99
        assert factory_calls == [client.config]
        mock_deps.submit.assert_awaited_once_with(
            "test-job",
            {"key": "val"},
            "fleur",
            input_entries=None,
            output_selections=None,
        )

    @pytest.mark.asyncio
    @patch("yascheduler.entrypoints.client.parse_config")
    async def test_queue_submit_task_async_forwards_input_entries(
        self,
        mock_from_cfg: MagicMock,
    ) -> None:
        """queue_submit_task_async forwards explicit typed entries to deps.submit."""
        from yascheduler.entrypoints.client import Yascheduler

        mock_deps = MagicMock()
        mock_deps.submit = AsyncMock(return_value=TaskId(99))
        mock_from_cfg.return_value = MagicMock()
        client = Yascheduler(deps_factory=lambda cfg: mock_deps)  # type: ignore[arg-type]
        entries = (TaskInputFile(path="INPUT", content=b"body"),)

        result = await client.queue_submit_task_async(
            label="test-job",
            metadata={"key": "val"},
            engine_name="fleur",
            input_entries=entries,
        )

        assert result == 99
        mock_deps.submit.assert_awaited_once_with(
            "test-job",
            {"key": "val"},
            "fleur",
            input_entries=entries,
            output_selections=None,
        )

    @patch("yascheduler.entrypoints.client.parse_config")
    def test_queue_submit_task_forwards_webhook_flag_and_input_entries(
        self,
        mock_from_cfg: MagicMock,
    ) -> None:
        """Sync wrapper forwards webhook_onsubmit and input_entries to async path."""
        from yascheduler.entrypoints.client import Yascheduler

        mock_from_cfg.return_value = MagicMock()
        client = Yascheduler(deps_factory=lambda cfg: MagicMock())  # type: ignore[arg-type]
        entries = (TaskInputFile(path="INPUT", content=b"body"),)

        with patch.object(
            client,
            "queue_submit_task_async",
            new=AsyncMock(return_value=77),
        ) as mock_async_submit:
            result = client.queue_submit_task(
                label="test-job",
                metadata={"key": "val"},
                engine_name="fleur",
                webhook_onsubmit=True,
                input_entries=entries,
            )

        assert result == 77
        mock_async_submit.assert_awaited_once_with(
            "test-job",
            {"key": "val"},
            "fleur",
            _webhook_onsubmit=True,
            input_entries=entries,
            output_selections=None,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "facade_module",
        ["yascheduler", "yascheduler.client", "yascheduler.entrypoints"],
    )
    async def test_every_public_facade_async_submit_forwards_output_selections(
        self,
        facade_module: str,
    ) -> None:
        """Async submissions forward supplied output selections through each facade."""
        client_cls = import_module(facade_module).Yascheduler
        mock_deps = MagicMock()
        mock_deps.submit = AsyncMock(return_value=TaskId(99))
        selections = ["result.dat", "OUT/**/*"]

        with patch(
            "yascheduler.entrypoints.client.parse_config",
            return_value=MagicMock(),
        ):
            client = client_cls(deps_factory=lambda cfg: mock_deps)

        result = await client.queue_submit_task_async(
            label="test-job",
            metadata={"key": "val"},
            engine_name="fleur",
            output_selections=selections,
        )

        assert result == 99
        mock_deps.submit.assert_awaited_once_with(
            "test-job",
            {"key": "val"},
            "fleur",
            input_entries=None,
            output_selections=selections,
        )

    @pytest.mark.parametrize(
        "facade_module",
        ["yascheduler", "yascheduler.client", "yascheduler.entrypoints"],
    )
    def test_every_public_facade_sync_submit_forwards_output_selections(
        self,
        facade_module: str,
    ) -> None:
        """Sync submissions forward supplied output selections through each facade."""
        client_cls = import_module(facade_module).Yascheduler
        selections = ["result.dat", "OUT/**/*"]

        with patch(
            "yascheduler.entrypoints.client.parse_config",
            return_value=MagicMock(),
        ):
            client = client_cls(deps_factory=lambda cfg: MagicMock())

        with patch.object(
            client,
            "queue_submit_task_async",
            new=AsyncMock(return_value=77),
        ) as mock_async_submit:
            result = client.queue_submit_task(
                label="test-job",
                metadata={"key": "val"},
                engine_name="fleur",
                output_selections=selections,
            )

        assert result == 77
        mock_async_submit.assert_awaited_once_with(
            "test-job",
            {"key": "val"},
            "fleur",
            _webhook_onsubmit=False,
            input_entries=None,
            output_selections=selections,
        )

    @pytest.mark.asyncio
    @patch("yascheduler.entrypoints.client.parse_config")
    async def test_queue_submit_task_async_propagates_output_selection_rejection(
        self,
        mock_from_cfg: MagicMock,
    ) -> None:
        """A use-case output-selection rejection reaches the async client caller."""
        from yascheduler.entrypoints.client import Yascheduler

        mock_deps = MagicMock()
        mock_deps.submit = AsyncMock(side_effect=ValidationError("unsafe selection"))
        mock_from_cfg.return_value = MagicMock()
        client = Yascheduler(deps_factory=lambda cfg: mock_deps)  # type: ignore[arg-type]

        with pytest.raises(ValidationError, match="unsafe selection"):
            await client.queue_submit_task_async(
                label="test-job",
                metadata={"key": "val"},
                engine_name="fleur",
                output_selections=["../escape"],
            )

        mock_deps.submit.assert_awaited_once_with(
            "test-job",
            {"key": "val"},
            "fleur",
            input_entries=None,
            output_selections=["../escape"],
        )
