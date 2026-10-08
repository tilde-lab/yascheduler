"""Tests for Vultr provider module."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for Vultr provider: exception hierarchy, cloud-init
#          user-data, SSH key registration, create/delete
#          orchestration, and log-marker emission.
# SCOPE: vultr.py module-level behavior; VultrClient and SSH helpers via mocks.
# KEYWORDS: vultr, provider, unit, exceptions, public-key, cloud-init, ssh
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Generator
from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yascheduler.shared.log import _NATIVE_KEYS


def _bm_stream(items: list, exc: Exception | None = None):
    """Build an async generator mimicking VultrClient.get_bare_metals.

    If exc is set, the iterator raises on first __anext__ (transient listing
    failure). Otherwise yields items one by one.
    """

    async def gen():
        if exc is not None:
            raise exc
        for bm in items:
            yield bm

    return gen()


async def _collect(agen):
    """Collect all items from an async generator into a list."""
    out: list = []
    async for item in agen:
        out.append(item)
    return out


def _ssh_key_stream(items: list):
    """Build an async generator mimicking VultrClient.get_ssh_keys."""

    async def gen():
        for key in items:
            yield key

    return gen()


class LogCaptureHandler(logging.Handler):
    """Capture log records for assertion."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@pytest.fixture
def log_records() -> Generator[list[logging.LogRecord], None, None]:
    """Capture log records from the vultr provider logger."""
    logger = logging.getLogger("yascheduler.infra.cloud.providers.vultr")
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


def extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Reconstruct structured fields from a log record."""
    return {k: getattr(record, k) for k in record.__dict__ if k not in _NATIVE_KEYS}


def _patch_vultr_client(
    mock_client: MagicMock,
) -> AbstractContextManager[MagicMock]:
    """Patch VultrClient so ``async with VultrClient(...)`` yields mock_client."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client
    mock_cm.__aexit__.return_value = None
    return patch(
        "yascheduler.infra.cloud.providers.vultr.VultrClient",
        return_value=mock_cm,
    )


# =============================================================================
# Exception hierarchy
# =============================================================================


class TestExceptionHierarchy:
    """Vultr exception hierarchy: APIError subclass relationship."""

    def test_api_error_is_exception(self) -> None:
        """APIError subclasses Exception."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        assert issubclass(APIError, Exception)

    def test_api_error_carries_message(self) -> None:
        """str(APIError(msg)) == msg."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        assert str(APIError("boom")) == "boom"

    def test_api_error_default_status_is_none(self) -> None:
        """APIError without explicit status has status=None (transport-level)."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        err = APIError("boom")
        assert err.status is None

    def test_api_error_5xx_is_transient(self) -> None:
        """5xx APIError is transient — worth retrying."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        assert APIError("HTTP 500", status=500).transient is True
        assert APIError("HTTP 503", status=503).transient is True

    def test_api_error_4xx_is_not_transient(self) -> None:
        """4xx APIError (except 429) is permanent — not retried."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        assert APIError("HTTP 404", status=404).transient is False
        assert APIError("HTTP 400", status=400).transient is False

    def test_api_error_429_is_transient(self) -> None:
        """429 rate-limit is transient — worth retrying."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        assert APIError("HTTP 429: Too Many Requests", status=429).transient is True

    def test_api_error_transport_failure_is_transient(self) -> None:
        """APIError with status=None (transport failure) is transient."""
        from yascheduler.infra.cloud.providers.vultr import APIError

        assert APIError("HTTP request failed: timeout").transient is True


# =============================================================================
# build_baremetal_user_data
# =============================================================================


