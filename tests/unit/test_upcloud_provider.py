"""Tests for Upcloud provider module."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for Upcloud provider: create_node orphan-prevention
#          (best-effort destroy on adoption failure) and the
#          _destroy_created_server_best_effort helper's never-raise contract.
# SCOPE: upcloud.py module-level behavior; CloudManager and Server via mocks.
# KEYWORDS: upcloud, provider, unit, orphan, create, destroy, best-effort
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("upcloud_api")

from yascheduler.infra.cloud.providers import upcloud
from yascheduler.infra.cloud.providers.upcloud import (
    UpCloudAPIError,
    _destroy_created_server_best_effort,
    upcloud_create_node_sync,
)

pytestmark = pytest.mark.unit


def _mock_cfg() -> MagicMock:
    """A ConfigCloudUpcloud-shaped mock with the fields create_node reads."""
    cfg = MagicMock()
    cfg.label = "test-node"
    cfg.username = "debian"
    cfg.jump_host = None
    cfg.jump_port = 22
    cfg.jump_username = None
    return cfg


def _patch_create_deps(
    server: MagicMock, hostname: str = "rnd-hostname"
) -> AbstractContextManager[None]:
    """Patch get_client/get_rnd_name/login_user_block/time.sleep for create_node."""
    client = MagicMock()
    client.create_server.return_value = server

    @contextmanager
    def cm() -> Iterator[None]:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(upcloud, "get_client", return_value=client)
            )
            stack.enter_context(
                patch.object(upcloud, "get_rnd_name", return_value=hostname)
            )
            stack.enter_context(
                patch.object(upcloud, "login_user_block", return_value=MagicMock())
            )
            stack.enter_context(patch.object(upcloud.time, "sleep"))
            yield

    return cm()


# =============================================================================
# upcloud_create_node_sync: orphan prevention
# =============================================================================


class TestCreateNodeOrphanPrevention:
    """create_node must never leave a billable server when adoption fails."""

    def test_create_returns_dto_when_ip_present(self) -> None:
        server = MagicMock()
        server.get_public_ip.return_value = "1.2.3.4"
        key = MagicMock()

        with _patch_create_deps(server):
            dto = upcloud_create_node_sync(_mock_cfg(), key)

        assert dto.external_id == "1.2.3.4"
        assert dto.hostname == "1.2.3.4"
        server.stop.assert_not_called()
        server.destroy.assert_not_called()

    def test_create_destroys_server_when_no_public_ip(self) -> None:
        """REGRESSION C6: a None IP used to raise AssertionError and orphan the VM."""
        server = MagicMock()
        server.get_public_ip.return_value = None
        server.storage_devices = []
        key = MagicMock()

        with (
            _patch_create_deps(server, hostname="h1"),
            pytest.raises(RuntimeError, match="without a public IP"),
        ):
            upcloud_create_node_sync(_mock_cfg(), key)

        # Adoption failed → the created server MUST be torn down best-effort.
        server.stop.assert_called_once()
        server.destroy.assert_called_once()

    def test_create_re_raises_when_get_public_ip_raises(self) -> None:
        server = MagicMock()
        server.get_public_ip.side_effect = ValueError("ip lookup failed")
        server.storage_devices = []
        key = MagicMock()

        with (
            _patch_create_deps(server),
            pytest.raises(ValueError, match="ip lookup failed"),
        ):
            upcloud_create_node_sync(_mock_cfg(), key)

        server.stop.assert_called_once()
        server.destroy.assert_called_once()

    def test_create_propagates_original_when_cleanup_also_fails(self) -> None:
        """Original create error wins even if best-effort destroy errors too."""
        server = MagicMock()
        server.get_public_ip.return_value = None
        server.stop.side_effect = UpCloudAPIError("AUTH", "auth failed")
        key = MagicMock()

        with (
            _patch_create_deps(server),
            pytest.raises(RuntimeError, match="without a public IP"),
        ):
            upcloud_create_node_sync(_mock_cfg(), key)

        # Cleanup attempted; its failure must not mask the original RuntimeError.
        server.stop.assert_called_once()

    def test_create_logs_hostname_when_create_server_raises(
        self, log_records: list[logging.LogRecord]
    ) -> None:
        """If create_server itself raises, no server handle exists — log for manual review."""
        client = MagicMock()
        client.create_server.side_effect = UpCloudAPIError("INVALID", "bad zone")
        key = MagicMock()

        with (
            patch.object(upcloud, "get_client", return_value=client),
            patch.object(upcloud, "get_rnd_name", return_value="lonely-host"),
            patch.object(upcloud, "login_user_block", return_value=MagicMock()),
            patch.object(upcloud.time, "sleep"),
            pytest.raises(UpCloudAPIError),
        ):
            upcloud_create_node_sync(_mock_cfg(), key)

        client.create_server.assert_called_once()
        manual = [r for r in log_records if "CREATE_SERVER_RAISED" in r.getMessage()]
        assert manual, "create_server failure must log hostname for manual review"
        assert "lonely-host" in manual[0].getMessage()


# =============================================================================
# _destroy_created_server_best_effort: never-raise contract
# =============================================================================


class TestDestroyBestEffort:
    """The cleanup helper never raises and handles idempotency."""

    @pytest.fixture(autouse=True)
    def _no_real_sleep(self) -> Generator[None, None, None]:
        """The helper waits _STOP_WAIT_SECONDS before destroy — patch it out."""
        with patch.object(upcloud.time, "sleep"):
            yield

    def test_destroy_succeeds_and_cleans_storage(self) -> None:
        storage1, storage2 = MagicMock(), MagicMock()
        server = MagicMock()
        server.storage_devices = [storage1, storage2]

        _destroy_created_server_best_effort(server, "h")

        server.stop.assert_called_once()
        server.destroy.assert_called_once()
        storage1.destroy.assert_called_once()
        storage2.destroy.assert_called_once()

    def test_destroy_treats_server_not_found_as_already_gone(
        self, log_records: list[logging.LogRecord]
    ) -> None:
        server = MagicMock()
        server.storage_devices = []
        server.destroy.side_effect = UpCloudAPIError("SERVER_NOT_FOUND", "gone")

        _destroy_created_server_best_effort(server, "h")

        assert any("ORPHAN_SERVER_ALREADY_GONE" in r.getMessage() for r in log_records)
        assert any("ORPHAN_SERVER_DESTROYED" in r.getMessage() for r in log_records)

    def test_destroy_never_raises_on_destroy_failure(self) -> None:
        server = MagicMock()
        server.destroy.side_effect = UpCloudAPIError("INTERNAL", "boom")

        # Must not raise — original create error must propagate instead.
        _destroy_created_server_best_effort(server, "h")

    def test_destroy_never_raises_on_stop_failure(
        self, log_records: list[logging.LogRecord]
    ) -> None:
        server = MagicMock()
        server.stop.side_effect = UpCloudAPIError("ILLEGAL_STATE", "running")

        _destroy_created_server_best_effort(server, "h")

        errors = [
            r for r in log_records if "ORPHAN_SERVER_DESTROY_FAILED" in r.getMessage()
        ]
        assert errors, "stop failure must be logged for manual reconciliation"
        assert "h" in errors[0].getMessage()

    def test_destroy_skips_storage_when_server_not_found(self) -> None:
        """SERVER_NOT_FOUND means the server is gone — no storage to clean."""
        storage = MagicMock()
        server = MagicMock()
        server.storage_devices = [storage]
        server.destroy.side_effect = UpCloudAPIError("SERVER_NOT_FOUND", "gone")

        _destroy_created_server_best_effort(server, "h")

        storage.destroy.assert_not_called()
