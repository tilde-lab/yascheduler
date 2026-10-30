"""Tests for Hetzner provider orphan-prevention and transient-error handling."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for hetzner create/delete orphan-prevention, transient-error propagation, and HetznerError.transient classification.
# SCOPE: hetzner.py create_node happy path, delete_node not_found/propagation; HetznerClient mocked.
# KEYWORDS: hetzner, orphan, transient, create, delete, unit
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from yascheduler.infra.cloud.cloud_configs import ConfigCloudHetzner
from yascheduler.infra.cloud.providers.hetzner import HetznerError
from yascheduler.shared.log import _NATIVE_KEYS


def _patch_hetzner_client(
    mock_client: MagicMock,
) -> AbstractContextManager[MagicMock]:
    """Patch HetznerClient so ``async with HetznerClient(...)`` yields mock_client."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client
    mock_cm.__aexit__.return_value = None
    return patch(
        "yascheduler.infra.cloud.providers.hetzner.HetznerClient",
        return_value=mock_cm,
    )


def extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Reconstruct structured fields from a log record."""
    return {k: getattr(record, k) for k in record.__dict__ if k not in _NATIVE_KEYS}


class LogCaptureHandler(logging.Handler):
    """Capture log records for assertion."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@pytest.fixture
def log_records() -> Generator[list[logging.LogRecord], None, None]:
    """Capture log records from the hetzner provider logger."""
    logger = logging.getLogger("yascheduler.infra.cloud.providers.hetzner")
    records: list[logging.LogRecord] = []
    handler = LogCaptureHandler(records)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _make_key() -> MagicMock:
    """Mock ASSHKey with the surface hetzner_create_node touches."""
    key = MagicMock()
    key.export_public_key.return_value = b"ssh-rsa AAAAB3..."
    return key


def _make_cfg() -> ConfigCloudHetzner:
    return ConfigCloudHetzner(token="test-token")


# =============================================================================
# HetznerError.transient
# =============================================================================


