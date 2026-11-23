"""VastAI cloud provider."""
# region MODULE_CONTRACT
# PURPOSE: Provision and decommission VastAI GPU instances so the scheduler can run compute workloads on transient VastAI hosts through the generic CloudAdapter contract.
# SCOPE: cloud-side lifecycle only, NOT DB/UoW/SSH-setup/allocator.
# DEPENDENCIES: USES API: cloud.vast.ai (aiohttp)
# KEYWORDS: vastai, provider, create, delete, ssh key, offers, instances
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TYPE_CHECKING, Literal, TypedDict

import aiohttp

from yascheduler.infra.cloud import CloudCreateNodeDTO
from yascheduler.infra.cloud.utils import get_rnd_name

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from asyncssh.public_key import SSHKey as ASSHKey

    from yascheduler.infra.cloud.cloud_configs import ConfigCloudVastAI
    from yascheduler.infra.cloud.cloud_init import CloudInitConfig
    from yascheduler.shared import Required, Self, TypeGuard, Unpack

__all__ = ["vastai_create_node", "vastai_delete_node"]
logger = logging.getLogger(__name__)

_VASTAI_BASE_URL = "https://console.vast.ai/api/v0/"
_HTTP_BAD_REQUEST = 400
_HTTP_NOT_FOUND = 404
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR = 500

# Best-effort orphan reconcile after an ambiguous non-idempotent create.
# VastAI listing is eventually consistent and unpredictably laggy
_RECONCILE_ATTEMPTS = 10
_RECONCILE_INTERVAL = 15.0

# DELETE retry loop: a transient 5xx/transport failure on the DELETE itself is
# retried in-process rather than propagated, so the create-cleanup path (which
# has no persisted id to retry against next cycle) doesn't leak a billed orphan.
_DELETE_ATTEMPTS = 3
_DELETE_INTERVAL = 5.0


