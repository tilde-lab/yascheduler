"""Characterization tests: verify Client.queue_submit_task_async delegates to deps.submit via deps_factory."""
# region MODULE_CONTRACT
# PURPOSE: Characterization tests — verify Yascheduler client queue-submit behaviour.
# SCOPE: Client.queue_submit_task_async delegation via deps_factory seam.
# KEYWORDS: client queue, submit, deps_factory
# endregion MODULE_CONTRACT

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yascheduler.domain import TaskId, TaskInputFile


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
            "test-job", {"key": "val"}, "fleur", input_entries=None
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
            "test-job", {"key": "val"}, "fleur", input_entries=entries
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
        )