class TestBuildBaremetalUserData:
    """build_baremetal_user_data: cloud-init payload shape per need_raid + cloud_config."""

    PUB = "ssh-rsa AAAAB3NzaC1yc2E= test"

    def test_returns_cloud_config_prefix(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=True)
        assert out.startswith("#cloud-config\n")

    def test_strips_to_valid_json(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=True)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert "runcmd" in data
        assert "packages" in data

    def test_need_raid_true_includes_mdadm_commands(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=True)
        assert "mdadm" in out
        assert "mdadm --create" in out
        assert "/dev/shm" in out

    def test_need_raid_false_skips_raid(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=False)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert "mdadm" not in data["packages"]
        runcmd_str = " ".join(data["runcmd"])
        assert "mdadm --create" not in runcmd_str
        assert "/dev/shm" not in runcmd_str

    def test_none_cloud_config_no_package_upgrade(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=True)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert data["package_upgrade"] is False

    def test_cloud_config_packages_merged(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        cc = CloudInitConfig(packages=["foo", "bar"], package_upgrade=True)
        out = build_baremetal_user_data("root", self.PUB, cc, need_raid=True)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert "foo" in data["packages"]
        assert "bar" in data["packages"]
        assert data["package_upgrade"] is True

    def test_engine_packages_deduped_after_base(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        # "git" is already in base packages; passing it via cloud_config must not duplicate.
        cc = CloudInitConfig(packages=["git", "custom-pkg"])
        out = build_baremetal_user_data("root", self.PUB, cc, need_raid=True)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert data["packages"].count("git") == 1
        assert "custom-pkg" in data["packages"]

    def test_engine_bootcmd_propagated(self) -> None:
        """cloud_config.bootcmd is rendered (previously vultr ignored it)."""
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        cc = CloudInitConfig(bootcmd=(["echo", "engine-boot"],))
        out = build_baremetal_user_data("root", self.PUB, cc, need_raid=False)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert data["bootcmd"] == [["echo", "engine-boot"]]

    def test_root_username_adds_root_authorized_keys(self) -> None:
        """username='root' → users has root with the key."""
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=False)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert data["users"] == [{"name": "root", "ssh_authorized_keys": [self.PUB]}]

    def test_non_root_username_adds_root_and_user(self) -> None:
        """username!='root' → users has root AND the custom user, both with the key."""
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("myuser", self.PUB, None, need_raid=False)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        assert data["users"] == [
            {"name": "root", "ssh_authorized_keys": [self.PUB]},
            {
                "name": "myuser",
                "ssh_authorized_keys": [self.PUB],
                "groups": "sudo",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
            },
        ]

    def test_non_root_user_has_passwordless_sudo(self) -> None:
        """Custom user gets passwordless sudo so setup_node can run `sudo apt-get ...`."""
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("myuser", self.PUB, None, need_raid=False)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        non_root = [u for u in data["users"] if u["name"] == "myuser"]
        assert len(non_root) == 1
        assert non_root[0]["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
        assert non_root[0]["groups"] == "sudo"

    def test_non_root_user_gets_chown_data(self) -> None:
        """Non-root ssh user can't write root-owned /data; runcmd chowns it."""
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("myuser", self.PUB, None, need_raid=False)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        runcmd_str = " ".join(data["runcmd"])
        assert "chown -R myuser:myuser /data" in runcmd_str

    def test_root_user_no_chown_data(self) -> None:
        """Root already owns /data; no chown emitted."""
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("root", self.PUB, None, need_raid=True)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        runcmd_str = " ".join(data["runcmd"])
        assert "chown" not in runcmd_str

    def test_non_root_chown_after_raid_mount(self) -> None:
        """chown /data must come after the RAID mount so it owns the mounted fs."""
        from yascheduler.infra.cloud.providers.vultr import build_baremetal_user_data

        out = build_baremetal_user_data("myuser", self.PUB, None, need_raid=True)
        data = json.loads(out.removeprefix("#cloud-config\n"))
        runcmd = data["runcmd"]
        mount_idx = next(i for i, c in enumerate(runcmd) if "mount /data" in c)
        chown_idx = next(i for i, c in enumerate(runcmd) if "chown -R myuser" in c)
        assert chown_idx > mount_idx


# =============================================================================
# VultrClient.get_ssh_keys
# =============================================================================


class TestVultrClientGetSshKeys:
    """VultrClient.get_ssh_keys: GET /ssh-keys shape validation + pagination."""

    @pytest.mark.asyncio
    async def test_single_page_returns_ssh_keys_list(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client,
            "_request",
            AsyncMock(
                return_value={
                    "ssh_keys": [
                        {"id": "k1", "ssh_key": "ssh-rsa AAAA= one"},
                        {"id": "k2", "ssh_key": "ssh-rsa BBBB= two"},
                    ]
                },
            ),
        ) as mock_request:
            result = await _collect(client.get_ssh_keys())

        assert result == [
            {"id": "k1", "ssh_key": "ssh-rsa AAAA= one"},
            {"id": "k2", "ssh_key": "ssh-rsa BBBB= two"},
        ]
        mock_request.assert_awaited_once_with(
            "GET", "ssh-keys", params={"per_page": 500}
        )

    @pytest.mark.asyncio
    async def test_custom_per_page_forwarded(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client, "_request", AsyncMock(return_value={"ssh_keys": []})
        ) as mock_request:
            await _collect(client.get_ssh_keys(per_page=100))

        mock_request.assert_awaited_once_with(
            "GET", "ssh-keys", params={"per_page": 100}
        )

    @pytest.mark.asyncio
    async def test_follows_cursor_until_no_next(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        page1 = {
            "ssh_keys": [{"id": "k1", "ssh_key": "ssh-rsa AAAA= one"}],
            "meta": {"links": {"next": "cursor-abc"}},
        }
        page2 = {
            "ssh_keys": [{"id": "k2", "ssh_key": "ssh-rsa BBBB= two"}],
            "meta": {"links": {"next": "cursor-def"}},
        }
        page3 = {"ssh_keys": [{"id": "k3", "ssh_key": "ssh-rsa CCCC= three"}]}
        snapshots: list[dict] = []

        async def fake_request(method, path, params=None, data=None):
            snapshots.append(dict(params) if params else {})
            return [page1, page2, page3][len(snapshots) - 1]

        with patch.object(client, "_request", side_effect=fake_request) as mock_request:
            result = await _collect(client.get_ssh_keys())

        assert [k["id"] for k in result] == ["k1", "k2", "k3"]
        assert mock_request.call_count == 3
        # page 1: no cursor; page 2: cursor=cursor-abc; page 3: cursor=cursor-def.
        assert snapshots[0] == {"per_page": 500}
        assert snapshots[1] == {"per_page": 500, "cursor": "cursor-abc"}
        assert snapshots[2] == {"per_page": 500, "cursor": "cursor-def"}

    @pytest.mark.asyncio
    async def test_missing_meta_stops_after_first_page(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client,
            "_request",
            AsyncMock(
                return_value={
                    "ssh_keys": [{"id": "k1", "ssh_key": "ssh-rsa AAAA= one"}]
                }
            ),
        ) as mock_request:
            result = await _collect(client.get_ssh_keys())

        assert [k["id"] for k in result] == ["k1"]
        mock_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_shape_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client, "_request", AsyncMock(return_value={"not_ssh_keys": []})
            ),
            pytest.raises(APIError, match="Invalid SSH keys list response"),
        ):
            await _collect(client.get_ssh_keys())

    @pytest.mark.asyncio
    async def test_missing_id_in_entry_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client,
                "_request",
                AsyncMock(
                    return_value={
                        "ssh_keys": [{"ssh_key": "ssh-rsa AAAA= one"}]
                    },  # no id
                ),
            ),
            pytest.raises(APIError, match="Invalid SSH keys list response"),
        ):
            await _collect(client.get_ssh_keys())

    @pytest.mark.asyncio
    async def test_missing_ssh_key_in_entry_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client,
                "_request",
                AsyncMock(return_value={"ssh_keys": [{"id": "k1"}]}),
            ),
            pytest.raises(APIError, match="Invalid SSH keys list response"),
        ):
            await _collect(client.get_ssh_keys())


# =============================================================================
# VultrClient.create_ssh_key
# =============================================================================


class TestVultrClientCreateSshKey:
    """VultrClient.create_ssh_key: POST /ssh-keys shape validation."""

    @pytest.mark.asyncio
    async def test_returns_ssh_key_id(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client,
            "_request",
            AsyncMock(
                return_value={
                    "ssh_key": {"id": "new-id", "ssh_key": "ssh-rsa AAAA= test"}
                }
            ),
        ) as mock_request:
            result = await client.create_ssh_key("my-key", "ssh-rsa AAAA= test")

        assert result == "new-id"
        mock_request.assert_awaited_once_with(
            "POST",
            "ssh-keys",
            data={"name": "my-key", "ssh_key": "ssh-rsa AAAA= test"},
        )

    @pytest.mark.asyncio
    async def test_response_without_ssh_key_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(client, "_request", AsyncMock(return_value={})),
            pytest.raises(APIError, match="Cannot create SSH key"),
        ):
            await client.create_ssh_key("my-key", "ssh-rsa AAAA= test")

    @pytest.mark.asyncio
    async def test_response_ssh_key_without_id_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(client, "_request", AsyncMock(return_value={"ssh_key": {}})),
            pytest.raises(APIError, match="Cannot create SSH key"),
        ):
            await client.create_ssh_key("my-key", "ssh-rsa AAAA= test")


# =============================================================================
# VultrClient.delete_ssh_key
# =============================================================================


class TestVultrClientDeleteSshKey:
    """VultrClient.delete_ssh_key: DELETE /ssh-keys/{id} passthrough."""

    @pytest.mark.asyncio
    async def test_deletes_by_id(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client,
            "_request",
            AsyncMock(return_value=None),  # DELETE 204 -> empty body
        ) as mock_request:
            await client.delete_ssh_key("key-id")

        mock_request.assert_awaited_once_with("DELETE", "ssh-keys/key-id")