# region BLOCK_API_errors
# PURPOSE: VastAI API error type carrying the HTTP status, with a transient classifier for retry decisions.
class VastAIError(Exception):
    """VastAI API error carrying the HTTP status.

    `status` is the HTTP status when the error originated from an HTTP
    response, or None for transport-level failures.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)

    @property
    def transient(self) -> bool:
        """True for 429/5xx responses and transport failures — worth retrying."""
        return (
            self.status is None
            or self.status == _HTTP_TOO_MANY_REQUESTS
            or self.status >= _HTTP_SERVER_ERROR
        )


# endregion BLOCK_API_errors

# region BLOCK_API_typed_dicts
# PURPOSE: TypedDict mirrors of the Vast.ai REST API response/request shapes, so client methods and validators share one structural contract.


class VastAISSHKey(TypedDict):
    public_key: str


class VastAICreateSSHKeyResponse(TypedDict):
    success: bool
    key: VastAISSHKey


class VastAIOffer(TypedDict):
    id: int
    dph_total: float | int


class VastAIOffersResponse(TypedDict):
    offers: list[VastAIOffer]


class VastAICreateInstanceResponse(TypedDict):
    new_contract: int | float


class VastAIInstance(TypedDict):
    id: int | float
    actual_status: str | None
    # ssh endpoint is unset until the instance reaches running
    ssh_host: str | None
    ssh_port: int | float | None


class VastAIShowInstanceResponse(TypedDict):
    # VastAI answers GET /instances/{id}/ with 200 + {"instances": null} for a
    # deleted/non-existent id
    instances: VastAIInstance | None


class VastAIShowInstancesResponse(TypedDict):
    next_token: str | None
    instances: list[VastAIInstance]


VastAIFilter = dict[str, str | int | float | bool | list[str]]


class VastAISearchOffersFilters(TypedDict, total=False):
    duration: VastAIFilter
    gpu_ram: VastAIFilter
    num_gpus: VastAIFilter
    gpu_frac: VastAIFilter
    reliability: VastAIFilter
    rentable: VastAIFilter
    rented: VastAIFilter
    dph_total: VastAIFilter


class VastAICreateInstanceParams(TypedDict, total=False):
    image: Required[str]
    label: str
    disk: int
    env: str
    vm: bool
    onstart: str


class VastAIShowInstancesFilters(TypedDict, total=False):
    label: VastAIFilter


# endregion BLOCK_API_typed_dicts

# region BLOCK_API_validators
# PURPOSE: TypeGuard validators narrowing untyped API JSON to the TypedDict shapes above, so client methods fail fast on shape drift with a VastAIError rather than silently misreading fields.


def _is_api_ssh_key(resp: object) -> TypeGuard[VastAISSHKey]:
    return isinstance(resp, dict) and isinstance(resp.get("public_key"), str)


def _is_api_ssh_keys_list(resp: object) -> TypeGuard[list[VastAISSHKey]]:
    return isinstance(resp, list) and all(_is_api_ssh_key(x) for x in resp)


def _is_api_ssh_key_created(resp: object) -> TypeGuard[VastAICreateSSHKeyResponse]:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("success"), bool)
        and "key" in resp
        and _is_api_ssh_key(resp["key"])
    )


def _is_api_offer(resp: object) -> TypeGuard[VastAIOffer]:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("id"), (int, float))
        and isinstance(resp.get("dph_total"), (int, float))
    )


def _is_api_offers_list(resp: object) -> TypeGuard[VastAIOffersResponse]:
    return (
        isinstance(resp, dict)
        and "offers" in resp
        and all(_is_api_offer(x) for x in resp["offers"])
    )


def _is_api_create_instance_resp(
    resp: object,
) -> TypeGuard[VastAICreateInstanceResponse]:
    return isinstance(resp, dict) and isinstance(resp.get("new_contract"), (float, int))


def _is_api_instance(resp: object) -> TypeGuard[VastAIInstance]:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("id"), (int, float))
        and isinstance(resp.get("actual_status"), (str, type(None)))
        and (resp.get("ssh_host") is None or isinstance(resp.get("ssh_host"), str))
        and (
            resp.get("ssh_port") is None
            or isinstance(resp.get("ssh_port"), (int, float))
        )
    )


def _is_api_show_instance_resp(resp: object) -> TypeGuard[VastAIShowInstanceResponse]:
    # `instances` is null when the id is non-existent
    return (
        isinstance(resp, dict)
        and "instances" in resp
        and (resp["instances"] is None or _is_api_instance(resp["instances"]))
    )


def _is_api_show_instances_resp(resp: object) -> TypeGuard[VastAIShowInstancesResponse]:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("next_token"), (str, type(None)))
        and isinstance(resp.get("instances"), list)
        and all(_is_api_instance(x) for x in resp["instances"])
    )


# endregion BLOCK_API_validators


# region CLASS_VastAIClient
# PURPOSE: Wrap a single aiohttp session authenticated to the VastAI API so repeated create/poll/delete calls reuse one connection pool instead of re-handshaking per request.
class VastAIClient:
    """Async Vastai REST API client (aiohttp-based)."""

    def __init__(self, api_key: str) -> None:
        self._session = aiohttp.ClientSession(
            base_url=_VASTAI_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
            },
            timeout=aiohttp.ClientTimeout(total=60),
        )

    async def close(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: aiohttp.typedefs.Query | None = None,
        data: dict | None = None,
    ) -> object:
        """Send an async HTTP request to the Vastai API; empty 2xx body -> None."""
        try:
            async with self._session.request(
                method, path, params=params, json=data
            ) as resp:
                logger.debug(
                    "VASTAI_REQUEST",
                    extra={"method": method, "path": path, "status": resp.status},
                )
                if resp.status >= _HTTP_BAD_REQUEST:
                    msg = f"HTTP {resp.status}: {await resp.text()}"
                    raise VastAIError(msg, status=resp.status)
                # DELETE may 2xx with empty body; json() would raise a bogus
                # non-transient VastAIError(status=200). Mirrors vultr.
                raw = await resp.text()
                return json.loads(raw) if raw else None
        except aiohttp.ClientResponseError as exc:
            msg = f"HTTP request failed: {exc.message}"
            raise VastAIError(msg, status=exc.status) from exc
        except json.JSONDecodeError as exc:
            msg = f"Invalid JSON response: {exc}"
            raise VastAIError(msg) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            msg = f"Transport error: {exc}"
            raise VastAIError(msg) from exc

    async def get_ssh_keys(self) -> list[VastAISSHKey]:
        resp = await self._request("GET", "ssh")
        if not _is_api_ssh_keys_list(resp):
            msg = f"Invalid SSH key list response: {resp}"
            raise VastAIError(msg)
        return resp

    async def create_ssh_key(self, ssh_key: str) -> bool:
        resp = await self._request("POST", "ssh", data={"ssh_key": ssh_key})
        if not _is_api_ssh_key_created(resp):
            msg = f"Invalid SSH key create response: {resp}"
            raise VastAIError(msg)
        return resp["success"] is True

    async def search_offers(
        self,
        instance_type: str = "ondemand",
        order: list[tuple[str, Literal["asc", "desc"]]] | None = None,
        limit: int = 20,
        **filters: Unpack[VastAISearchOffersFilters],
    ) -> list[VastAIOffer]:
        data = dict(filters)
        data["type"] = instance_type
        if order:
            data["order"] = [list(x) for x in order]
        if limit:
            data["limit"] = limit
        resp = await self._request("POST", "bundles", data=data)
        if _is_api_offers_list(resp):
            offers = resp["offers"]
            logger.debug("OFFER_SEARCH", extra={"offer_count": len(offers)})
            return offers
        msg = f"Invalid offers list response: {resp}"
        raise VastAIError(msg)

    async def create_instance(
        self, ask_id: int, **params: Unpack[VastAICreateInstanceParams]
    ) -> int:
        data = dict(
            target_state="running", runtype="ssh_proxy", cancel_unavail=True, **params
        )
        resp = await self._request("PUT", f"asks/{ask_id}", data=data)
        if not _is_api_create_instance_resp(resp):
            msg = f"Invalid create instance response: {resp}"
            raise VastAIError(msg)
        return int(resp["new_contract"])

    async def destroy_instance(self, instance_id: int) -> None:
        await self._request("DELETE", f"instances/{instance_id}")

    async def show_instance(self, instance_id: int) -> VastAIInstance | None:
        resp = await self._request("GET", f"instances/{instance_id}")
        if not _is_api_show_instance_resp(resp):
            msg = f"Invalid show instance response: {resp}"
            raise VastAIError(msg)
        # None = the instance is gone (200 + {"instances": null}).
        return resp["instances"]

    async def show_instances(
        self, select_filters: VastAIShowInstancesFilters | None = None, limit: int = 25
    ) -> AsyncIterator[VastAIInstance]:
        """Yield instances one at a time, paginating through all pages."""
        params: dict = {"limit": limit}
        if select_filters:
            params["select_filters"] = json.dumps(select_filters)
        while True:
            resp = await self._request("GET", "instances", params=params)
            if not _is_api_show_instances_resp(resp):
                msg = f"Invalid show instances response: {resp}"
                raise VastAIError(msg)
            for instance in resp["instances"]:
                yield instance
            if not (after_token := resp.get("next_token")):
                break
            params["after_token"] = after_token


# endregion CLASS_VastAIClient


# region FUNC_ensure_ssh_key
# PURPOSE: Ensure the operator SSH key is registered so the freshly created instance authorizes it and the scheduler can connect without password auth.
# REQUIRES: public_key is a valid SSH public key string.
# ENSURES: Key is registered on the account
async def ensure_ssh_key(
    client: VastAIClient,
    public_key: str,
) -> bool:
    """Ensure the SSH public key is registered on the VastAI account."""
    logger.debug("SSH_KEY_PRESENCE_CHECK", extra={"public_key": public_key})
    for k in await client.get_ssh_keys():
        if k["public_key"].strip() == public_key.strip():
            return True

    success = await client.create_ssh_key(public_key)
    logger.debug(
        "SSH_KEY_REGISTERED", extra={"public_key": public_key, "success": success}
    )
    return success


# endregion FUNC_ensure_ssh_key


# region FUNC_select_cheapest_offer
# PURPOSE: Bound spend under the configured ceiling and spread jobs off a single flaky host — random pick from the five cheapest rather than always the cheapest.
# REQUIRES: offers is a list of offers.
# ENSURES: Returns a random offer from the top-5 cheapest; raises on empty list or any offer exceeding the ceiling.
async def select_cheapest_offer(
    offers: list[VastAIOffer],
    max_price_per_hr: float,
) -> VastAIOffer:
    """Pick a random offer from the top-5 cheapest in the list."""
    if not offers:
        msg = "No offers found matching the configured criteria"
        raise VastAIError(msg)

    # Not cheapest, but random from the top-5 cheapest
    # Don't fall into the same broken provider
    offer = random.choice(sorted(offers, key=lambda x: x["dph_total"])[:5])  # noqa: S311

    dph_total = offer["dph_total"]
    if dph_total > max_price_per_hr:
        msg = f"Offer price {dph_total} exceeds max price {max_price_per_hr}"
        raise VastAIError(msg)

    logger.debug(
        "SELECTED_OFFER",
        extra={"offer_id": offer["id"], "dph_total": dph_total},
    )
    return offer


# endregion FUNC_select_cheapest_offer


# region FUNC_generate_onstart
# PURPOSE: Flatten the cloud-init config into one shell script because VastAI's onstart is a single script, not cloud-init.
# ENSURES: Returns a startup script string.
async def generate_onstart(
    cfg: ConfigCloudVastAI,
    cloud_config: CloudInitConfig | None = None,
) -> str:
    """Generate the onstart script for a VastAI instance."""
    if cfg.onstart_script is not None:
        return cfg.onstart_script

    if cloud_config is None:
        return ""

    image = cfg.image
    is_kvm = detect_launch_mode(image) == "kvm"
    use_apt = "debian" in image.lower() or "ubuntu" in image.lower()
    pm = "apt-get" if use_apt else "dnf"

    lines: list[str] = []
    if is_kvm:
        lines.append("#!/bin/bash")

    if cloud_config.package_upgrade:
        lines.append(
            "apt-get update && apt-get upgrade -y" if use_apt else "dnf upgrade -y",
        )

    if cloud_config.packages:
        lines.append(f"{pm} install -y {' '.join(cloud_config.packages)}")

    for cmd in cloud_config.bootcmd:
        if isinstance(cmd, list):
            lines.extend(cmd)
        else:
            lines.append(cmd)

    return "\n".join(lines)


# endregion FUNC_generate_onstart


# region FUNC__reconcile_orphan_by_label
# PURPOSE: Close the non-idempotent-create orphan window by matching and best-effort deleting an instance created during a failed/ambiguous PUT, using the unique label generated pre-PUT.
# ENSURES: Never raises — original create error propagates regardless. Retries listing _RECONCILE_ATTEMPTS times. On label match: _delete_instance exception swallowed (RECONCILE_DELETE_FAILED); False return (no captured id/DB row to retry) logged as ERROR RECONCILE_ORPHAN_STILL_BILLING; CancelledError propagates.
async def _reconcile_orphan_by_label(
    client: VastAIClient,
    label: str,
) -> None:
    """Best-effort delete of an instance created during an ambiguous PUT.

    PUT /asks/{offer_id}/ is not idempotent: if the transport breaks after the
    server accepted the create, an instance exists that we never got an id for.
    Match it by the unique label generated pre-PUT and delete it. Never raises.
    """
    for attempt in range(1, _RECONCILE_ATTEMPTS + 1):
        orphan_id: int | float | None = None
        try:
            async for inst in client.show_instances(
                select_filters={"label": {"eq": label}}
            ):
                orphan_id = inst["id"]
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "RECONCILE_LIST_TRANSIENT",
                extra={"label": label, "attempt": attempt},
            )
            if attempt < _RECONCILE_ATTEMPTS:
                await asyncio.sleep(_RECONCILE_INTERVAL)
                continue
            logger.warning(
                "RECONCILE_LOOKUP_FAILED — potential orphan billing; manual check needed",
                extra={"label": label},
            )
            return
        if orphan_id is not None:
            logger.warning(
                "RECONCILE_DELETE_ORPHAN",
                extra={"label": label, "instance_id": orphan_id},
            )
            # False (not deleted) = billed orphan with no captured id /
            # DB row to retry against — log ERROR, don't silently return.
            try:
                gone = await _delete_instance(client, int(orphan_id))
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logger.warning(
                    "RECONCILE_DELETE_FAILED",
                    extra={"label": label, "instance_id": orphan_id, "error": str(err)},
                )
                return
            if not gone:
                logger.error(
                    "RECONCILE_ORPHAN_STILL_BILLING — instance %s may still bill; "
                    "manual deletion required via VastAI console",
                    orphan_id,
                    extra={"label": label, "instance_id": orphan_id},
                )
            return
        logger.debug(
            "RECONCILE_NO_ORPHAN",
            extra={"label": label, "attempt": attempt},
        )
        if attempt < _RECONCILE_ATTEMPTS:
            await asyncio.sleep(_RECONCILE_INTERVAL)
    logger.warning(
        "RECONCILE_NO_ORPHAN — %s listing attempts found no match for label %s",
        _RECONCILE_ATTEMPTS,
        label,
    )


# endregion FUNC__reconcile_orphan_by_label


# region FUNC__delete_instance
# PURPOSE: Delete a VastAI instance by known id with transient retry
# ENSURES: Returns True iff the instance is gone — DELETE accepted or DELETE 404. Returns False if DELETE permanently fails (4xx non-404/non-429) or transient retries exhaust. Never raises — this serves both create-cleanup and delete_node.
async def _delete_instance(client: VastAIClient, instance_id: int) -> bool:
    """Delete an instance with transient retry."""
    for attempt in range(1, _DELETE_ATTEMPTS + 1):
        try:
            await client.destroy_instance(instance_id)
        except VastAIError as err:
            if err.status == _HTTP_NOT_FOUND:
                logger.warning("Instance %s already gone (DELETE 404)", instance_id)
                return True
            if err.transient and attempt < _DELETE_ATTEMPTS:
                logger.debug(
                    "DELETE_TRANSIENT_RETRY",
                    extra={
                        "instance_id": instance_id,
                        "attempt": attempt,
                        "attempts": _DELETE_ATTEMPTS,
                        "status": err.status,
                    },
                )
                await asyncio.sleep(_DELETE_INTERVAL)
                continue
            # Permanent 4xx / retries exhausted — expected, not a crash.
            logger.warning(
                "Instance %s delete failed after %s attempts (last status=%s)",
                instance_id,
                attempt,
                err.status,
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Instance %s delete failed", instance_id)
            return False
        logger.debug("INSTANCE_DELETED", extra={"instance_id": instance_id})
        return True
    return False


# endregion FUNC__delete_instance


# region FUNC_wait_until_ready
# PURPOSE: Bridge VastAI's async create (returns before ssh_host/port exist) so create_node only reports success once the scheduler can actually connect.
# REQUIRES: client is an open VastAIClient; instance_id is a valid instance id.
# ENSURES: Returns the instance dict when actual_status == "running". Raises VastAIError on timeout, terminal status, instance-gone (None), or a permanent (4xx non-429) show-instance failure; a transient (429/5xx/transport) show-instance failure is retried until the deadline. Does NOT delete the instance — vastai_create_node wraps this in try/except and runs _delete_instance so the instance is deleted, not best-effort-gone.
async def wait_until_ready(
    client: VastAIClient, instance_id: int, timeout: float
) -> VastAIInstance:
    """Poll until the VastAI instance is ready."""
    deadline = asyncio.get_running_loop().time() + timeout
    poll_interval = 5.0

    while True:
        now = asyncio.get_running_loop().time()
        if now >= deadline:
            logger.debug("POLL_TIMEOUT", extra={"instance_id": instance_id})
            msg = f"Instance {instance_id} did not become ready within {timeout}s"
            raise VastAIError(msg)

        try:
            inst = await client.show_instance(instance_id)
        except asyncio.CancelledError:
            raise
        except VastAIError as err:
            if err.transient:
                logger.debug(
                    "POLL_TRANSIENT_RETRY",
                    extra={"instance_id": instance_id, "status": err.status},
                )
                await asyncio.sleep(poll_interval)
                continue
            logger.debug(
                "POLL_SHOW_FAILED",
                extra={"instance_id": instance_id, "status": err.status},
            )
            msg = f"Instance {instance_id} status query failed: {err}"
            raise VastAIError(msg, status=err.status) from err
        # None = the instance is gone (200 + {"instances": null}); it cannot
        # become ready, and the caller's cleanup will DELETE an already-gone
        # id harmlessly (DELETE 404 → already-gone).
        if inst is None:
            logger.debug("POLL_INSTANCE_GONE", extra={"instance_id": instance_id})
            msg = f"Instance {instance_id} no longer exists (gone during readiness)"
            raise VastAIError(msg)
        status = inst["actual_status"]

        logger.debug(
            "POLL_STATUS",
            extra={"instance_id": instance_id, "status": status},
        )

        if status == "running":
            # A running instance must have its ssh endpoint assigned; if not,
            # keep polling rather than returning a half-formed instance — the
            # VastAIInstance type allows None (reconcile needs that for fresh
            # orphans), but the scheduler cannot connect without host/port.
            if not inst["ssh_host"] or inst["ssh_port"] is None:
                logger.debug(
                    "POLL_RUNNING_NO_SSH",
                    extra={"instance_id": instance_id},
                )
                await asyncio.sleep(poll_interval)
                continue
            logger.debug(
                "INSTANCE_READY",
                extra={
                    "instance_id": instance_id,
                    "ssh_host": inst["ssh_host"],
                    "ssh_port": inst["ssh_port"],
                },
            )
            logger.info(
                "NEW INSTANCE! %s with root@%s -P %s",
                instance_id,
                inst["ssh_host"],
                inst["ssh_port"],
            )
            return inst

        if status in ("stopped", "frozen", "exited", "unknown", "offline"):
            logger.debug(
                "POLL_TERMINAL",
                extra={"instance_id": instance_id, "status": status},
            )
            msg = f"Instance {instance_id} entered terminal status: {status}"
            raise VastAIError(msg)

        await asyncio.sleep(poll_interval)


# endregion FUNC_wait_until_ready


def detect_launch_mode(image: str) -> str:
    """Detect the launch mode (kvm or docker) from the image name."""
    return "kvm" if "vastai/kvm" in image else "docker"


# region FUNC_vastai_create_node
# PURPOSE: Provision a VastAI GPU instance via the CloudAdapter interface so the generic provisioner can launch VastAI compute nodes.
# ENSURES: Returns CloudCreateNodeDTO with external_id = instance id, hostname = SSH host, port = SSH port.
# INVARIANTS:
# - external_id = instance id; session closed on all paths.
# - Never raises after an instance was created without best-effort removing it: a failed create call reconciles any instance matching the unique per-create label before re-raising.
# - Readiness polling cleans up the known instance on every failure path that leaves the poll loop.
async def vastai_create_node(
    cfg: ConfigCloudVastAI,
    key: ASSHKey,
    cloud_config: CloudInitConfig | None = None,
) -> CloudCreateNodeDTO:
    """Create a VastAI GPU instance."""
    public_key = key.export_public_key().decode()

    async with VastAIClient(cfg.api_key) as client:
        logger.debug("SSH_KEY_CHECK", extra={})
        # False = key not registered; proceeding would launch a billed GPU
        # instance the scheduler can never SSH into. No instance created yet.
        if not await ensure_ssh_key(client, public_key):
            msg = "SSH key registration refused by VastAI (success:false)"
            raise VastAIError(msg)

        logger.debug("OFFER_SEARCH_START", extra={})
        offers = await client.search_offers(
            instance_type="ondemand",
            order=[("dph_total", "asc")],
            limit=20,
            duration={"gte": 60},
            gpu_ram={"gte": cfg.min_vram_mb},
            num_gpus={"eq": cfg.num_gpus},
            gpu_frac={"gte": 1.0},
            reliability={"gte": 0.99},
            rentable={"eq": True},
            rented={"eq": False},
            dph_total={"lte": cfg.max_price_per_hr},
        )

        logger.debug("OFFER_SELECT", extra={})
        offer = await select_cheapest_offer(offers, cfg.max_price_per_hr)

        onstart = await generate_onstart(cfg, cloud_config)
        mode = detect_launch_mode(cfg.image)

        # Unique per-create label: PUT /asks is not idempotent, so a transport
        # break after the server accepted the create loses the returned id. The
        # unique label lets _reconcile_orphan_by_label target only the instance
        # this create produced, never other instances on the same account. The
        # configured cfg.label prefix is retained so broad filters (e2e cleanup)
        # still match via startswith.
        create_label = get_rnd_name(cfg.label)
        try:
            instance_id = await client.create_instance(
                ask_id=offer["id"],
                image=cfg.image,
                disk=cfg.disk_gb,
                vm=mode == "kvm",
                label=create_label,
                onstart=onstart,
                env=cfg.docker_options or "",
            )
        except BaseException:
            # Transport ambiguity (break after accept), malformed create
            # response (2xx without new_contract), OR cancellation/Shutdown
            # mid-PUT (Ctrl-C on the foreground daemon from dev.py): the
            # instance may exist with no captured id. Reconcile by the unique
            # label before re-raising so no billable orphan leaks. BaseException
            # (not Exception) so CancelledError/SystemExit/KeyboardInterrupt
            # still reconcile — the PUT may have been accepted. The first
            # await inside reconcile respects a repeat interrupt, so this
            # never hangs shutdown.
            logger.warning(
                "CREATE_INSTANCE_FAILED — reconciling by label %s",
                create_label,
            )
            await _reconcile_orphan_by_label(client, create_label)
            raise
        logger.debug("INSTANCE_CREATE", extra={"offer_id": offer["id"]})

        # The instance now exists and bills. Any failure below MUST delete it
        # so create_node never leaks a billable orphan (instance id known here,
        # so no label reconcile needed — direct delete).
        try:
            instance = await wait_until_ready(client, instance_id, cfg.connect_grace)
            # wait_until_ready only returns once status == running WITH an ssh
            # endpoint assigned, so the Optional
            # fields are populated here. Assert narrows the type for the DTO
            # without an unreachable raise branch.
            ssh_host = instance["ssh_host"]
            ssh_port = instance["ssh_port"]
            assert ssh_host is not None
            assert ssh_port is not None
        except BaseException:
            # Includes CancelledError/SystemExit/KeyboardInterrupt
            # (BaseException since Py3.8): the poll loop can run for
            # connect_grace (default 300s), so shutdown-driven cancellation
            # here is realistic; `except Exception` would orphan the billing
            # instance. _delete_instance respects a repeat interrupt at its
            # awaits, so this never hangs shutdown.
            logger.exception(
                "Instance %s create_node failed before returning", instance_id
            )
            await _delete_instance(client, instance_id)
            raise
        logger.debug("INSTANCE_READY", extra={"instance_id": instance_id})

    return CloudCreateNodeDTO(
        external_id=str(instance_id),
        hostname=ssh_host,
        port=int(ssh_port),
        username="root",
        jump_host=cfg.jump_host,
        jump_port=cfg.jump_port,
        jump_username=cfg.jump_username or "root",
    )


# endregion FUNC_vastai_create_node


# region FUNC_vastai_delete_node
# PURPOSE: Tear down a VastAI instance so billing stops and the node slot is freed for reallocation.
# INVARIANTS:
# - external_id = instance id
# - delegates to _delete_instance (DELETE with transient retry) so the public delete path inherits the same orphan-prevention guarantees as create_node cleanup.
# - On _delete_instance returning False RAISES VastAIError so the caller's failure handling (DB row stays disabled for cross-cycle retry) kicks in.
# - DELETE errors other than 404 propagate to the orchestrator, which repeats the whole delete next cycle (DELETE is idempotent).
async def vastai_delete_node(cfg: ConfigCloudVastAI, external_id: str) -> None:
    """Delete a VastAI GPU instance."""
    try:
        instance_id = int(external_id)
    except (TypeError, ValueError) as err:
        msg = f"Invalid VastAI instance id {external_id!r}: {err}"
        raise VastAIError(msg) from err

    async with VastAIClient(cfg.api_key) as client:
        logger.debug("INSTANCE_DELETE", extra={"instance_id": instance_id})
        if await _delete_instance(client, instance_id):
            logger.info("DELETED %s", instance_id)
            return
    msg = (
        f"Instance {instance_id} delete failed (permanent error or retries "
        f"exhausted after {_DELETE_ATTEMPTS} attempts); "
        "orchestrator will retry next cycle"
    )
    raise VastAIError(msg)


# endregion FUNC_vastai_delete_node
