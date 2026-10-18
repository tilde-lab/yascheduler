# region MODULE_CONTRACT
# PURPOSE: Real-Vultr live test for SSH-key registration — upload once, reuse on sequential and concurrent lookups, no account duplicates, guaranteed cleanup.
# SCOPE: opt-in env-gated test (YASCHEDULER_TEST_VULTR=1 + VULTR_API_KEY); drives get_ssh_key_id directly with a throwaway key; asserts id reuse and exactly one account key per body; deletes every account key carrying that body in finally with loud-fail-on-leak.
# DEPENDENCIES: USES API: api.vultr.com/v2 (aiohttp via VultrClient)
# KEYWORDS: Vultr, ssh keys, live, duplicates, reuse, cleanup
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import logging
import os

import asyncssh
import pytest

from yascheduler.infra.cloud.providers.vultr import VultrClient, get_ssh_key_id

pytestmark = pytest.mark.integration

log = logging.getLogger("integration.test_vultr_live")

# Listing visibility windows: Vultr listings are usually read-after-write
# consistent, but the test tolerates brief lag instead of failing on it —
# it asserts OUR dedup logic, not Vultr's replication.
_LIST_TIMEOUT_S = 30.0
_GONE_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 1.0
_CONCURRENT_CALLS = 3


def _vultr_env_or_skip() -> str:
    if os.environ.get("YASCHEDULER_TEST_VULTR") != "1":
        pytest.skip(
            "YASCHEDULER_TEST_VULTR != 1; set YASCHEDULER_TEST_VULTR=1 to enable",
        )
    api_key = os.environ.get("VULTR_API_KEY", "")
    if not api_key:
        pytest.skip(
            "VULTR_API_KEY unset/empty; set it to a real Vultr API key",
        )
    return api_key


async def _matching_key_ids(client: VultrClient, body: str) -> list[str]:
    """Ids of account keys whose stored body matches (whitespace-trimmed)."""
    return [
        key["id"]
        async for key in client.get_ssh_keys()
        if key["ssh_key"].strip() == body
    ]


async def _wait_visible(
    client: VultrClient,
    body: str,
    timeout_s: float,
) -> list[str]:
    """Poll the listing until at least one key with the body shows up."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        ids = await _matching_key_ids(client, body)
        if ids:
            return ids
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"created key not visible in Vultr listing within {timeout_s}s")


async def _wait_gone(
    client: VultrClient,
    body: str,
    timeout_s: float,
) -> None:
    """Poll the listing until no key with the body remains."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if not await _matching_key_ids(client, body):
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"keys with the test body still listed after {timeout_s}s")


async def _cleanup_by_body(client: VultrClient, body: str) -> None:
    """Delete EVERY account key carrying the body — even duplicates a buggy
    code path managed to create — then assert none remain (leak fails loudly).
    """
    for key_id in await _matching_key_ids(client, body):
        try:
            await client.delete_ssh_key(key_id)
        except Exception as err:  # noqa: PERF203 — best-effort per key
            log.error(
                "[vultr_live][CLEANUP] delete_ssh_key(%s) failed: %s", key_id, err
            )
    await _wait_gone(client, body, _GONE_TIMEOUT_S)
    left = await _matching_key_ids(client, body)
    if left:
        pytest.fail(f"Vultr SSH keys {left} were NOT deleted — manual cleanup required")


async def test_vultr_ssh_key_no_duplicates_live() -> None:
    """get_ssh_key_id against the real API: upload once, reuse, no duplicates.

    Regression coverage for both duplicate causes: the whitespace-trimmed
    match (sequential reuse) and the list->miss->create race (concurrent
    reuse, the Vultr adapter runs with op_limit=2).
    """
    api_key = _vultr_env_or_skip()

    # Throwaway key shaped like the daemon's: fresh RSA, comment = key name.
    key_name = f"yakey-itest-{os.urandom(4).hex()}"
    key = asyncssh.generate_private_key(alg_name="ssh-rsa", comment=key_name)
    # Compare by the trimmed body: the daemon's export carries a trailing
    # newline Vultr strips on storage.
    pub_body = key.export_public_key("openssh").decode("utf-8").strip()

    async with VultrClient(api_key) as client:
        try:
            # First allocation: uploads the key.
            first_id = await get_ssh_key_id(client, key)
            assert first_id, "get_ssh_key_id returned an empty id"
            assert first_id in await _wait_visible(client, pub_body, _LIST_TIMEOUT_S)

            # Second allocation (sequential): must reuse, not re-upload.
            second_id = await get_ssh_key_id(client, key)
            assert second_id == first_id, (
                f"sequential lookup re-uploaded: {first_id} != {second_id}"
            )

            # Concurrent allocations (op_limit=2 window): all reuse one id.
            concurrent_ids = await asyncio.gather(
                *[get_ssh_key_id(client, key) for _ in range(_CONCURRENT_CALLS)]
            )
            assert set(concurrent_ids) == {first_id}, (
                f"concurrent lookups created duplicates: {concurrent_ids}"
            )

            # Exactly one account key carries the body — no duplicates left.
            final_ids = await _matching_key_ids(client, pub_body)
            assert final_ids == [first_id], f"duplicate keys on account: {final_ids}"
        finally:
            await _cleanup_by_body(client, pub_body)