# =============================================================================
# VultrClient.create_bare_metal
# =============================================================================


class TestVultrClientCreateBareMetal:
    """VultrClient.create_bare_metal: POST /bare-metals shape validation."""

    @pytest.mark.asyncio
    async def test_returns_bare_metal_entity(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        bm_resp = {"bare_metal": {"id": "inst-1", "label": "test"}}
        with patch.object(
            client, "_request", AsyncMock(return_value=bm_resp)
        ) as mock_request:
            result = await client.create_bare_metal(
                region="ams",
                plan="vbm-24c-256gb-amd",
                os_id=2136,
                label="test",
                hostname="test",
                sshkey_id=["k1"],
                user_data="data",
                enable_ipv6=True,
            )

        assert result == {"id": "inst-1", "label": "test"}
        mock_request.assert_awaited_once_with(
            "POST",
            "bare-metals",
            data={
                "region": "ams",
                "plan": "vbm-24c-256gb-amd",
                "os_id": 2136,
                "label": "test",
                "hostname": "test",
                "sshkey_id": ["k1"],
                "user_data": "data",
                "enable_ipv6": True,
            },
        )

    @pytest.mark.asyncio
    async def test_response_without_bare_metal_key_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client, "_request", AsyncMock(return_value={"not_bare_metal": {}})
            ),
            pytest.raises(APIError, match="Invalid create Bare Metal response"),
        ):
            await client.create_bare_metal(
                region="ams",
                plan="p",
                os_id=1,
                label="t",
                hostname="t",
                sshkey_id=["k"],
                user_data="d",
                enable_ipv6=True,
            )

    @pytest.mark.asyncio
    async def test_response_bare_metal_without_id_raises(self) -> None:
        """create_bare_metal rejects a bare_metal without id — id is required
        by VultrBareMetal and validated via _is_bare_metal_resp."""
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client,
                "_request",
                AsyncMock(return_value={"bare_metal": {"label": "no-id"}}),
            ),
            pytest.raises(APIError, match="Invalid create Bare Metal response"),
        ):
            await client.create_bare_metal(
                region="ams",
                plan="p",
                os_id=1,
                label="t",
                hostname="t",
                sshkey_id=["k"],
                user_data="d",
                enable_ipv6=True,
            )


# =============================================================================
# VultrClient.get_bare_metals
# =============================================================================


class TestVultrClientGetBareMetals:
    """VultrClient.get_bare_metals: GET /bare-metals shape validation + pagination."""

    @pytest.mark.asyncio
    async def test_single_page_returns_bare_metals_list(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client,
            "_request",
            AsyncMock(
                return_value={
                    "bare_metals": [
                        {"id": "bm-1", "label": "n1"},
                        {"id": "bm-2", "label": "n2"},
                    ]
                },
            ),
        ) as mock_request:
            result = await _collect(client.get_bare_metals())

        assert result == [
            {"id": "bm-1", "label": "n1"},
            {"id": "bm-2", "label": "n2"},
        ]
        mock_request.assert_awaited_once_with(
            "GET", "bare-metals", params={"per_page": 500}
        )

    @pytest.mark.asyncio
    async def test_custom_per_page_forwarded(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client, "_request", AsyncMock(return_value={"bare_metals": []})
        ) as mock_request:
            await _collect(client.get_bare_metals(per_page=50))

        mock_request.assert_awaited_once_with(
            "GET", "bare-metals", params={"per_page": 50}
        )

    @pytest.mark.asyncio
    async def test_follows_cursor_until_no_next(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        page1 = {
            "bare_metals": [{"id": "bm-1", "label": "n1"}],
            "meta": {"links": {"next": "cursor-abc"}},
        }
        page2 = {
            "bare_metals": [{"id": "bm-2", "label": "n2"}],
            "meta": {"links": {"next": "cursor-def"}},
        }
        page3 = {"bare_metals": [{"id": "bm-3", "label": "n3"}]}
        snapshots: list[dict] = []

        async def fake_request(method, path, params=None, data=None):
            snapshots.append(dict(params) if params else {})
            return [page1, page2, page3][len(snapshots) - 1]

        with patch.object(client, "_request", side_effect=fake_request) as mock_request:
            result = await _collect(client.get_bare_metals())

        assert [bm["id"] for bm in result] == ["bm-1", "bm-2", "bm-3"]
        assert mock_request.call_count == 3
        assert snapshots[0] == {"per_page": 500}
        assert snapshots[1] == {"per_page": 500, "cursor": "cursor-abc"}
        assert snapshots[2] == {"per_page": 500, "cursor": "cursor-def"}

    @pytest.mark.asyncio
    async def test_missing_meta_stops_after_first_page(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        with patch.object(
            client,
            "_request",
            AsyncMock(return_value={"bare_metals": [{"id": "bm-1"}]}),
        ) as mock_request:
            result = await _collect(client.get_bare_metals())

        assert [bm["id"] for bm in result] == ["bm-1"]
        mock_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_shape_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client, "_request", AsyncMock(return_value={"not_bare_metals": []})
            ),
            pytest.raises(APIError, match="Invalid bare-metals list response"),
        ):
            await _collect(client.get_bare_metals())

    @pytest.mark.asyncio
    async def test_entry_without_id_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, VultrClient

        client = VultrClient.__new__(VultrClient)
        with (
            patch.object(
                client,
                "_request",
                AsyncMock(
                    return_value={"bare_metals": [{"label": "no-id"}]},  # no id
                ),
            ),
            pytest.raises(APIError, match="Invalid bare-metals list response"),
        ):
            await _collect(client.get_bare_metals())


# =============================================================================
# VultrClient._request
# =============================================================================


class _FakeResponse:
    """Minimal aiohttp response stub: status + async text()/aenter/aexit."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """Records the last request and returns a canned response."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def request(self, method: str, path: str, **kwargs: object) -> _FakeResponse:
        self.last = {"method": method, "path": path, **kwargs}
        return self._response


