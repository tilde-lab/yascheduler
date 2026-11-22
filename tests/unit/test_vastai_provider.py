"""Tests for VastAI provider module."""
# region MODULE_CONTRACT
# PURPOSE: Unit tests for VastAI provider: exception type, validators, client
#          methods, and create/delete orphan-prevention branches.
# SCOPE: vastai.py module-level behavior; VastAIClient and helpers via mocks.
# KEYWORDS: vastai, provider, unit, exceptions, orphan-prevention
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from tests.log_assertions import extra_fields
from yascheduler.infra.cloud.cloud_configs import ConfigCloudVastAI
from yascheduler.infra.cloud.providers.vastai import VastAIInstance, VastAIOffer

pytestmark = pytest.mark.unit

_INSTANCE: VastAIInstance = {
    "id": 1,
    "actual_status": "running",
    "ssh_host": "1.2.3.4",
    "ssh_port": 22,
}
_OFFER: VastAIOffer = {"id": 42, "dph_total": 0.5}
_VALID_OFFERS = {"offers": [_OFFER]}
_PUBKEY = b"ssh-rsa AAAAB3NzaC1yc2E= test"


# region HELPERS


def _instance_stream(items: list, exc: BaseException | None = None):
    """Async generator mimicking VastAIClient.show_instances."""

    async def gen():
        if exc is not None:
            raise exc
        for inst in items:
            yield inst

    return gen()