class TestHetznerErrorTransient:
    """HetznerError.transient — classify which errors are worth retrying."""

    def test_transport_error_transient(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import HetznerError

        assert HetznerError("net", status=None).transient is True

    def test_429_transient(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import HetznerError

        assert HetznerError("slow down", status=429).transient is True

    def test_5xx_transient(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import HetznerError

        for code in (500, 502, 503, 504):
            assert HetznerError("boom", status=code).transient is True

    def test_4xx_not_transient(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import HetznerError

        for code in (400, 403, 404, 409):
            assert HetznerError("bad", status=code).transient is False


# =============================================================================
# HetznerClient._request
# =============================================================================


class TestHetznerClientRequest:
    """HetznerClient._request — empty-body handling for DELETE 204 + error paths."""

    @staticmethod
    def _client_with_response(resp_mock: MagicMock):
        """Build a HetznerClient whose _session.request yields resp_mock.

        Bypass __init__ (which opens a real aiohttp.ClientSession).
        """
        from yascheduler.infra.cloud.providers.hetzner import HetznerClient

        client = HetznerClient.__new__(HetznerClient)
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp_mock)
        cm.__aexit__ = AsyncMock(return_value=None)
        session.request = MagicMock(return_value=cm)
        client._session = session
        return client

    @pytest.mark.asyncio
    async def test_delete_204_empty_body_returns_none(self) -> None:
        """Regression: Hetzner DELETE returns 204 No Content with an empty body.

        The old code called resp.json(), which raises ContentTypeError (status=204,
        a ClientResponseError subclass) on the empty octet-stream body, so every
        successful delete was misclassified as HetznerError(status=204) failure.
        """
        resp = MagicMock()
        resp.status = 204
        resp.text = AsyncMock(return_value="")
        # Emulate what aiohttp does on an empty 204 body if json() were called.
        # Built via __new__ to skip the RequestInfo/URL ceremony the typed
        # __init__ demands; the regression path only inspects .status/.message.
        cte = aiohttp.ContentTypeError.__new__(aiohttp.ContentTypeError)
        cte.status = 204
        cte.message = "Attempt to decode JSON with unexpected mimetype: "
        resp.json = AsyncMock(side_effect=cte)
        client = self._client_with_response(resp)

        # Must not raise (the regression raised HetznerError on the empty 204 body).
        await client.delete_server(42)
        # Empty-body path must not call json() at all.
        resp.json.assert_not_called()

    @pytest.mark.asyncio
    async def test_json_body_parsed(self) -> None:
        import json as _json

        from yascheduler.infra.cloud.providers.hetzner import HetznerClient

        payload = {
            "server": {
                "id": 1,
                "name": "n",
                "public_net": {"ipv4": {"ip": "1.2.3.4"}},
                "labels": {},
            }
        }
        resp = MagicMock()
        resp.status = 200
        resp.text = AsyncMock(return_value=_json.dumps(payload))
        client = self._client_with_response(resp)

        result = await client._request("GET", "/servers/1")

        assert result == payload
        assert isinstance(client, HetznerClient)

    @pytest.mark.parametrize(
        ("status", "body", "expected_status"),
        [
            pytest.param(500, "boom", 500, id="5xx-body"),
            # A 2xx with a non-JSON body (truncated chunk, CDN error page) is a
            # status-less transient error so callers retry instead of seeing a raw
            # JSONDecodeError escape _request unwrapped and losing the retry class.
            pytest.param(
                200,
                "<html>Gateway Timeout</html>",
                None,
                id="2xx-non-json-body",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_error_body_raises_transient_hetzner_error(
        self,
        status: int,
        body: str,
        expected_status: int | None,
    ) -> None:
        """An HTTP error or non-JSON body raises a transient HetznerError."""
        from yascheduler.infra.cloud.providers.hetzner import HetznerError

        resp = MagicMock()
        resp.status = status
        resp.text = AsyncMock(return_value=body)
        client = self._client_with_response(resp)

        with pytest.raises(HetznerError) as exc_info:
            await client._request("GET", "/servers/1")

        assert exc_info.value.status == expected_status
        assert exc_info.value.transient is True


class TestHetznerCreateNode:
    """hetzner_create_node — happy path, ssh-key reuse, DTO fields."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_dto(self) -> None:
        from yascheduler.infra.cloud.dto import CloudCreateNodeDTO
        from yascheduler.infra.cloud.providers.hetzner import hetzner_create_node

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 123, "name": "yakey-abc", "fingerprint": "aa:bb:cc"}
        )
        mock_client.create_server = AsyncMock(
            return_value={
                "id": 42,
                "name": "node-x",
                "public_net": {"ipv4": {"ip": "1.2.3.4"}},
            }
        )

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
        ):
            result = await hetzner_create_node(_make_cfg(), _make_key())

        assert isinstance(result, CloudCreateNodeDTO)
        assert result.external_id == "42"
        assert result.hostname == "1.2.3.4"
        assert result.username == "root"
        assert result.port == 22
        # POST /ssh_keys happened (key not found via GET), then POST /servers.
        mock_client.create_ssh_key.assert_awaited_once()
        mock_client.create_server.assert_awaited_once()
        create_kwargs = mock_client.create_server.call_args.kwargs
        assert create_kwargs["name"].startswith("yascheduler")
        assert create_kwargs["ssh_keys"] == [123]
        assert create_kwargs["user_data"].startswith("#cloud-config\n")

    @pytest.mark.asyncio
    async def test_existing_ssh_key_skips_post(self) -> None:
        """GET /ssh_keys finds the key → POST /ssh_keys skipped → POST /servers."""
        from yascheduler.infra.cloud.providers.hetzner import hetzner_create_node

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream(
                [{"id": 7, "name": "yakey-abc", "fingerprint": "aa:bb:cc:dd:ee"}]
            )
        )
        mock_client.create_ssh_key = AsyncMock()
        mock_client.create_server = AsyncMock(
            return_value={
                "id": 42,
                "name": "node-x",
                "public_net": {"ipv4": {"ip": "1.2.3.4"}},
            }
        )

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
        ):
            result = await hetzner_create_node(_make_cfg(), _make_key())

        assert result.external_id == "42"
        mock_client.create_ssh_key.assert_not_awaited()
        mock_client.create_server.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dto_carries_jump_config(self) -> None:
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudHetzner
        from yascheduler.infra.cloud.providers.hetzner import hetzner_create_node

        cfg = ConfigCloudHetzner(
            username="compute",
            token="t",
            jump_host="jump.example.com",
            jump_port=2222,
            jump_username="jumper",
        )
        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        mock_client.create_server = AsyncMock(
            return_value={
                "id": 7,
                "name": "n",
                "public_net": {"ipv4": {"ip": "5.6.7.8"}},
            }
        )

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
        ):
            result = await hetzner_create_node(cfg, _make_key())

        assert result.username == "compute"
        assert result.jump_host == "jump.example.com"
        assert result.jump_port == 2222
        assert result.jump_username == "jumper"

    @pytest.mark.asyncio
    async def test_user_data_has_root_and_non_root_users(self) -> None:
        import json

        from yascheduler.infra.cloud.cloud_configs import ConfigCloudHetzner
        from yascheduler.infra.cloud.providers.hetzner import hetzner_create_node

        cfg = ConfigCloudHetzner(username="compute", token="t")
        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        mock_client.create_server = AsyncMock(
            return_value={
                "id": 7,
                "name": "n",
                "public_net": {"ipv4": {"ip": "1.2.3.4"}},
            }
        )

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
        ):
            await hetzner_create_node(cfg, _make_key())

        user_data = mock_client.create_server.call_args.kwargs["user_data"]
        assert user_data.startswith("#cloud-config\n")
        payload = json.loads(user_data[len("#cloud-config\n") :])
        assert payload["users"] == [
            {"name": "root", "ssh_authorized_keys": ["ssh-rsa AAAAB3..."]},
            {
                "name": "compute",
                "ssh_authorized_keys": ["ssh-rsa AAAAB3..."],
                "groups": "sudo",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
            },
        ]
        # Non-root user gets passwordless sudo so setup_node can run `sudo apt-get ...`.
        non_root = [u for u in payload["users"] if u["name"] == "compute"]
        assert non_root[0]["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
        assert non_root[0]["groups"] == "sudo"

    @pytest.mark.asyncio
    async def test_invalid_create_response_raises_and_reconciles(self) -> None:
        """Malformed create-server response raises HetznerError; reconcile runs but finds no orphan."""
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_create_node,
        )

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        # 2xx body missing public_net → _is_api_server fails → HetznerError.
        # The client's create_server itself raises on invalid shape, so emulate
        # that: create_server raises HetznerError("Invalid create server response").
        mock_client.create_server = AsyncMock(
            side_effect=HetznerError("Invalid create server response")
        )
        # Reconcile lists by label and finds nothing → no delete.
        mock_client.get_servers = MagicMock(return_value=_servers_stream([]))
        mock_client.delete_server = AsyncMock()

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._RECONCILE_INTERVAL",
                0.0,
            ),
            pytest.raises(HetznerError, match="Invalid create server response"),
        ):
            await hetzner_create_node(_make_cfg(), _make_key())

        # Reconcile listed by label but found no orphan → no delete.
        assert mock_client.get_servers.called
        mock_client.delete_server.assert_not_awaited()

    @pytest.mark.parametrize(
        ("create_error", "match"),
        [
            pytest.param(
                HetznerError("Transport error: timeout"),
                "Transport error",
                id="transport-error",
            ),
            # CancelledError is a BaseException since Py3.8: a POST the server
            # accepted but the client never finished reading leaked a billable
            # orphan on every daemon stop mid-provision.
            pytest.param(
                asyncio.CancelledError(),
                None,
                id="cancelled-mid-create",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_create_failure_reconciles_orphan(
        self,
        create_error: BaseException,
        match: str | None,
    ) -> None:
        """A create that fails after accept reconciles the orphan by label and deletes it."""
        from yascheduler.infra.cloud.providers.hetzner import hetzner_create_node

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        mock_client.create_server = AsyncMock(side_effect=create_error)
        # Reconcile finds the orphan server by label.
        mock_client.get_servers = MagicMock(
            return_value=_servers_stream(
                [
                    {
                        "id": 999,
                        "name": "orphan",
                        "public_net": {"ipv4": {"ip": "1.2.3.4"}},
                        "labels": {},
                    }
                ]
            )
        )
        mock_client.delete_server = AsyncMock()
        # Orphan delete is verified gone via GET /servers/{id} → 404.
        mock_client.get_server = AsyncMock(side_effect=HetznerError("gone", status=404))

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._RECONCILE_INTERVAL",
                0.0,
            ),
            pytest.raises(type(create_error), match=match),
        ):
            await hetzner_create_node(_make_cfg(), _make_key())

        # Reconcile listed by label, found the orphan, deleted it.
        mock_client.get_servers.assert_called_once()
        mock_client.delete_server.assert_awaited_once_with(999)

    @pytest.mark.asyncio
    async def test_reconcile_listing_cancelled_logs_and_propagates_original(
        self,
    ) -> None:
        """CancelledError in the reconcile listing must not escape _reconcile_orphan_by_label.

        Regression (S5): the listing ``except Exception`` missed CancelledError
        (a BaseException since Py3.8). With no outer guard in
        hetzner_create_node, it masked the original create error AND skipped the
        RECONCILE_LOOKUP_FAILED manual-check log — a billable orphan with no
        warning. ``except BaseException`` honors the "Never raises" contract.
        """
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_create_node,
        )

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        # Original create failure (a real error, not cancellation).
        original = HetznerError("POST rejected", status=400)
        mock_client.create_server = AsyncMock(side_effect=original)
        # Listing cancelled mid-reconcile (e.g. daemon shutdown).
        mock_client.get_servers = MagicMock(side_effect=asyncio.CancelledError())

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._RECONCILE_INTERVAL",
                0.0,
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._RECONCILE_ATTEMPTS",
                1,
            ),
            pytest.raises(HetznerError, match="POST rejected"),
        ):
            await hetzner_create_node(_make_cfg(), _make_key())

        # The original create error propagated, not the listing's CancelledError.
        # Reconcile exhausted its attempts (CancelledError caught as transient).

    @pytest.mark.asyncio
    async def test_create_server_passes_reconcile_label(self) -> None:
        """create_server receives a unique reconcile-token label so orphans are matchable."""
        from yascheduler.infra.cloud.providers.hetzner import (
            _RECONCILE_LABEL_KEY,
            hetzner_create_node,
        )

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        mock_client.create_server = AsyncMock(
            return_value={
                "id": 42,
                "name": "node-x",
                "public_net": {"ipv4": {"ip": "1.2.3.4"}},
                "labels": {},
            }
        )

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
        ):
            await hetzner_create_node(_make_cfg(), _make_key())

        create_kwargs = mock_client.create_server.call_args.kwargs
        labels = create_kwargs["labels"]
        assert _RECONCILE_LABEL_KEY in labels
        token = labels[_RECONCILE_LABEL_KEY]
        assert token == create_kwargs["name"], "reconcile token must match server name"

    @pytest.mark.asyncio
    async def test_empty_ipv4_reconciles_and_raises(self) -> None:
        """An accepted create with an empty IPv4 is treated as a create failure.

        Hetzner assigns the IPv4 synchronously; an empty IP is unusable for SSH.
        Rather than return a DTO with hostname="" the orchestrator can never reach,
        reconcile the created server by label and re-raise so no billable orphan
        lingers and the allocator retries.
        """
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_create_node,
        )

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(return_value=_ssh_key_stream([]))
        mock_client.create_ssh_key = AsyncMock(
            return_value={"id": 1, "name": "yakey", "fingerprint": "aa"}
        )
        # Accepted create with an empty IPv4 — server exists and bills.
        mock_client.create_server = AsyncMock(
            return_value={
                "id": 77,
                "name": "orphan-ip",
                "public_net": {"ipv4": {"ip": ""}},
                "labels": {},
            }
        )
        # Reconcile finds the orphan by label and deletes it.
        mock_client.get_servers = MagicMock(
            return_value=_servers_stream(
                [
                    {
                        "id": 77,
                        "name": "orphan-ip",
                        "public_net": {"ipv4": {"ip": ""}},
                        "labels": {},
                    }
                ]
            )
        )
        mock_client.delete_server = AsyncMock()
        mock_client.get_server = AsyncMock(side_effect=HetznerError("gone", status=404))

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner.get_key_name",
                return_value="yakey-abc",
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._RECONCILE_INTERVAL",
                0.0,
            ),
            pytest.raises(HetznerError, match="empty IPv4"),
        ):
            await hetzner_create_node(_make_cfg(), _make_key())

        # The created server was reconciled (deleted) so it does not bill.
        mock_client.delete_server.assert_awaited_once_with(77)


def _ssh_key_stream(items: list):
    """Build an async generator mimicking HetznerClient.get_ssh_keys."""

    async def gen():
        for key in items:
            yield key

    return gen()


def _servers_stream(items: list):
    """Build an async generator mimicking HetznerClient.get_servers."""

    async def gen():
        for server in items:
            yield server

    return gen()


# =============================================================================
# _reconcile_orphan_by_label
# =============================================================================


class TestReconcileOrphanByLabel:
    """_reconcile_orphan_by_label — phase-2 retries delete+verify on a known id.

    Regression: the old loop re-listed by label after a failed verify, so an
    accepted-but-async DELETE could return an empty list (propagation lag) and
    the reconcile falsely concluded "no orphan" while the server still billed.
    Once an orphan id is found, phase 2 retries _delete_and_verify directly on
    that id and never re-lists.
    """

    @pytest.mark.asyncio
    async def test_verify_timeout_then_404_retries_on_same_id(self) -> None:
        """First verify times out (server still present), retry delete hits 404.

        The orphan id is known after phase 1; phase 2 must retry _delete_and_verify
        on that id rather than re-listing. delete_server is called twice (once per
        phase-2 attempt); get_servers is called once (phase 1 only).
        """
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_servers = MagicMock(
            return_value=_servers_stream(
                [
                    {
                        "id": 1234,
                        "name": "orphan",
                        "public_net": {"ipv4": {"ip": "1.2.3.4"}},
                        "labels": {},
                    }
                ]
            )
        )
        # First delete accepted, but GET keeps returning 200 (verify times out);
        # second delete hits 404 (already gone) → success.
        mock_client.delete_server = AsyncMock(
            side_effect=[None, HetznerError("nf", status=404)]
        )
        # get_server returns a valid server first (no 404 → keep polling →
        # _VERIFY_TIMEOUT), then is not reached on the second attempt (404 on DELETE).
        valid_server = {
            "id": 1234,
            "name": "orphan",
            "public_net": {"ipv4": {"ip": "1.2.3.4"}},
            "labels": {},
        }
        mock_client.get_server = AsyncMock(return_value=valid_server)

        with (
            patch(
                "yascheduler.infra.cloud.providers.hetzner._RECONCILE_INTERVAL",
                0.0,
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._VERIFY_TIMEOUT",
                0.0,
            ),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._VERIFY_INTERVAL",
                0.0,
            ),
        ):
            await _reconcile_orphan_by_label(mock_client, "orphan")

        # Phase 1 lists once; phase 2 never re-lists.
        mock_client.get_servers.assert_called_once()
        # Two delete attempts on the SAME id, not a re-list-and-match.
        assert mock_client.delete_server.await_count == 2
        assert all(
            call.args == (1234,) for call in mock_client.delete_server.call_args_list
        )

    @pytest.mark.asyncio
    async def test_no_orphan_does_not_delete(self) -> None:
        """Label lists empty → no delete, warning logged, never raises."""
        from yascheduler.infra.cloud.providers.hetzner import (
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_servers = MagicMock(return_value=_servers_stream([]))
        mock_client.delete_server = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.hetzner._RECONCILE_INTERVAL",
            0.0,
        ):
            await _reconcile_orphan_by_label(mock_client, "none")

        mock_client.delete_server.assert_not_awaited()


# =============================================================================
# hetzner_delete_node
# =============================================================================


class TestHetznerDeleteNode:
    """hetzner_delete_node — invalid id, happy path, idempotent not_found, propagation."""

    @pytest.mark.asyncio
    async def test_invalid_external_id_raises(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_delete_node,
        )

        with pytest.raises(HetznerError, match="Invalid Hetzner server id"):
            await hetzner_delete_node(_make_cfg(), external_id="not-a-number")

    @pytest.mark.asyncio
    async def test_happy_path_delete(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_delete_node,
        )

        mock_client = MagicMock()
        mock_client.delete_server = AsyncMock()
        # Accepted DELETE is verified gone via GET /servers/{id} → 404.
        mock_client.get_server = AsyncMock(side_effect=HetznerError("gone", status=404))

        with _patch_hetzner_client(mock_client):
            await hetzner_delete_node(_make_cfg(), external_id="42")

        mock_client.delete_server.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_not_found_is_idempotent(self) -> None:
        """DELETE returning 404 is a no-op, not an error."""
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_delete_node,
        )

        mock_client = MagicMock()
        mock_client.delete_server = AsyncMock(
            side_effect=HetznerError("not found", status=404)
        )

        with _patch_hetzner_client(mock_client):
            # Should not raise.
            await hetzner_delete_node(_make_cfg(), external_id="152213839")

        mock_client.delete_server.assert_awaited_once_with(152213839)

    @pytest.mark.asyncio
    async def test_permanent_4xx_raises(self) -> None:
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_delete_node,
        )

        mock_client = MagicMock()
        mock_client.delete_server = AsyncMock(
            side_effect=HetznerError("forbidden", status=403)
        )

        with (
            _patch_hetzner_client(mock_client),
            pytest.raises(HetznerError),
        ):
            await hetzner_delete_node(_make_cfg(), external_id="42")

    @pytest.mark.asyncio
    async def test_transient_5xx_propagates(self) -> None:
        """Transient errors propagate so the orchestrator retries on the next cycle."""
        from yascheduler.infra.cloud.providers.hetzner import (
            HetznerError,
            hetzner_delete_node,
        )

        mock_client = MagicMock()
        mock_client.delete_server = AsyncMock(
            side_effect=HetznerError("boom", status=500)
        )

        with (
            _patch_hetzner_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.hetzner._DELETE_INTERVAL",
                0.0,
            ),
            pytest.raises(HetznerError) as exc_info,
        ):
            await hetzner_delete_node(_make_cfg(), external_id="42")

        assert exc_info.value.transient is True