class TestVultrClientRequest:
    """_request: empty body (204 / empty 200) → None, JSON body → parsed.

    Regression: Vultr DELETE returns 204 No Content; resp.json() raised
    ContentTypeError, turning every successful delete into a bogus APIError.
    """

    @pytest.mark.asyncio
    async def test_204_empty_body_returns_none(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        client._session = _FakeSession(_FakeResponse(204, ""))  # type: ignore[assignment]

        result = await client._request("DELETE", "/bare-metals/inst-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_200_empty_body_returns_none(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        client._session = _FakeSession(_FakeResponse(200, ""))  # type: ignore[assignment]

        assert await client._request("DELETE", "/bare-metals/inst-1") is None

    @pytest.mark.asyncio
    async def test_json_body_parsed(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import VultrClient

        client = VultrClient.__new__(VultrClient)
        client._session = _FakeSession(  # type: ignore[assignment]
            _FakeResponse(200, '{"bare_metal": {"id": "inst-1"}}')
        )

        result = await client._request("GET", "/bare-metals/inst-1")

        assert result == {"bare_metal": {"id": "inst-1"}}


class TestGetSshKeyId:
    """get_ssh_key_id: public-key match reuses key, miss uploads new one."""

    @pytest.mark.asyncio
    async def test_public_key_match_without_fingerprint_reuses_existing_id(
        self,
    ) -> None:
        from yascheduler.infra.cloud.providers.vultr import get_ssh_key_id

        pubkey = "ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = pubkey.encode()

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream(
                [{"id": "existing-id", "ssh_key": pubkey}],
            ),
        )
        mock_client.create_ssh_key = AsyncMock()

        result = await get_ssh_key_id(mock_client, mock_key)

        assert result == "existing-id"
        mock_client.get_ssh_keys.assert_called_once()
        mock_client.create_ssh_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_public_key_match_ignores_trailing_newline(self) -> None:
        """Regression: asyncssh export ends with a newline; Vultr may strip it.

        Pre-fix, the exact-equality comparison missed the stored body and
        uploaded a duplicate key on every allocation.
        """
        from yascheduler.infra.cloud.providers.vultr import get_ssh_key_id

        stored = "ssh-rsa AAAAB3NzaC1yc2E= test"  # as Vultr returns it
        mock_key = MagicMock()
        # asyncssh export_public_key("openssh") appends a trailing newline.
        mock_key.export_public_key.return_value = (stored + "\n").encode()

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream(
                [{"id": "existing-id", "ssh_key": stored}],
            ),
        )
        mock_client.create_ssh_key = AsyncMock()

        result = await get_ssh_key_id(mock_client, mock_key)

        assert result == "existing-id"
        mock_client.create_ssh_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stored_key_with_trailing_newline_matches(self) -> None:
        """The match must ignore surrounding whitespace on the stored side too."""
        from yascheduler.infra.cloud.providers.vultr import get_ssh_key_id

        exported = "ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = exported.encode()

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream(
                [{"id": "existing-id", "ssh_key": exported + "\n"}],
            ),
        )
        mock_client.create_ssh_key = AsyncMock()

        result = await get_ssh_key_id(mock_client, mock_key)

        assert result == "existing-id"
        mock_client.create_ssh_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_upload_stores_stripped_body(self) -> None:
        """Upload must store the same form the match compares.

        The exported key carries a trailing newline; if it were uploaded
        as-is, the stored form would differ from the compared form forever.
        """
        from yascheduler.infra.cloud.providers.vultr import get_ssh_key_id

        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test\n"

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream([]),  # no existing keys
        )
        mock_client.create_ssh_key = AsyncMock(return_value="new-id")

        result = await get_ssh_key_id(mock_client, mock_key)

        assert result == "new-id"
        mock_client.create_ssh_key.assert_awaited_once()
        call = mock_client.create_ssh_key.await_args
        assert call is not None
        name, uploaded = call.args
        assert name  # any non-empty key name
        assert uploaded == "ssh-rsa AAAAB3NzaC1yc2E= test"

    @pytest.mark.asyncio
    async def test_concurrent_allocations_create_one_key(self) -> None:
        """Two concurrent get_ssh_key_id calls must not race a duplicate.

        The Vultr adapter runs with op_limit=2: two allocations can be in
        flight at once. Without serialization, both list before either
        uploads, both miss, and both upload their own copy.
        """
        from yascheduler.infra.cloud.providers.vultr import get_ssh_key_id

        exported = "ssh-rsa AAAAB3NzaC1yc2E= test"
        shared: list = []
        state = {"creates": 0}

        def make_client() -> MagicMock:
            client = MagicMock()

            def get_keys():
                async def gen():
                    snapshot = list(shared)  # listing result fixed at request time
                    await asyncio.sleep(0)  # HTTP in flight — both racers get here
                    for key in snapshot:
                        yield key

                return gen()

            client.get_ssh_keys = get_keys

            async def create_key(name, pub_key):
                state["creates"] += 1
                new_id = f"id-{state['creates']}"
                shared.append({"id": new_id, "ssh_key": pub_key})
                return new_id

            client.create_ssh_key = create_key
            return client

        mock_key = MagicMock()
        mock_key.export_public_key.return_value = exported.encode()

        results = await asyncio.gather(
            get_ssh_key_id(make_client(), mock_key),
            get_ssh_key_id(make_client(), mock_key),
        )

        assert state["creates"] == 1
        assert results == ["id-1", "id-1"]

    @pytest.mark.asyncio
    async def test_no_match_uploads_and_returns_new_id(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import get_ssh_key_id

        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream([]),  # no existing keys
        )
        mock_client.create_ssh_key = AsyncMock(return_value="new-id")

        result = await get_ssh_key_id(mock_client, mock_key)
        assert result == "new-id"
        mock_client.get_ssh_keys.assert_called_once()
        mock_client.create_ssh_key.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_without_id_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import APIError, get_ssh_key_id

        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"

        mock_client = MagicMock()
        mock_client.get_ssh_keys = MagicMock(
            return_value=_ssh_key_stream([]),
        )
        # create_ssh_key raises APIError when POST response has no ssh_key.id
        mock_client.create_ssh_key = AsyncMock(side_effect=APIError("no id"))

        with pytest.raises(APIError):
            await get_ssh_key_id(mock_client, mock_key)


# =============================================================================
# vultr_create_node
# =============================================================================