def _patch_vastai_client(
    mock_client: MagicMock,
) -> AbstractContextManager[MagicMock]:
    """Patch VastAIClient so ``async with VastAIClient(...)`` yields mock_client."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_client
    mock_cm.__aexit__.return_value = None
    return patch(
        "yascheduler.infra.cloud.providers.vastai.VastAIClient",
        return_value=mock_cm,
    )


def _cfg(
    *,
    api_key: str = "secret-key-XYZ",
    onstart_script: str | None = None,
    label: str = "yascheduler",
    image: str = "pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel",
    jump_host: str | None = None,
    jump_port: int = 22,
    jump_username: str | None = None,
    min_vram_mb: int = 1024,
    num_gpus: int = 1,
    max_price_per_hr: float = 1.5,
    connect_grace: int = 300,
) -> ConfigCloudVastAI:
    from yascheduler.infra.cloud.cloud_configs import ConfigCloudVastAI

    return ConfigCloudVastAI(
        api_key=api_key,
        onstart_script=onstart_script,
        label=label,
        image=image,
        jump_host=jump_host,
        jump_port=jump_port,
        jump_username=jump_username,
        min_vram_mb=min_vram_mb,
        num_gpus=num_gpus,
        max_price_per_hr=max_price_per_hr,
        connect_grace=connect_grace,
    )


def _key(pubkey: bytes = _PUBKEY) -> MagicMock:
    mock_key = MagicMock()
    mock_key.export_public_key.return_value = pubkey
    return mock_key


def _markers(records: list[logging.LogRecord]) -> list[str]:
    """Return the positional block-marker messages, in order."""
    return [r.getMessage() for r in records if r.getMessage().isupper()]


# endregion HELPERS


# =============================================================================
# Exception type
# =============================================================================


class TestVastAIError:
    def test_is_exception_subclass(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert issubclass(VastAIError, Exception)

    def test_message_preserved(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert str(VastAIError("boom")) == "boom"

    def test_default_status_none(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("x").status is None

    def test_status_preserved(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("x", status=503).status == 503

    def test_transient_429(self) -> None:
        # PURPOSE: 429 (rate-limit) is transient — retryable on the GPU market
        # where demand spikes are common. Mirrors vultr/hetzner.
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("x", status=429).transient is True

    def test_transient_5xx(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("x", status=500).transient is True
        assert VastAIError("x", status=503).transient is True

    def test_transient_transport_none_status(self) -> None:
        # PURPOSE: transport-level failure (no HTTP status) is transient —
        # network blip, not a server-side rejection.
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("net").transient is True

    def test_not_transient_4xx(self) -> None:
        # PURPOSE: 4xx (non-429) is permanent — auth/permission/client error
        # won't self-heal by retrying.
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("x", status=403).transient is False
        assert VastAIError("x", status=400).transient is False

    def test_not_transient_404(self) -> None:
        # PURPOSE: 404 is NOT transient — it's the idempotent "already gone"
        # success signal in _delete_instance, not a retry candidate.
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert VastAIError("x", status=404).transient is False

    def test_not_scheduling_error(self) -> None:
        try:
            from yascheduler.domain.exceptions import SchedulingError
        except ImportError:
            pytest.skip("SchedulingError not importable")
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        assert not issubclass(VastAIError, SchedulingError)


# =============================================================================
# VastAIClient._request
# =============================================================================


class TestRequest:
    @pytest.mark.asyncio
    async def test_2xx_returns_json(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient

        client = VastAIClient.__new__(VastAIClient)
        resp_ctx = AsyncMock()
        resp_ctx.__aenter__.return_value.status = 200
        resp_ctx.__aenter__.return_value.text = AsyncMock(return_value='{"ok": 1}')
        client._session = MagicMock()
        client._session.request.return_value = resp_ctx
        result = await client._request("GET", "/x")
        assert result == {"ok": 1}

    @pytest.mark.asyncio
    async def test_4xx_raises_with_status(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient, VastAIError

        client = VastAIClient.__new__(VastAIClient)
        resp_ctx = AsyncMock()
        resp = resp_ctx.__aenter__.return_value
        resp.status = 403
        resp.text = AsyncMock(return_value="forbidden")
        client._session = MagicMock()
        client._session.request.return_value = resp_ctx
        with pytest.raises(VastAIError) as exc_info:
            await client._request("GET", "/x")
        assert exc_info.value.status == 403

    @pytest.mark.asyncio
    async def test_client_response_error_wrapped_with_status(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient, VastAIError

        client = VastAIClient.__new__(VastAIClient)
        client._session = MagicMock()
        client._session.request.side_effect = aiohttp.ClientResponseError(
            request_info=MagicMock(), history=(), message="boom", status=502
        )
        with pytest.raises(VastAIError) as exc_info:
            await client._request("GET", "/x")
        assert exc_info.value.status == 502

    @pytest.mark.asyncio
    async def test_client_error_wrapped_status_none(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient, VastAIError

        client = VastAIClient.__new__(VastAIClient)
        client._session = MagicMock()
        client._session.request.side_effect = aiohttp.ClientError("net down")
        with pytest.raises(VastAIError) as exc_info:
            await client._request("GET", "/x")
        assert exc_info.value.status is None

    @pytest.mark.asyncio
    async def test_timeout_error_wrapped_status_none(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient, VastAIError

        client = VastAIClient.__new__(VastAIClient)
        client._session = MagicMock()
        client._session.request.side_effect = asyncio.TimeoutError()
        with pytest.raises(VastAIError) as exc_info:
            await client._request("GET", "/x")
        assert exc_info.value.status is None

    @pytest.mark.asyncio
    async def test_logs_request_marker(self, log_records: list) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient

        client = VastAIClient.__new__(VastAIClient)
        resp_ctx = AsyncMock()
        resp = resp_ctx.__aenter__.return_value
        resp.status = 200
        resp.text = AsyncMock(return_value="{}")
        client._session = MagicMock()
        client._session.request.return_value = resp_ctx
        await client._request("GET", "/foo")
        rec = next(r for r in log_records if r.getMessage() == "VASTAI_REQUEST")
        assert extra_fields(rec) == {"method": "GET", "path": "/foo", "status": 200}

    @pytest.mark.asyncio
    async def test_2xx_empty_body_returns_none(self) -> None:
        # PURPOSE: DELETE may 2xx with empty body; json() would raise a bogus
        # non-transient VastAIError(status=200). Empty body -> None.
        from yascheduler.infra.cloud.providers.vastai import VastAIClient

        client = VastAIClient.__new__(VastAIClient)
        resp_ctx = AsyncMock()
        resp = resp_ctx.__aenter__.return_value
        resp.status = 204
        resp.text = AsyncMock(return_value="")
        client._session = MagicMock()
        client._session.request.return_value = resp_ctx
        assert await client._request("DELETE", "/instances/7") is None


# =============================================================================
# Client method validators
# =============================================================================


def _client_with_request(return_value):
    from yascheduler.infra.cloud.providers.vastai import VastAIClient

    client = VastAIClient.__new__(VastAIClient)
    patcher = patch.object(client, "_request", AsyncMock(return_value=return_value))
    return client, patcher


class TestGetSshKeys:
    @pytest.mark.asyncio
    async def test_valid_list(self) -> None:

        client, patcher = _client_with_request([{"public_key": "ssh-rsa AAA"}])
        with patcher as mock_req:
            result = await client.get_ssh_keys()
        assert result == [{"public_key": "ssh-rsa AAA"}]
        mock_req.assert_awaited_once_with("GET", "ssh")

    @pytest.mark.asyncio
    async def test_invalid_shape_dict(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request({"not": "a list"})
        with patcher, pytest.raises(VastAIError, match="Invalid SSH key list"):
            await client.get_ssh_keys()

    @pytest.mark.asyncio
    async def test_invalid_entry(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request([{"no_public_key": 1}])
        with patcher, pytest.raises(VastAIError, match="Invalid SSH key list"):
            await client.get_ssh_keys()


class TestCreateSshKey:
    @pytest.mark.asyncio
    async def test_success_true_returns_true(self) -> None:

        resp = {"success": True, "key": {"public_key": "ssh-rsa AAA"}}
        client, patcher = _client_with_request(resp)
        with patcher as mock_req:
            result = await client.create_ssh_key("ssh-rsa AAA")
        assert result is True
        mock_req.assert_awaited_once_with(
            "POST", "ssh", data={"ssh_key": "ssh-rsa AAA"}
        )

    @pytest.mark.asyncio
    async def test_success_false_returns_false(self) -> None:

        resp = {"success": False, "key": {"public_key": "ssh-rsa AAA"}}
        client, patcher = _client_with_request(resp)
        with patcher:
            result = await client.create_ssh_key("ssh-rsa AAA")
        assert result is False

    @pytest.mark.asyncio
    async def test_invalid_shape_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request({"no_success": 1})
        with patcher, pytest.raises(VastAIError, match="Invalid SSH key create"):
            await client.create_ssh_key("k")


class TestSearchOffers:
    @pytest.mark.asyncio
    async def test_valid_returns_offers_and_logs(self, log_records: list) -> None:

        client, patcher = _client_with_request(_VALID_OFFERS)
        with patcher as mock_req:
            result = await client.search_offers()
        assert result == [_OFFER]
        rec = next(r for r in log_records if r.getMessage() == "OFFER_SEARCH")
        assert extra_fields(rec) == {"offer_count": 1}
        # Default body: type + limit
        _, kwargs = mock_req.call_args
        assert kwargs["data"]["type"] == "ondemand"
        assert kwargs["data"]["limit"] == 20
        mock_req.assert_awaited_once_with("POST", "bundles", data=kwargs["data"])

    @pytest.mark.asyncio
    async def test_invalid_shape_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request({"no_offers": 1})
        with patcher, pytest.raises(VastAIError, match="Invalid offers list"):
            await client.search_offers()

    @pytest.mark.asyncio
    async def test_order_forwarded_as_list_of_lists(self) -> None:

        client, patcher = _client_with_request(_VALID_OFFERS)
        with patcher as mock_req:
            await client.search_offers(order=[("dph_total", "asc")])
        _, kwargs = mock_req.call_args
        assert kwargs["data"]["order"] == [["dph_total", "asc"]]

    @pytest.mark.asyncio
    async def test_limit_zero_not_added(self) -> None:

        client, patcher = _client_with_request(_VALID_OFFERS)
        with patcher as mock_req:
            await client.search_offers(limit=0)
        _, kwargs = mock_req.call_args
        assert "limit" not in kwargs["data"]

    @pytest.mark.asyncio
    async def test_filters_forwarded(self) -> None:

        client, patcher = _client_with_request(_VALID_OFFERS)
        with patcher as mock_req:
            await client.search_offers(gpu_ram={"gte": 1024})
        _, kwargs = mock_req.call_args
        assert kwargs["data"]["gpu_ram"] == {"gte": 1024}


class TestCreateInstance:
    @pytest.mark.asyncio
    async def test_valid_new_contract_int(self) -> None:

        client, patcher = _client_with_request({"new_contract": 7})
        with patcher as mock_req:
            result = await client.create_instance(ask_id=42, image="img")
        assert result == 7
        _, kwargs = mock_req.call_args
        assert kwargs["data"] == {
            "target_state": "running",
            "runtype": "ssh_proxy",
            "cancel_unavail": True,
            "image": "img",
        }
        mock_req.assert_awaited_once_with("PUT", "asks/42", data=kwargs["data"])

    @pytest.mark.asyncio
    async def test_float_new_contract_returns_int(self) -> None:

        client, patcher = _client_with_request({"new_contract": 9.0})
        with patcher:
            result = await client.create_instance(ask_id=1, image="i")
        assert result == 9 and isinstance(result, int)

    @pytest.mark.asyncio
    async def test_invalid_shape_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request({"no_contract": 1})
        with patcher, pytest.raises(VastAIError, match="Invalid create instance"):
            await client.create_instance(ask_id=1, image="i")


class TestDestroyInstance:
    @pytest.mark.asyncio
    async def test_delete_path(self) -> None:

        client, patcher = _client_with_request(None)
        with patcher as mock_req:
            await client.destroy_instance(99)
        mock_req.assert_awaited_once_with("DELETE", "instances/99")


class TestShowInstance:
    @pytest.mark.asyncio
    async def test_valid_returns_instance_dict(self) -> None:

        client, patcher = _client_with_request({"instances": _INSTANCE})
        with patcher as mock_req:
            result = await client.show_instance(1)
        assert result == _INSTANCE
        mock_req.assert_awaited_once_with("GET", "instances/1")

    @pytest.mark.asyncio
    async def test_instances_none_returns_none(self) -> None:
        # PURPOSE: VastAI answers GET for a deleted/non-existent id with 200 +
        # {"instances": null} (404 is not defined for this endpoint per the
        # official vast-cli SDK). That is "gone", not a malformed response.
        client, patcher = _client_with_request({"instances": None})
        with patcher as mock_req:
            result = await client.show_instance(1)
        assert result is None
        # ponytail: no leading slash — aiohttp base_url has a trailing slash, so
        # a leading slash would resolve against the host root, dropping the API
        # base path.
        mock_req.assert_awaited_once_with("GET", "instances/1")

    @pytest.mark.asyncio
    async def test_missing_instances_key_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request({"other": 1})
        with patcher, pytest.raises(VastAIError, match="Invalid show instance"):
            await client.show_instance(1)


class TestShowInstances:
    @pytest.mark.asyncio
    async def test_single_page_yields_all(self) -> None:

        page = {"next_token": None, "instances": [_INSTANCE, _INSTANCE]}
        client, patcher = _client_with_request(page)
        with patcher:
            result = [x async for x in client.show_instances()]
        assert result == [_INSTANCE, _INSTANCE]

    @pytest.mark.asyncio
    async def test_pagination_three_pages(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient

        pages = [
            {"next_token": "t1", "instances": [_INSTANCE]},
            {
                "next_token": "t2",
                "instances": [
                    {
                        "id": 2,
                        "actual_status": "running",
                        "ssh_host": "5.6.7.8",
                        "ssh_port": 22,
                    }
                ],
            },
            {
                "next_token": None,
                "instances": [
                    {
                        "id": 3,
                        "actual_status": "running",
                        "ssh_host": "9.0.0.1",
                        "ssh_port": 22,
                    }
                ],
            },
        ]
        client = VastAIClient.__new__(VastAIClient)
        captured: list[dict] = []

        async def fake_request(method, path, params=None, **kw):
            # snapshot params at call time (show_instances mutates the dict in place)
            captured.append(dict(params) if params else {})
            return pages.pop(0)

        with patch.object(client, "_request", fake_request):
            result = [x async for x in client.show_instances()]
        assert [x["id"] for x in result] == [1, 2, 3]
        # call 1: no after_token; call 2: after_token from page1 (t1); call 3: t2.
        assert "after_token" not in captured[0]
        assert captured[1]["after_token"] == "t1"
        assert captured[2]["after_token"] == "t2"
        assert len(captured) == 3

    @pytest.mark.asyncio
    async def test_invalid_shape_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIError

        client, patcher = _client_with_request(
            {"next_token": None, "instances": "notlist"}
        )
        with patcher, pytest.raises(VastAIError, match="Invalid show instances"):
            async for _ in client.show_instances():
                pass

    @pytest.mark.asyncio
    async def test_select_filters_json_encoded(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import VastAIClient

        page = {"next_token": None, "instances": [_INSTANCE]}
        client = VastAIClient.__new__(VastAIClient)
        captured: list[dict] = []

        async def fake_request(method, path, params=None, **kw):
            captured.append(dict(params) if params else {})
            return page

        with patch.object(client, "_request", fake_request):
            result = [
                x
                async for x in client.show_instances(
                    select_filters={"label": {"eq": "yascheduler-XYZ"}}
                )
            ]
        assert result == [_INSTANCE]
        assert captured[0]["select_filters"] == '{"label": {"eq": "yascheduler-XYZ"}}'


# =============================================================================
# ensure_ssh_key
# =============================================================================


class TestEnsureSshKey:
    @pytest.mark.asyncio
    async def test_present_no_create(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import ensure_ssh_key

        client = MagicMock()
        client.get_ssh_keys = AsyncMock(return_value=[{"public_key": "ssh-rsa AAA"}])
        client.create_ssh_key = AsyncMock()
        result = await ensure_ssh_key(client, "ssh-rsa AAA")
        assert result is True
        client.create_ssh_key.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_absent_creates_and_logs(self, log_records: list) -> None:
        from yascheduler.infra.cloud.providers.vastai import ensure_ssh_key

        client = MagicMock()
        client.get_ssh_keys = AsyncMock(return_value=[])
        client.create_ssh_key = AsyncMock(return_value=True)
        result = await ensure_ssh_key(client, "ssh-rsa AAA")
        assert result is True
        client.create_ssh_key.assert_awaited_once_with("ssh-rsa AAA")
        assert any(r.getMessage() == "SSH_KEY_REGISTERED" for r in log_records)


# =============================================================================
# select_cheapest_offer
# =============================================================================


class TestSelectCheapestOffer:
    @pytest.mark.asyncio
    async def test_empty_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            select_cheapest_offer,
        )

        with pytest.raises(VastAIError, match="No offers"):
            await select_cheapest_offer([], 1.0)

    @pytest.mark.asyncio
    async def test_single_within_price(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import select_cheapest_offer

        with patch(
            "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
        ):
            result = await select_cheapest_offer([_OFFER], 1.0)
        assert result == _OFFER

    @pytest.mark.asyncio
    async def test_random_choice_over_top5_sorted(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import select_cheapest_offer

        offers: list[VastAIOffer] = [
            {"id": i, "dph_total": float(i)} for i in range(10)
        ]
        captured: list = []
        with patch(
            "yascheduler.infra.cloud.providers.vastai.random.choice",
            lambda x: captured.append(x) or x[0],
        ):
            await select_cheapest_offer(offers, 100.0)
        # choice received the top-5 cheapest sorted asc
        assert [o["id"] for o in captured[0]] == [0, 1, 2, 3, 4]

    @pytest.mark.asyncio
    async def test_price_over_max_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            select_cheapest_offer,
        )

        with pytest.raises(VastAIError, match="exceeds max price"):
            await select_cheapest_offer([_OFFER], 0.1)

    @pytest.mark.asyncio
    async def test_logs_selected_offer(self, log_records: list) -> None:
        from yascheduler.infra.cloud.providers.vastai import select_cheapest_offer

        with patch(
            "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
        ):
            await select_cheapest_offer([_OFFER], 1.0)
        rec = next(r for r in log_records if r.getMessage() == "SELECTED_OFFER")
        assert extra_fields(rec) == {"offer_id": 42, "dph_total": 0.5}


# =============================================================================
# generate_onstart
# =============================================================================


class TestGenerateOnstart:
    @pytest.mark.asyncio
    async def test_custom_script_verbatim(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(onstart_script="#!/bin/sh\necho hi")
        assert await generate_onstart(cfg) == "#!/bin/sh\necho hi"

    @pytest.mark.asyncio
    async def test_no_cloud_config_returns_empty(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        assert await generate_onstart(_cfg()) == ""

    @pytest.mark.asyncio
    async def test_apt_for_debian(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(image="debian:12")
        cc = CloudInitConfig(
            package_upgrade=True, packages=["vim"], bootcmd=("echo done",)
        )
        result = await generate_onstart(cfg, cc)
        assert "apt-get update && apt-get upgrade -y" in result
        assert "apt-get install -y vim" in result
        assert "echo done" in result

    @pytest.mark.asyncio
    async def test_apt_for_ubuntu(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(image="ubuntu:24.04")
        cc = CloudInitConfig(package_upgrade=True)
        assert "apt-get update && apt-get upgrade -y" in await generate_onstart(cfg, cc)

    @pytest.mark.asyncio
    async def test_dnf_for_non_debian(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(image="fedora:40")
        cc = CloudInitConfig(package_upgrade=True)
        assert "dnf upgrade -y" in await generate_onstart(cfg, cc)

    @pytest.mark.asyncio
    async def test_kvm_shebang(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(image="vastai/kvm:debian12")
        cc = CloudInitConfig(bootcmd=("echo x",))
        result = await generate_onstart(cfg, cc)
        assert result.startswith("#!/bin/bash")

    @pytest.mark.asyncio
    async def test_no_upgrade_when_disabled(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(image="debian:12")
        cc = CloudInitConfig(package_upgrade=False, bootcmd=("echo x",))
        assert "upgrade" not in await generate_onstart(cfg, cc)

    @pytest.mark.asyncio
    async def test_bootcmd_list_extended(self) -> None:
        from yascheduler.infra.cloud.cloud_init import CloudInitConfig
        from yascheduler.infra.cloud.providers.vastai import generate_onstart

        cfg = _cfg(image="debian:12")
        cc = CloudInitConfig(bootcmd=(["line1", "line2"],))
        result = await generate_onstart(cfg, cc)
        assert "line1" in result and "line2" in result


# =============================================================================
# detect_launch_mode
# =============================================================================


class TestDetectLaunchMode:
    def test_kvm(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import detect_launch_mode

        assert detect_launch_mode("vastai/kvm:debian12") == "kvm"

    def test_docker(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import detect_launch_mode

        assert detect_launch_mode("pytorch/pytorch:2.2.2") == "docker"


# =============================================================================
# _reconcile_orphan_by_label
# =============================================================================


class TestReconcileOrphan:
    @pytest.mark.asyncio
    async def test_orphan_found_delete_instance(self) -> None:
        # PURPOSE: reconcile delegates to _delete_instance (not raw
        # destroy_instance) so a transient DELETE failure can't leave a billed
        # orphan after reconcile claims success.
        from yascheduler.infra.cloud.providers.vastai import _reconcile_orphan_by_label

        client = MagicMock()
        client.show_instances = MagicMock(return_value=_instance_stream([_INSTANCE]))
        client.destroy_instance = AsyncMock()
        dav = AsyncMock()
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            patch("yascheduler.infra.cloud.providers.vastai._delete_instance", dav),
        ):
            await _reconcile_orphan_by_label(client, "lbl")
        dav.assert_awaited_once_with(client, 1)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_orphan_after_all_attempts(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import _reconcile_orphan_by_label

        client = MagicMock()
        client.show_instances = MagicMock(return_value=_instance_stream([]))
        client.destroy_instance = AsyncMock()
        with (
            patch("yascheduler.infra.cloud.providers.vastai._RECONCILE_INTERVAL", 0.0),
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
        ):
            await _reconcile_orphan_by_label(client, "lbl")
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transient_then_orphan(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _reconcile_orphan_by_label,
        )

        client = MagicMock()
        client.show_instances = MagicMock(
            side_effect=[
                _instance_stream([], exc=VastAIError("x")),
                _instance_stream([_INSTANCE]),
            ]
        )
        client.destroy_instance = AsyncMock()
        dav = AsyncMock()
        with (
            patch("yascheduler.infra.cloud.providers.vastai._RECONCILE_INTERVAL", 0.0),
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            patch("yascheduler.infra.cloud.providers.vastai._delete_instance", dav),
        ):
            await _reconcile_orphan_by_label(client, "lbl")
        assert client.show_instances.call_count == 2
        dav.assert_awaited_once_with(client, 1)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fresh_orphan_null_ssh_still_matched(self) -> None:
        # PURPOSE: a brand-new orphan (the exact case reconcile exists for:
        # ambiguous PUT) has no ssh_host/ssh_port yet. The list validator must
        # NOT reject it, or reconcile silently misses the orphan.
        from yascheduler.infra.cloud.providers.vastai import _reconcile_orphan_by_label

        fresh = {
            "id": 9,
            "actual_status": "provisioning",
            "ssh_host": None,
            "ssh_port": None,
        }
        client = MagicMock()
        client.show_instances = MagicMock(return_value=_instance_stream([fresh]))
        dav = AsyncMock()
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            patch("yascheduler.infra.cloud.providers.vastai._delete_instance", dav),
        ):
            await _reconcile_orphan_by_label(client, "lbl")
        dav.assert_awaited_once_with(client, 9)

    @pytest.mark.asyncio
    async def test_delete_instance_failure_swallowed(self) -> None:
        # PURPOSE: a _delete_instance exception (e.g. DELETE 403 raising) must
        # NOT mask the original create error — reconcile swallows it; the
        # original error still propagates from vastai_create_node.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _reconcile_orphan_by_label,
        )

        client = MagicMock()
        client.show_instances = MagicMock(return_value=_instance_stream([_INSTANCE]))
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                AsyncMock(side_effect=VastAIError("deny", status=403)),
            ),
        ):
            await _reconcile_orphan_by_label(client, "lbl")  # must not raise

    @pytest.mark.asyncio
    async def test_delete_instance_false_logs_error_no_success(
        self, log_records: list
    ) -> None:
        # PURPOSE: False (delete failed) = billed orphan with no captured
        # id / DB row to retry — reconcile MUST log ERROR, not silently return.
        from yascheduler.infra.cloud.providers.vastai import _reconcile_orphan_by_label

        client = MagicMock()
        client.show_instances = MagicMock(return_value=_instance_stream([_INSTANCE]))
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                AsyncMock(return_value=False),
            ),
        ):
            await _reconcile_orphan_by_label(client, "lbl")  # must not raise
        markers = [r.getMessage() for r in log_records]
        assert any(m.startswith("RECONCILE_ORPHAN_STILL_BILLING") for m in markers), (
            f"expected RECONCILE_ORPHAN_STILL_BILLING; got {markers}"
        )

    @pytest.mark.asyncio
    async def test_delete_instance_cancelled_propagates(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import _reconcile_orphan_by_label

        client = MagicMock()
        client.show_instances = MagicMock(return_value=_instance_stream([_INSTANCE]))
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _reconcile_orphan_by_label(client, "lbl")

    @pytest.mark.asyncio
    async def test_listing_fails_all_attempts(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _reconcile_orphan_by_label,
        )

        client = MagicMock()
        client.show_instances = MagicMock(
            return_value=_instance_stream([], exc=VastAIError("x"))
        )
        client.destroy_instance = AsyncMock()
        with (
            patch("yascheduler.infra.cloud.providers.vastai._RECONCILE_INTERVAL", 0.0),
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
        ):
            await _reconcile_orphan_by_label(client, "lbl")  # must not raise
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_error_not_swallowed(self) -> None:
        # PURPOSE: Guard against asyncio.CancelledError being swallowed by the
        # broad `except Exception` in the listing loop.
        from yascheduler.infra.cloud.providers.vastai import _reconcile_orphan_by_label

        client = MagicMock()
        client.show_instances = MagicMock(
            return_value=_instance_stream([], exc=asyncio.CancelledError())
        )
        client.destroy_instance = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await _reconcile_orphan_by_label(client, "lbl")


# =============================================================================
# _delete_instance
# =============================================================================


class TestDeleteInstance:
    @pytest.mark.asyncio
    async def test_delete_404_returns_true(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _delete_instance,
        )

        client = MagicMock()
        client.destroy_instance = AsyncMock(side_effect=VastAIError("", status=404))
        assert await _delete_instance(client, 1) is True

    @pytest.mark.asyncio
    async def test_delete_permanent_4xx_returns_false(self) -> None:
        # PURPOSE: a permanent 4xx (non-404, e.g. 403) is NOT retried and does
        # NOT raise — _delete_instance returns False so vastai_delete_node
        # raises its "delete failed" message (orchestrator retries with the
        # persisted id). Raising here would leak a billed orphan via the
        # create-cleanup path, where the id is not yet persisted.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _delete_instance,
        )

        client = MagicMock()
        client.destroy_instance = AsyncMock(side_effect=VastAIError("", status=403))
        assert await _delete_instance(client, 1) is False

    @pytest.mark.asyncio
    async def test_delete_transient_5xx_retried_then_accepts(self) -> None:
        # PURPOSE: a transient 5xx on DELETE is retried in-process (not
        # propagated) so the create-cleanup path doesn't leak a billed orphan
        # on a flaky DELETE. Once DELETE is accepted (2xx), the instance is
        # gone — VastAI DELETE is synchronous, no verify poll.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _delete_instance,
        )

        client = MagicMock()
        client.destroy_instance = AsyncMock(
            side_effect=[VastAIError("", status=503), None]
        )
        with patch("yascheduler.infra.cloud.providers.vastai._DELETE_INTERVAL", 0.0):
            assert await _delete_instance(client, 1) is True
        assert client.destroy_instance.await_count == 2

    @pytest.mark.asyncio
    async def test_delete_429_retried_then_accepts(self) -> None:
        # PURPOSE: 429 (rate-limit) on DELETE is transient and retried
        # in-process — the GPU market rate-limits under peak demand, and
        # propagating would leak a billed orphan via the create-cleanup path.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _delete_instance,
        )

        client = MagicMock()
        client.destroy_instance = AsyncMock(
            side_effect=[VastAIError("", status=429), None]
        )
        with patch("yascheduler.infra.cloud.providers.vastai._DELETE_INTERVAL", 0.0):
            assert await _delete_instance(client, 1) is True
        assert client.destroy_instance.await_count == 2

    @pytest.mark.asyncio
    async def test_delete_transient_transport_retried(self) -> None:
        # PURPOSE: status None (transport error) is transient too.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _delete_instance,
        )

        client = MagicMock()
        client.destroy_instance = AsyncMock(side_effect=[VastAIError("net"), None])
        with patch("yascheduler.infra.cloud.providers.vastai._DELETE_INTERVAL", 0.0):
            assert await _delete_instance(client, 1) is True
        assert client.destroy_instance.await_count == 2

    @pytest.mark.asyncio
    async def test_delete_transient_exhausted_returns_false(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            _delete_instance,
        )

        client = MagicMock()
        client.destroy_instance = AsyncMock(side_effect=VastAIError("", status=503))
        with patch("yascheduler.infra.cloud.providers.vastai._DELETE_INTERVAL", 0.0):
            assert await _delete_instance(client, 1) is False
        assert client.destroy_instance.await_count == 3  # _DELETE_ATTEMPTS

    @pytest.mark.asyncio
    async def test_success_returns_true(self) -> None:
        # PURPOSE: VastAI DELETE is synchronous — 2xx means destroyed, so a
        # successful DELETE returns True immediately with no verify poll.
        from yascheduler.infra.cloud.providers.vastai import _delete_instance

        client = MagicMock()
        client.destroy_instance = AsyncMock()
        assert await _delete_instance(client, 1) is True
        assert client.destroy_instance.await_count == 1


# =============================================================================
# wait_until_ready
# =============================================================================


class TestWaitUntilReady:
    @pytest.mark.asyncio
    async def test_ready_returns_instance(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import wait_until_ready

        client = MagicMock()
        client.show_instance = AsyncMock(return_value=_INSTANCE)
        result = await wait_until_ready(client, 1, 30.0)
        assert result == _INSTANCE

    @pytest.mark.asyncio
    async def test_timeout_raises_no_delete(self) -> None:
        # PURPOSE: Guard against the orphan-leak regression where wait_until_ready
        # best-effort-deleted the instance itself. Cleanup is the caller's job;
        # a poll failure must NOT call destroy_instance.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            wait_until_ready,
        )

        client = MagicMock()
        # show_instance returns a non-terminal non-running status forever; force timeout=0
        non_ready = dict(_INSTANCE, actual_status="provisioning")
        client.show_instance = AsyncMock(return_value=non_ready)
        client.destroy_instance = AsyncMock()
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            pytest.raises(VastAIError, match="did not become ready"),
        ):
            await wait_until_ready(client, 1, 0.0)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_status_raises_no_delete(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            wait_until_ready,
        )

        client = MagicMock()
        terminal = dict(_INSTANCE, actual_status="exited")
        client.show_instance = AsyncMock(return_value=terminal)
        client.destroy_instance = AsyncMock()
        with pytest.raises(VastAIError, match="terminal status"):
            await wait_until_ready(client, 1, 30.0)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_show_4xx_raises_no_delete(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            wait_until_ready,
        )

        client = MagicMock()
        client.show_instance = AsyncMock(side_effect=VastAIError("", status=403))
        client.destroy_instance = AsyncMock()
        with pytest.raises(VastAIError, match="status query failed"):
            await wait_until_ready(client, 1, 30.0)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_show_transient_5xx_retries_until_ready(self) -> None:
        # PURPOSE: a transient 429/5xx during readiness must NOT fail the poll
        # — raising here makes vastai_create_node delete a healthy, billing
        # instance (the S6 cost bug). Mirror vultr: treat the blip as "no data
        # this tick" and keep polling until the instance reports ready.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            wait_until_ready,
        )

        client = MagicMock()
        client.show_instance = AsyncMock(
            side_effect=[
                VastAIError("HTTP 500", status=500),
                VastAIError("HTTP 429", status=429),
                _INSTANCE,
            ]
        )
        client.destroy_instance = AsyncMock()
        with patch(
            "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
        ):
            result = await wait_until_ready(client, 1, 30.0)
        assert result == _INSTANCE
        assert client.show_instance.await_count == 3
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_show_transient_until_deadline_raises_timeout_no_delete(
        self,
    ) -> None:
        # PURPOSE: a transient show failure that persists until the deadline
        # surfaces as POLL_TIMEOUT ("did not become ready"), NOT as a show
        # failure — so vastai_create_node's cleanup does not delete a possibly
        # running, billing instance on a provider blip.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            wait_until_ready,
        )

        client = MagicMock()
        client.show_instance = AsyncMock(
            side_effect=VastAIError("HTTP 500", status=500)
        )
        client.destroy_instance = AsyncMock()
        loop = MagicMock()
        # deadline = 0 + 10 = 10; iter1 now=5 (<10 → show→transient, sleep);
        # iter2 now=10 (>=10 → POLL_TIMEOUT).
        loop.time = MagicMock(side_effect=[0, 5, 10])
        with (
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.get_running_loop",
                return_value=loop,
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
            ),
            pytest.raises(VastAIError, match="did not become ready"),
        ):
            await wait_until_ready(client, 1, 10.0)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_terminal_loops(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import wait_until_ready

        client = MagicMock()
        provisioning = dict(_INSTANCE, actual_status="provisioning")
        client.show_instance = AsyncMock(
            side_effect=[provisioning, provisioning, _INSTANCE]
        )
        with patch(
            "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
        ):
            result = await wait_until_ready(client, 1, 30.0)
        assert result == _INSTANCE
        assert client.show_instance.await_count == 3

    @pytest.mark.asyncio
    async def test_cancelled_not_caught_by_vastai_error(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import wait_until_ready

        client = MagicMock()
        client.show_instance = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await wait_until_ready(client, 1, 30.0)

    @pytest.mark.asyncio
    async def test_instance_gone_raises_no_delete(self) -> None:
        # PURPOSE: show_instance returns None when VastAI answers 200 +
        # {"instances": null} (instance deleted/non-existent). The instance
        # cannot become ready; polling MUST raise (terminal), and — like every
        # other failure path here — MUST NOT delete the instance itself
        # (cleanup is the caller's job). Regression for the readiness-poll
        # crash on None that surfaced alongside the verify-gone bug.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            wait_until_ready,
        )

        client = MagicMock()
        client.show_instance = AsyncMock(return_value=None)
        client.destroy_instance = AsyncMock()
        with pytest.raises(VastAIError, match="no longer exists"):
            await wait_until_ready(client, 1, 30.0)
        client.destroy_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_without_ssh_keeps_polling(self) -> None:
        # PURPOSE: a freshly-running instance may briefly report running before
        # ssh_host/port are assigned. Polling must NOT return a half-formed
        # instance (the scheduler cannot connect); keep polling until the
        # endpoint appears.
        from yascheduler.infra.cloud.providers.vastai import wait_until_ready

        client = MagicMock()
        no_ssh = dict(_INSTANCE, actual_status="running", ssh_host=None, ssh_port=None)
        client.show_instance = AsyncMock(side_effect=[no_ssh, no_ssh, _INSTANCE])
        with patch(
            "yascheduler.infra.cloud.providers.vastai.asyncio.sleep", AsyncMock()
        ):
            result = await wait_until_ready(client, 1, 30.0)
        assert result == _INSTANCE
        assert client.show_instance.await_count == 3


# =============================================================================
# vastai_create_node
# =============================================================================


class TestCreateNode:
    @pytest.mark.asyncio
    async def test_happy_path_returns_dto(self, log_records: list) -> None:
        from yascheduler.infra.cloud.providers.vastai import vastai_create_node

        cfg = _cfg(jump_host="j.example.com", jump_port=2222, jump_username="ju")
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.create_ssh_key = AsyncMock()
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(return_value=7)
        mock_client.show_instance = AsyncMock(return_value=_INSTANCE)
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-XYZ",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
        ):
            result = await vastai_create_node(cfg, _key())
        assert result.external_id == "7"
        assert result.hostname == "1.2.3.4"
        assert result.port == 22
        assert result.username == "root"
        assert result.jump_host == "j.example.com"
        assert result.jump_port == 2222
        assert result.jump_username == "ju"
        # Block markers emitted in order.
        markers = _markers(log_records)
        for m in [
            "SSH_KEY_CHECK",
            "OFFER_SEARCH_START",
            "OFFER_SELECT",
            "INSTANCE_CREATE",
            "INSTANCE_READY",
        ]:
            assert m in markers

    @pytest.mark.asyncio
    async def test_api_key_absent_from_logs(self, log_records: list) -> None:
        # PURPOSE: api_key lives only in the Authorization header; ensure it
        # never leaks into a structured log field.
        from yascheduler.infra.cloud.providers.vastai import vastai_create_node

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(return_value=7)
        mock_client.show_instance = AsyncMock(return_value=_INSTANCE)
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-XYZ",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
        ):
            await vastai_create_node(cfg, _key())
        for rec in log_records:
            for v in extra_fields(rec).values():
                assert cfg.api_key not in str(v)

    @pytest.mark.asyncio
    async def test_create_label_is_unique(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import vastai_create_node

        cfg = _cfg(label="yascheduler")
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(return_value=7)
        mock_client.show_instance = AsyncMock(return_value=_INSTANCE)
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-ABC",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
        ):
            await vastai_create_node(cfg, _key())
        label = mock_client.create_instance.call_args.kwargs["label"]
        assert label == "yascheduler-ABC"
        assert label.startswith(cfg.label)
        assert label != cfg.label

    @pytest.mark.asyncio
    async def test_create_instance_failure_reconciles(self) -> None:
        # PURPOSE: when create_instance raises (transport break / shape), id is
        # unknown — reconcile by label must run before re-raise so no orphan bills.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_create_node,
        )

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(side_effect=VastAIError("break"))
        reconcile = AsyncMock()
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-ABC",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai._reconcile_orphan_by_label",
                reconcile,
            ),
            pytest.raises(VastAIError, match="break"),
        ):
            await vastai_create_node(cfg, _key())
        reconcile.assert_awaited_once_with(mock_client, "yascheduler-ABC")

    @pytest.mark.asyncio
    async def test_wait_until_ready_failure_deletes_and_reraises(self) -> None:
        # PURPOSE: instance exists and bills after create_instance succeeds; a
        # poll failure MUST run _delete_instance (delete the known id, not
        # best-effort) before re-raising so no billable orphan leaks.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_create_node,
        )

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(return_value=7)
        # Poll always fails (terminal) → wait_until_ready raises.
        terminal = dict(_INSTANCE, actual_status="exited")
        mock_client.show_instance = AsyncMock(return_value=terminal)
        mock_client.destroy_instance = AsyncMock()
        dav = AsyncMock(return_value=True)
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-ABC",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                dav,
            ),
            pytest.raises(VastAIError, match="terminal status"),
        ):
            await vastai_create_node(cfg, _key())
        dav.assert_awaited_once_with(mock_client, 7)

    @pytest.mark.asyncio
    async def test_ensure_ssh_key_failure_propagates_no_create(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_create_node,
        )

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(side_effect=VastAIError("keys down"))
        mock_client.create_instance = AsyncMock()
        with (
            _patch_vastai_client(mock_client),
            pytest.raises(VastAIError, match="keys down"),
        ):
            await vastai_create_node(cfg, _key())
        mock_client.create_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_ssh_key_returns_false_aborts_no_create(self) -> None:
        # PURPOSE: success:false = key not registered; create MUST abort before
        # launching a billed instance. No instance created, so no reconcile.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_create_node,
        )

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(return_value=[])
        mock_client.create_ssh_key = AsyncMock(return_value=False)
        mock_client.create_instance = AsyncMock()
        with (
            _patch_vastai_client(mock_client),
            pytest.raises(VastAIError, match="SSH key registration refused"),
        ):
            await vastai_create_node(cfg, _key())
        mock_client.create_instance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_instance_cancelled_reconciles_and_reraises(self) -> None:
        # PURPOSE: cancellation mid-PUT must still reconcile a possible orphan
        # (the server may have accepted the create) before re-raising —
        # `except Exception` would skip reconcile and leak a billed instance.
        # CancelledError is BaseException since Py3.8.
        from yascheduler.infra.cloud.providers.vastai import vastai_create_node

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(side_effect=asyncio.CancelledError())
        reconcile = AsyncMock()
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-ABC",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai._reconcile_orphan_by_label",
                reconcile,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await vastai_create_node(cfg, _key())
        reconcile.assert_awaited_once_with(mock_client, "yascheduler-ABC")

    @pytest.mark.asyncio
    async def test_wait_until_ready_cancelled_deletes_and_reraises(self) -> None:
        # PURPOSE: the instance already exists and bills when readiness polling
        # starts. Shutdown-driven cancellation during the (up to connect_grace=
        # 300s) poll loop MUST delete the known instance — `except Exception`
        # would orphan it. Mirrors manager.allocate's BaseException guard.
        from yascheduler.infra.cloud.providers.vastai import vastai_create_node

        cfg = _cfg()
        mock_client = MagicMock()
        mock_client.get_ssh_keys = AsyncMock(
            return_value=[{"public_key": _PUBKEY.decode()}]
        )
        mock_client.search_offers = AsyncMock(return_value=[_OFFER])
        mock_client.create_instance = AsyncMock(return_value=7)
        dav = AsyncMock(return_value=True)
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai.get_rnd_name",
                return_value="yascheduler-ABC",
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.random.choice", lambda x: x[0]
            ),
            patch(
                "yascheduler.infra.cloud.providers.vastai.wait_until_ready",
                AsyncMock(side_effect=asyncio.CancelledError()),
            ),
            patch("yascheduler.infra.cloud.providers.vastai._delete_instance", dav),
            pytest.raises(asyncio.CancelledError),
        ):
            await vastai_create_node(cfg, _key())
        dav.assert_awaited_once_with(mock_client, 7)


# =============================================================================
# vastai_delete_node
# =============================================================================


class TestDeleteNode:
    @pytest.mark.asyncio
    async def test_bad_external_id_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_delete_node,
        )

        with pytest.raises(VastAIError, match="Invalid VastAI instance id"):
            await vastai_delete_node(_cfg(), "abc")

    @pytest.mark.asyncio
    async def test_delete_confirmed_returns_none(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import vastai_delete_node

        mock_client = MagicMock()
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                AsyncMock(return_value=True),
            ),
        ):
            await vastai_delete_node(_cfg(), "7")

    @pytest.mark.asyncio
    async def test_delete_failed_raises(self) -> None:
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_delete_node,
        )

        mock_client = MagicMock()
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                AsyncMock(return_value=False),
            ),
            pytest.raises(VastAIError, match="delete failed"),
        ):
            await vastai_delete_node(_cfg(), "7")

    @pytest.mark.asyncio
    async def test_delete_instance_unexpected_raise_propagates(self) -> None:
        # PURPOSE: _delete_instance is contracted never to raise, but if it
        # ever does (defensive), vastai_delete_node must propagate so the
        # orchestrator sees the failure rather than silently succeeding.
        from yascheduler.infra.cloud.providers.vastai import (
            VastAIError,
            vastai_delete_node,
        )

        mock_client = MagicMock()
        with (
            _patch_vastai_client(mock_client),
            patch(
                "yascheduler.infra.cloud.providers.vastai._delete_instance",
                AsyncMock(side_effect=VastAIError("unexpected", status=500)),
            ),
            pytest.raises(VastAIError, match="unexpected"),
        ):
            await vastai_delete_node(_cfg(), "7")