class TestVultrCreateNode:
    """vultr_create_node: full orchestration with mocked client and SSH helpers."""

    @pytest.mark.asyncio
    async def test_returns_dto_with_correct_fields(self) -> None:
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import vultr_create_node

        cfg = ConfigCloudVultr(
            api_key="test-key",
            jump_host="jump.example.com",
            jump_port=2222,
            jump_username="jumpuser",
        )
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            return_value={"id": "inst-1", "status": "active", "main_ip": "1.2.3.4"},
        )
        mock_client.delete_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
        ):
            result = await vultr_create_node(cfg, mock_key)

        assert result.external_id == "inst-1"
        assert result.hostname == "1.2.3.4"
        assert result.username == "root"
        assert result.jump_host == "jump.example.com"
        assert result.jump_port == 2222
        assert result.jump_username == "jumpuser"

    @pytest.mark.asyncio
    async def test_label_prefix_sourced_from_cfg_label(self) -> None:
        """vultr_create_node passes cfg.label to get_rnd_name so an operator-set
        label prefix is honored — consistent with az/hetzner/upcloud/vastai.

        Regression: the prefix was hardcoded to "yascheduler", so a config
        label like ``myteam`` was ignored and every instance got a
        ``yascheduler-...`` label.
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key", label="myteam")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            return_value={"id": "inst-1", "status": "active", "main_ip": "1.2.3.4"},
        )
        mock_client.delete_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="myteam-abcdefgh",
            ) as rnd_mock,
        ):
            await vultr_create_node(cfg, mock_key)

        rnd_mock.assert_called_once_with("myteam")

    @pytest.mark.asyncio
    async def test_create_node_does_not_poll_ssh_readiness(self) -> None:
        """SSH readiness is delegated to the cloud manager's setup connect
        retry — vultr_create_node returns once the instance is active with an
        IP, without polling the SSH port or key-based auth itself."""
        import yascheduler.infra.cloud.providers.vultr as vultr_mod

        assert not hasattr(vultr_mod, "_wait_ssh_port")
        assert not hasattr(vultr_mod, "_check_ssh_auth")
        assert not hasattr(vultr_mod, "asyncssh")

    @pytest.mark.asyncio
    async def test_post_without_instance_id_raises_apierror(self) -> None:
        """create_bare_metal itself rejects a 2xx response without a usable
        bare_metal.id (shape validation via _is_bare_metal_resp). The
        reconcile-by-label path runs before the error propagates so a
        possibly-created instance is not orphaned."""
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        # create_bare_metal raises APIError on invalid shape (no bare_metal.id)
        mock_client.create_bare_metal = AsyncMock(
            side_effect=APIError("Invalid Bare Metal create response")
        )
        mock_client.get_bare_metals = MagicMock(
            return_value=_bm_stream([]),  # reconcile GET list: no match
        )
        mock_client.delete_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(APIError),
        ):
            await vultr_create_node(cfg, mock_key)

        # Reconcile ran (best-effort) but found no orphan — no DELETE.
        mock_client.delete_bare_metal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_active_raises_apierror(self) -> None:
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import (
            POLL_TIMEOUT,
            APIError,
            vultr_create_node,
        )

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[
                {"id": "inst-1", "status": "pending", "main_ip": "0.0.0.0"},
                APIError("HTTP 404", status=404),  # verify: gone
            ],
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)

        loop = MagicMock()
        # create-poll consumes 3 (deadline, while-check, while-check-exit);
        # verify consumes 2 (deadline, while-check) then 404 returns.
        loop.time = MagicMock(side_effect=[0, 0, POLL_TIMEOUT + 1, 0, 1])

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(APIError),
        ):
            await vultr_create_node(cfg, mock_key)

        # Instance was created → cleanup MUST have deleted it (BUG-1 regression).
        mock_client.delete_bare_metal.assert_awaited_once_with("inst-1")

    @pytest.mark.asyncio
    async def test_emits_poll_status_marker(
        self,
        log_records: list,
    ) -> None:
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            return_value={"id": "inst-1", "status": "active", "main_ip": "1.2.3.4"},
        )
        mock_client.delete_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
        ):
            await vultr_create_node(cfg, mock_key)

        poll_records = [r for r in log_records if r.getMessage() == "POLL_STATUS"]
        assert len(poll_records) >= 1
        fields = extra_fields(poll_records[0])
        assert fields["instance_id"] == "inst-1"
        assert fields["status"] == "active"
        assert fields["ip"] == "1.2.3.4"

    @pytest.mark.asyncio
    async def test_get_poll_retries_transient_500_then_succeeds(self) -> None:
        """A flapping 5xx on GET status must not abort create_node — the
        instance is alive and the poll should recover on the next tick."""
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        transient_err = APIError("HTTP 500: Internal server error", status=500)
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[
                transient_err,  # GET 1: flap
                {
                    "id": "inst-1",
                    "status": "active",
                    "main_ip": "1.2.3.4",
                },  # GET 2: recovered
            ],
        )
        mock_client.delete_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
        ):
            result = await vultr_create_node(cfg, mock_key)

        assert result.external_id == "inst-1"
        assert result.hostname == "1.2.3.4"

    @pytest.mark.asyncio
    async def test_user_data_carries_users_section_with_cfg_username(
        self,
    ) -> None:
        """create_node forwards pub_key to user_data and stamps cfg.username on
        the returned DTO.

        Regression for non-root provisioning: Vultr sshkey_id only injects into
        root's authorized_keys, so a non-root user is created via cloud-init
        and the returned DTO carries cfg.username.
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key", username="myuser")
        pub = "ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = pub.encode()

        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            return_value={"id": "inst-1", "status": "active", "main_ip": "1.2.3.4"},
        )
        mock_client.delete_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
        ):
            result = await vultr_create_node(cfg, mock_key)

        # create-node body MUST contain user_data with a `users` section carrying pub.
        create_call = mock_client.create_bare_metal.call_args
        body = create_call.kwargs
        import base64 as b64m

        decoded = b64m.b64decode(body["user_data"]).decode()
        data = json.loads(decoded.removeprefix("#cloud-config\n"))
        assert data["users"] == [
            {"name": "root", "ssh_authorized_keys": [pub]},
            {
                "name": "myuser",
                "ssh_authorized_keys": [pub],
                "groups": "sudo",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
            },
        ]

        assert result.username == "myuser"

    @pytest.mark.asyncio
    async def test_post_transport_failure_reconciles_orphan_by_label(
        self,
    ) -> None:
        """POST /bare-metals transport failure AFTER the server accepted the
        create leaves an instance we have no id for. Reconcile-by-label
        finds it via the label generated pre-POST and best-effort deletes it,
        so no billable orphan leaks even when the create call itself failed.

        Regression: the original code only cleaned up failures AFTER receiving
        an instance_id; a timeout mid-POST leaked an orphan because the
        cleanup `try` had not started yet.
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"

        post_err = APIError("HTTP request failed: timeout")  # transport-level

        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(side_effect=post_err)
        mock_client.get_bare_metals = MagicMock(
            return_value=_bm_stream([{"id": "orphan-1", "label": "test-node"}]),
        )
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # verify: gone
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(APIError),
        ):
            await vultr_create_node(cfg, mock_key)

        # POST was attempted once via create_bare_metal.
        mock_client.create_bare_metal.assert_awaited_once()
        # Reconcile fired: get_bare_metals found the orphan, DELETE on delete_bare_metal.
        mock_client.get_bare_metals.assert_called_once()
        mock_client.delete_bare_metal.assert_awaited_once_with("orphan-1")
        # Verify poll ran on get_bare_metal.
        assert mock_client.get_bare_metal.call_count == 1

    @pytest.mark.asyncio
    async def test_get_poll_permanent_4xx_does_not_retry(self) -> None:
        """A 4xx on GET must surface immediately — it is permanent, not a flap."""
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        permanent_err = APIError("HTTP 404: Not found", status=404)
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[
                permanent_err,  # GET create-poll: 404 — permanent, raise
                APIError("HTTP 404", status=404),  # GET verify: gone
            ],
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=MagicMock(time=MagicMock(side_effect=[0, 1, 0, 1])),
            ),
            pytest.raises(APIError) as exc_info,
        ):
            await vultr_create_node(cfg, mock_key)

        # The 404 propagated, not swallowed.
        assert exc_info.value.status == 404
        # Instance was created → cleanup DELETE fired once.
        mock_client.delete_bare_metal.assert_awaited_once_with("inst-1")

    @pytest.mark.asyncio
    async def test_cancelled_during_poll_deletes_instance(self) -> None:
        """CancelledError (daemon shutdown) mid-poll must still trigger the
        cleanup DELETE so a billing bare-metal is not orphaned.

        Regression: the cleanup clause was ``except Exception``, and
        CancelledError is a BaseException since Py3.8 — so cancellation
        skipped cleanup entirely, leaking the instance until manual deletion.
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        # Poll GET delivers the cancellation; verify GET then confirms gone.
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[
                asyncio.CancelledError(),
                APIError("HTTP 404", status=404),
            ],
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 0, 0])

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await vultr_create_node(cfg, mock_key)

        # Cleanup DELETE fired despite cancellation; verify confirmed gone.
        mock_client.delete_bare_metal.assert_awaited_once_with("inst-1")
        assert mock_client.get_bare_metal.call_count == 2

    @pytest.mark.asyncio
    async def test_cancelled_during_post_reconciles_orphan(self) -> None:
        """CancelledError mid-POST (server accepted, client never read the id)
        must still reconcile by label so the created instance is deleted.

        Regression: the reconcile clause was ``except Exception``; a transport
        cancel after acceptance left a billable orphan with no known id.
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        # POST is cancelled mid-flight (instance may exist server-side).
        mock_client.create_bare_metal = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )
        # Reconcile finds the orphan by label, then delete+verify confirm gone.
        mock_client.get_bare_metals = MagicMock(
            return_value=_bm_stream([{"id": "orphan-1", "label": "test-node"}]),
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0])

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await vultr_create_node(cfg, mock_key)

        # Reconcile fired: orphan matched by label and best-effort deleted.
        mock_client.get_bare_metals.assert_called_once()
        mock_client.delete_bare_metal.assert_awaited_once_with("orphan-1")

    @pytest.mark.asyncio
    async def test_cleanup_interruption_preserves_original_error(
        self,
        log_records: list,
    ) -> None:
        """A second exception during the cleanup DELETE MUST NOT suppress the
        original create error. Without this, a cancel during shutdown loses the
        instance id (no DB row yet) and leaves an orphan with no operator signal.

        Regression: the cleanup call was unguarded; a BaseException raised by
        _delete_and_verify during shutdown replaced the original error.
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(return_value={"id": "inst-1"})
        # Poll raises CancelledError (original error: shutdown mid-provision).
        mock_client.get_bare_metal = AsyncMock(side_effect=asyncio.CancelledError())
        mock_client.delete_bare_metal = AsyncMock()

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0])

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            # Cleanup itself is interrupted — DIFFERENT exception type so the
            # test proves the ORIGINAL propagates, not this one.
            patch(
                "yascheduler.infra.cloud.providers.vultr._delete_and_verify",
                AsyncMock(side_effect=RuntimeError("cleanup blew up")),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await vultr_create_node(cfg, mock_key)

        # Original CancelledError propagated (not the RuntimeError from cleanup).
        interrupted = [
            r for r in log_records if "CLEANUP_INTERRUPTED" in r.getMessage()
        ]
        assert len(interrupted) == 1
        assert "inst-1" in interrupted[0].getMessage()

    @pytest.mark.asyncio
    async def test_reconcile_interruption_preserves_post_error(
        self,
        log_records: list,
    ) -> None:
        """A second exception during POST-failure reconcile MUST NOT suppress
        the original POST error — the label is the only handle on a
        possibly-created orphan, so it must be logged for manual recovery."""
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_create_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_key = MagicMock()
        mock_key.export_public_key.return_value = b"ssh-rsa AAAAB3NzaC1yc2E= test"
        post_err = APIError("HTTP request failed: timeout")  # original error

        mock_client = MagicMock()
        mock_client.create_bare_metal = AsyncMock(side_effect=post_err)

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_ssh_key_id",
                AsyncMock(return_value="key-1"),
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.get_rnd_name",
                return_value="test-node",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            # Reconcile itself is interrupted — different exception type than post_err.
            patch(
                "yascheduler.infra.cloud.providers.vultr._reconcile_orphan_by_label",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(APIError, match="HTTP request failed"),
        ):
            await vultr_create_node(cfg, mock_key)

        interrupted = [
            r for r in log_records if "RECONCILE_INTERRUPTED" in r.getMessage()
        ]
        assert len(interrupted) == 1
        assert "test-node" in interrupted[0].getMessage()


# =============================================================================
# _delete_and_verify
# =============================================================================


class TestDeleteAndVerify:
    """_delete_and_verify: DELETE + verify-poll semantics."""

    @pytest.mark.asyncio
    async def test_delete_accepted_then_verify_404(self) -> None:
        """2xx DELETE → verify GET polls until 404 (confirmed gone)."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(return_value=None)  # DELETE 2xx
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[
                {"id": "inst-1"},  # GET 1: still present
                APIError("HTTP 404", status=404),  # GET 2: gone
            ],
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch("yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()),
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is True

        # 1 DELETE on delete_bare_metal + 2 GETs on get_bare_metal (present then gone).
        mock_client.delete_bare_metal.assert_awaited_once()
        assert mock_client.get_bare_metal.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_404_means_already_gone_no_verify(self) -> None:
        """DELETE returns 404 → already gone → no verify-poll needed."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=APIError("HTTP 404", status=404),
        )
        mock_client.get_bare_metal = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is True

        # Only the DELETE — no verify GET.
        mock_client.delete_bare_metal.assert_awaited_once()
        mock_client.get_bare_metal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_500_retried_then_accepted(self) -> None:
        """DELETE 5xx is transient → retry DELETE; then 2xx accepted → verify."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=[
                APIError("HTTP 500", status=500),  # DELETE 1: transient
                None,  # DELETE 2: accepted
            ],
        )
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # GET: gone
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch("yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()),
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is True

        # 2 DELETEs on delete_bare_metal, 1 GET verify on get_bare_metal.
        assert mock_client.delete_bare_metal.call_count == 2
        assert mock_client.get_bare_metal.call_count == 1

    @pytest.mark.asyncio
    async def test_delete_500_exhausts_retries_no_verify(self) -> None:
        """Persistent 5xx on DELETE exhausts retries → no verify (never accepted)."""
        from yascheduler.infra.cloud.providers.vultr import (
            CLEANUP_DELETE_ATTEMPTS,
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=APIError("HTTP 500", status=500),
        )
        mock_client.get_bare_metal = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is False

        # All calls are DELETE retries — no verify GET ever issued.
        assert mock_client.delete_bare_metal.call_count == CLEANUP_DELETE_ATTEMPTS
        mock_client.get_bare_metal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_permanent_4xx_no_retry_no_verify(self) -> None:
        """DELETE 4xx (non-404) is permanent → no retry, no verify."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=APIError("HTTP 403", status=403),
        )
        mock_client.get_bare_metal = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is False

        mock_client.delete_bare_metal.assert_awaited_once()
        mock_client.get_bare_metal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verify_timeout_logs_error_still_present(
        self,
        log_records: list,
    ) -> None:
        """DELETE accepted but instance never goes 404 → ERROR log for manual intervention."""
        from yascheduler.infra.cloud.providers.vultr import (
            CLEANUP_VERIFY_TIMEOUT,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            return_value={"id": "inst-1"},  # always present
        )

        # Simulate verify-timeout: time() jumps past deadline after first GET.
        call_count = [0]

        def fake_time() -> float:
            call_count[0] += 1
            if call_count[0] <= 2:
                return 0  # deadline = 0 + CLEANUP_VERIFY_TIMEOUT; while: 0 < deadline
            return CLEANUP_VERIFY_TIMEOUT + 1  # expire on second while-check

        loop = MagicMock()
        loop.time = MagicMock(side_effect=fake_time)

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch("yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()),
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is False

        # Must NOT claim success — escalate to ERROR.
        error_records = [
            r
            for r in log_records
            if r.levelno >= logging.ERROR and "STILL PRESENT" in r.getMessage()
        ]
        assert len(error_records) == 1

    @pytest.mark.asyncio
    async def test_verify_get_transient_error_keeps_polling(self) -> None:
        """Transient GET error during verify → keep polling (uncertain), not abort."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(side_effect=[None])
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[
                APIError("HTTP 500", status=500),  # GET 1: transient
                APIError("HTTP 404", status=404),  # GET 2: gone
            ],
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch("yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()),
        ):
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is True

        # 1 DELETE on delete_bare_metal, 2 GETs on get_bare_metal.
        mock_client.delete_bare_metal.assert_awaited_once()
        assert mock_client.get_bare_metal.call_count == 2

    @pytest.mark.asyncio
    async def test_never_raises(self) -> None:
        """Cleanup must never raise even when everything fails."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _delete_and_verify,
        )

        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=APIError("HTTP 500", status=500),
        )
        mock_client.get_bare_metal = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()
        ):
            # Must not raise.
            result = await _delete_and_verify(mock_client, "inst-1")

        assert result is False
        mock_client.get_bare_metal.assert_not_awaited()


# =============================================================================
# _reconcile_orphan_by_label
# =============================================================================


class TestReconcileOrphanByLabel:
    """_reconcile_orphan_by_label: closes the POST-ambiguity orphan window.

    Vultr POST /bare-metals is not idempotent: if the client times out or the
    transport breaks after the server accepted the create, an instance exists
    that we have no id for. The helper matches it by the label generated
    pre-POST and best-effort deletes it. Never raises.
    """

    @pytest.mark.asyncio
    async def test_label_match_deletes_orphan(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_bare_metals = MagicMock(
            return_value=_bm_stream(
                [
                    {"id": "orphan-1", "label": "test-node"},
                    {"id": "other-2", "label": "unrelated"},
                ],
            ),
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # verify: gone
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
        ):
            await _reconcile_orphan_by_label(mock_client, "test-node")

        # get_bare_metals called once; DELETE on delete_bare_metal; GET verify on get_bare_metal.
        mock_client.get_bare_metals.assert_called_once()
        mock_client.delete_bare_metal.assert_awaited_once_with("orphan-1")
        assert mock_client.get_bare_metal.call_count == 1

    @pytest.mark.asyncio
    async def test_no_match_no_delete(self) -> None:
        from yascheduler.infra.cloud.providers.vultr import (
            RECONCILE_ATTEMPTS,
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_bare_metals = MagicMock(
            return_value=_bm_stream([{"id": "x", "label": "unrelated"}]),
        )
        mock_client.delete_bare_metal = AsyncMock()
        mock_client.get_bare_metal = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()
        ):
            await _reconcile_orphan_by_label(mock_client, "test-node")

        # get_bare_metals retried RECONCILE_ATTEMPTS times; no DELETE, no verify.
        assert mock_client.get_bare_metals.call_count == RECONCILE_ATTEMPTS
        mock_client.delete_bare_metal.assert_not_awaited()
        assert mock_client.get_bare_metal.call_count == 0

    @pytest.mark.asyncio
    async def test_listing_lag_orphan_appears_on_retry(
        self,
        log_records: list,
    ) -> None:
        """Listing-lag: the orphan is not visible on the first listing (Vultr
        not yet consistent after POST) but appears on the second attempt. The
        retry loop must catch it and delete it, not give up after attempt 1."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_bare_metals = MagicMock(
            side_effect=[
                _bm_stream([]),  # attempt 1: not yet visible (lag)
                _bm_stream([{"id": "orphan-1", "label": "test-node"}]),  # attempt 2
            ],
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # verify: gone
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
        ):
            await _reconcile_orphan_by_label(mock_client, "test-node")

        # get_bare_metals called twice (lag then match); DELETE on delete_bare_metal.
        assert mock_client.get_bare_metals.call_count == 2
        mock_client.delete_bare_metal.assert_awaited_once_with("orphan-1")
        assert mock_client.get_bare_metal.call_count == 1

    @pytest.mark.asyncio
    async def test_listing_transient_failure_then_success_deletes_orphan(
        self,
        log_records: list,
    ) -> None:
        """Transient listing failure (5xx) on attempt 1 must not abort
        reconcile — the loop retries and deletes the orphan once the listing
        recovers."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_bare_metals = MagicMock(
            side_effect=[
                _bm_stream(
                    [], exc=APIError("HTTP 502", status=502)
                ),  # attempt 1: listing down
                _bm_stream(
                    [{"id": "orphan-1", "label": "test-node"}]
                ),  # attempt 2: recovered
            ],
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # verify: gone
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
        ):
            await _reconcile_orphan_by_label(mock_client, "test-node")

        # get_bare_metals called twice (fail then recover); DELETE on delete_bare_metal.
        assert mock_client.get_bare_metals.call_count == 2
        mock_client.delete_bare_metal.assert_awaited_once_with("orphan-1")
        assert mock_client.get_bare_metal.call_count == 1

    @pytest.mark.asyncio
    async def test_orphan_on_second_page_is_found(
        self,
        log_records: list,
    ) -> None:
        """Accounts with >500 bare-metals paginate: the orphan sits on page 2
        and must still be matched and deleted. get_bare_metals paginates
        internally as it yields, so the orphan is visible to the caller once
        the iterator advances to page 2."""
        from yascheduler.infra.cloud.providers.vultr import (
            LIST_PAGE_SIZE,
            APIError,
            _reconcile_orphan_by_label,
        )

        page1 = [
            {"id": f"bm-{i}", "label": f"other-{i}"} for i in range(LIST_PAGE_SIZE)
        ]
        page2 = [{"id": "orphan-1", "label": "test-node"}]

        mock_client = MagicMock()
        mock_client.get_bare_metals = MagicMock(
            return_value=_bm_stream(page1 + page2),
        )
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # verify: gone
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
        ):
            await _reconcile_orphan_by_label(mock_client, "test-node")

        # get_bare_metals flattened both pages; DELETE on delete_bare_metal.
        mock_client.get_bare_metals.assert_called_once()
        mock_client.delete_bare_metal.assert_awaited_once_with("orphan-1")
        assert mock_client.get_bare_metal.call_count == 1

    @pytest.mark.asyncio
    async def test_lookup_failure_never_raises(
        self,
        log_records: list,
    ) -> None:
        """Lookup GET itself fails -> log ERROR for manual check, no raise."""
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            _reconcile_orphan_by_label,
        )

        mock_client = MagicMock()
        mock_client.get_bare_metals = MagicMock(
            side_effect=lambda *a, **kw: _bm_stream(
                [], exc=APIError("HTTP 500", status=500)
            ),
        )
        mock_client.delete_bare_metal = AsyncMock()
        mock_client.get_bare_metal = AsyncMock()

        with patch(
            "yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()
        ):
            # Must not raise — original POST error propagates regardless.
            await _reconcile_orphan_by_label(mock_client, "test-node")

        assert any("RECONCILE_LOOKUP_FAILED" in r.getMessage() for r in log_records)
        assert mock_client.get_bare_metal.call_count == 0
        mock_client.delete_bare_metal.assert_not_awaited()


# =============================================================================
# vultr_delete_node
# =============================================================================


class TestVultrDeleteNode:
    """vultr_delete_node: DELETE /bare-metals/{external_id} + verify + log.

    external_id is the bare-metal instance id (not the IP). A 404 on DELETE
    means already gone (idempotent no-op). A 2xx DELETE triggers verify-poll
    until 404. _delete_and_verify returning False raises APIError for
    cross-cycle retry.
    """

    @pytest.mark.asyncio
    async def test_delete_accepted_then_verify_404_logs_deleted(
        self,
        log_records: list,
    ) -> None:
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            vultr_delete_node,
        )

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            side_effect=[APIError("HTTP 404", status=404)],  # verify: gone
        )

        loop = MagicMock()
        loop.time = MagicMock(side_effect=[0, 0, 1, 1])

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch("yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()),
        ):
            await vultr_delete_node(cfg, "inst-1")

        # DELETE targeted external_id directly (no IP lookup).
        mock_client.delete_bare_metal.assert_awaited_once_with("inst-1")
        # DELETED info marker emitted only after verify confirms gone
        deleted_records = [r for r in log_records if "DELETED" in r.getMessage()]
        assert len(deleted_records) == 1

    @pytest.mark.asyncio
    async def test_delete_404_already_gone_is_noop(
        self,
        log_records: list,
    ) -> None:
        """DELETE returns 404 -> already gone -> no verify, no raise. Vultr
        DELETE 404 is idempotent (matches hetzner contract)."""
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import (
            APIError,
            vultr_delete_node,
        )

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=APIError("HTTP 404", status=404),  # DELETE: already gone
        )
        mock_client.get_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch("yascheduler.infra.cloud.providers.vultr.asyncio.sleep", AsyncMock()),
        ):
            await vultr_delete_node(cfg, "inst-gone")

        # Only the DELETE call — no verify GET.
        mock_client.delete_bare_metal.assert_awaited_once_with("inst-gone")
        mock_client.get_bare_metal.assert_not_awaited()
        # Already-gone (404) is confirmed gone -> DELETED logged + already-gone warning.
        deleted_records = [
            r for r in log_records if r.getMessage() == "DELETED inst-gone"
        ]
        assert len(deleted_records) == 1
        already_gone_records = [
            r for r in log_records if "already gone" in r.getMessage()
        ]
        assert len(already_gone_records) == 1

    @pytest.mark.asyncio
    async def test_delete_not_confirmed_raises_to_enable_cross_cycle_retry(
        self,
    ) -> None:
        """_delete_and_verify returns False -> vultr_delete_node RAISES so the
        node stays disabled in DB and the next orchestrator cycle retries.

        Regression: previously delete_node swallowed the False and returned
        silently, deallocate_node then removed the DB row, and the cloud VM
        became a permanent orphan (no row left to retry against).
        """
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import APIError, vultr_delete_node

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(
            side_effect=APIError("HTTP 403", status=403),  # DELETE: permanent
        )
        mock_client.get_bare_metal = AsyncMock()

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(APIError, match="delete not confirmed"),
        ):
            await vultr_delete_node(cfg, "inst-1")

        mock_client.get_bare_metal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_verify_timeout_raises(
        self,
        log_records: list,
    ) -> None:
        """DELETE accepted but instance never reaches 404 (verify timeout) ->
        vultr_delete_node RAISES so the row survives and the next cycle
        retries. An ERROR log still escalates for manual intervention."""
        from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
        from yascheduler.infra.cloud.providers.vultr import (
            CLEANUP_VERIFY_TIMEOUT,
            APIError,
            vultr_delete_node,
        )

        cfg = ConfigCloudVultr(api_key="test-key")
        mock_client = MagicMock()
        mock_client.delete_bare_metal = AsyncMock(return_value=None)
        mock_client.get_bare_metal = AsyncMock(
            return_value={"id": "inst-1"},  # always present
        )

        call_count = [0]

        def fake_time() -> float:
            call_count[0] += 1
            if call_count[0] <= 2:
                return 0
            return CLEANUP_VERIFY_TIMEOUT + 1

        loop = MagicMock()
        loop.time = MagicMock(side_effect=fake_time)

        with (
            _patch_vultr_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vultr.asyncio.sleep",
                AsyncMock(),
            ),
            pytest.raises(APIError, match="delete not confirmed"),
        ):
            await vultr_delete_node(cfg, "inst-1")

        # Escalation log preserved for manual reconciliation.
        error_records = [
            r
            for r in log_records
            if r.levelno >= logging.ERROR and "STILL PRESENT" in r.getMessage()
        ]
        assert len(error_records) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
