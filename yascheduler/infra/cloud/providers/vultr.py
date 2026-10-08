"""Vultr cloud provider (bare metal via REST API v2)."""
# region MODULE_CONTRACT
# PURPOSE: Provision and decommission Vultr servers so the scheduler can run compute workloads on Vultr through the generic CloudAdapter contract.
# SCOPE: cloud-side lifecycle only, NOT DB/UoW/SSH-setup/allocator.
# DEPENDENCIES: USES API: api.vultr.com/v2 (aiohttp).
# KEYWORDS: vultr, provider, bare metal, create, delete, ssh key, raid, cloud-init
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import base64
import json
import logging
import weakref
from json import JSONDecodeError
from typing import TYPE_CHECKING, TypedDict

import aiohttp

from yascheduler.infra.cloud import (
    CloudCreateNodeDTO,
    CloudInitConfig,
    build_cloud_init_users,
    get_key_name,
    get_rnd_name,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from asyncssh.public_key import SSHKey as ASSHKey

    from yascheduler.infra.cloud.cloud_configs import ConfigCloudVultr
    from yascheduler.shared import Required, Self, TypeGuard, Unpack

__all__ = ["vultr_create_node", "vultr_delete_node"]
logger = logging.getLogger(__name__)


API_BASE = "https://api.vultr.com/v2/"
POLL_INTERVAL = 20
POLL_TIMEOUT = 1200
# DELETE retries on transient (5xx) failures; the DELETE call itself.
CLEANUP_DELETE_ATTEMPTS = 3
CLEANUP_DELETE_INTERVAL = 5
# After a 2xx DELETE, poll GET until the instance is confirmed gone (404).
# Vultr bare-metal deletion is asynchronous: a 2xx means "accepted", not "gone".
CLEANUP_VERIFY_TIMEOUT = 180
CLEANUP_VERIFY_INTERVAL = 10
# Listing pagination + orphan-reconcile retry. Vultr caps per_page at 500;
# accounts with more bare-metals need cursor pagination via meta.links.next.
# Reconcile retries the listing a few times with a delay to cover both
# listing-lag (instance not yet visible after a POST) and transient listing
# failures (5xx/transport) — without this, a freshly created orphan could be
# missed and left billing.
LIST_PAGE_SIZE = 500
RECONCILE_ATTEMPTS = 10
RECONCILE_INTERVAL = 15
_HTTP_BAD_REQUEST_CODE = 400
_HTTP_INTERNAL_ERROR_CODE = 500
_HTTP_NOT_FOUND_CODE = 404
_HTTP_TOO_MANY_REQUESTS = 429


# region BLOCK_API_response_shapes
# PURPOSE: Abstract Vultr REST API responces
class VultrSSHKey(TypedDict):
    id: str
    ssh_key: str


class VultrSSHKeysResponse(TypedDict):
    ssh_keys: list[VultrSSHKey]


class VultrSSHKeyCreateResponse(TypedDict):
    ssh_key: VultrSSHKey


class VultrBareMetal(TypedDict, total=False):
    id: Required[str]
    label: str
    status: str
    main_ip: str


class VultrBareMetalResponse(TypedDict):
    bare_metal: VultrBareMetal


class VultrBareMetalsCreate(TypedDict):
    region: str
    plan: str
    os_id: int
    label: str
    hostname: str
    sshkey_id: list[str]
    user_data: str
    enable_ipv6: bool


class VultrBareMetalsResponse(TypedDict, total=False):
    bare_metals: Required[list[VultrBareMetal]]


def _is_ssh_key(x: object) -> TypeGuard[VultrSSHKey]:
    return (
        isinstance(x, dict)
        and isinstance(x.get("id"), str)
        and isinstance(x.get("ssh_key"), str)
    )


def _is_ssh_keys_resp(resp: object) -> TypeGuard[VultrSSHKeysResponse]:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("ssh_keys"), list)
        and all(_is_ssh_key(k) for k in resp["ssh_keys"])
    )


def _is_ssh_key_create_resp(
    resp: object,
) -> TypeGuard[VultrSSHKeyCreateResponse]:
    return isinstance(resp, dict) and _is_ssh_key(resp.get("ssh_key"))


def _is_bare_metal(x: object) -> TypeGuard[VultrBareMetal]:
    if not isinstance(x, dict):
        return False
    if not isinstance(x.get("id"), str):
        return False
    if "label" in x and not isinstance(x["label"], str):
        return False
    if "status" in x and not isinstance(x["status"], str):
        return False
    return "main_ip" not in x or isinstance(x["main_ip"], str)


def _is_bare_metal_resp(resp: object) -> TypeGuard[VultrBareMetalResponse]:
    return isinstance(resp, dict) and _is_bare_metal(resp.get("bare_metal"))


def _is_bare_metals_resp(resp: object) -> TypeGuard[VultrBareMetalsResponse]:
    return (
        isinstance(resp, dict)
        and isinstance(resp.get("bare_metals"), list)
        and all(_is_bare_metal(b) for b in resp["bare_metals"])
        and (  # meta optional but if present must be a dict
            "meta" not in resp or isinstance(resp["meta"], dict)
        )
    )


# endregion BLOCK_API_response_shapes


class APIError(Exception):
    """Vultr API error.

    `status` is the HTTP status when the error originated from an HTTP
    response, or None for transport-level failures.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """True for 429/5xx responses and transport failures — worth retrying."""
        return (
            self.status is None
            or self.status == _HTTP_TOO_MANY_REQUESTS
            or self.status >= _HTTP_INTERNAL_ERROR_CODE
        )


# region CLASS_VultrClient
# PURPOSE: Wrap a single aiohttp session authenticated to the Vultr API so repeated create/poll/delete calls reuse one connection pool instead of re-handshaking per request.
class VultrClient:
    """Async Vultr REST API client (aiohttp-based)."""

    def __init__(self, api_key: str) -> None:
        self._session = aiohttp.ClientSession(
            base_url=API_BASE,
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
        """Send an async HTTP request to the Vultr API v2 and return parsed JSON.

        DELETE returns 204 No Content (empty body); such responses yield None
        instead of raising on ``resp.json()``.
        """
        try:
            async with self._session.request(
                method, path, params=params, json=data
            ) as resp:
                if resp.status >= _HTTP_BAD_REQUEST_CODE:
                    msg = f"HTTP {resp.status}: {await resp.text()}"
                    raise APIError(msg, status=resp.status)
                # Vultr DELETE returns 204 with an empty body; json() would
                # raise ContentTypeError on it, turning every successful delete
                # into a bogus APIError. Empty body -> None.
                raw = await resp.text()
                return json.loads(raw) if raw else None
        except JSONDecodeError as exc:
            msg = f"Invalid JSON response: {exc}"
            raise APIError(msg) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            msg = f"Transport error: {exc}"
            raise APIError(msg) from exc

    def _get_next_cursor(self, resp: dict) -> str | None:
        "Return meta.links.next cursor path or None."
        return resp.get("meta", {}).get("links", {}).get("next")

    async def get_ssh_keys(
        self, per_page: int = LIST_PAGE_SIZE
    ) -> AsyncIterator[VultrSSHKey]:
        """Yield SSH keys one at a time, paginating through all pages."""
        params: dict[str, str | int] = {"per_page": per_page}
        while True:
            resp = await self._request("GET", "ssh-keys", params=params)
            if not _is_ssh_keys_resp(resp):
                msg = f"Invalid SSH keys list response: {resp}"
                raise APIError(msg)
            for key in resp["ssh_keys"]:
                yield key
            if not (cursor := self._get_next_cursor(dict(resp))):
                break
            params["cursor"] = cursor

    async def create_ssh_key(self, name: str, pub_key: str) -> str:
        data = {"name": name, "ssh_key": pub_key}
        resp = await self._request("POST", "ssh-keys", data=data)
        if not _is_ssh_key_create_resp(resp):
            msg = f"Cannot create SSH key: {resp}"
            raise APIError(msg)
        return resp["ssh_key"]["id"]

    async def delete_ssh_key(self, key_id: str) -> None:
        """Delete an account SSH key by id (cleanup/testing path)."""
        await self._request("DELETE", f"ssh-keys/{key_id}")

    async def create_bare_metal(
        self, **params: Unpack[VultrBareMetalsCreate]
    ) -> VultrBareMetal:
        resp = await self._request("POST", "bare-metals", data=dict(params))
        if not _is_bare_metal_resp(resp):
            msg = f"Invalid create Bare Metal response: {resp}"
            raise APIError(msg)
        return resp["bare_metal"]

    async def get_bare_metal(self, instance_id: str) -> VultrBareMetal:
        resp = await self._request("GET", f"bare-metals/{instance_id}")
        if not _is_bare_metal_resp(resp):
            msg = f"Invalid get Bare Metal response: {resp}"
            raise APIError(msg)
        return resp["bare_metal"]

    async def get_bare_metals(
        self, per_page: int = LIST_PAGE_SIZE
    ) -> AsyncIterator[VultrBareMetal]:
        """Yield bare metals one at a time, paginating through all pages."""
        params: dict[str, str | int] = {"per_page": per_page}
        while True:
            resp = await self._request("GET", "bare-metals", params=params)
            if not _is_bare_metals_resp(resp):
                msg = f"Invalid bare-metals list response: {resp}"
                raise APIError(msg)
            for bm in resp["bare_metals"]:
                yield bm
            if not (cursor := self._get_next_cursor(dict(resp))):
                break
            params["cursor"] = cursor

    async def delete_bare_metal(self, instance_id: str) -> None:
        await self._request("DELETE", f"bare-metals/{instance_id}")


# endregion CLASS_VultrClient


# region FUNC_get_ssh_key_id
# PURPOSE: Upload (or reuse) an SSH key on Vultr and return its id so bare-metal instances can be launched with authorized_keys preinstalled.
# ENSURES: Returns the Vultr ssh-key id; whitespace-normalized public-key match against existing keys avoids duplicate uploads (asyncssh export appends a trailing newline Vultr may strip on storage). The list+create section is serialized per event loop, so concurrent allocations (Vultr adapter op_limit=2) cannot both miss and both upload a duplicate.
# RATIONALE:
# - Q: Why a per-loop lock map and not one module-level asyncio.Lock?
#   A: A Lock binds to the loop that first acquires it; a process that runs
#      several event loops over its lifetime (daemon restarts in-process,
#      per-test loops in pytest) would break on the second loop. The map
#      creates the lock lazily inside the running loop, like the adapter's
#      @cache'd op semaphore.
_ssh_key_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def _ssh_key_lock() -> asyncio.Lock:
    """Return this loop's lock, creating it on first use inside the loop."""
    loop = asyncio.get_running_loop()
    lock = _ssh_key_locks.get(loop)
    if lock is None:
        lock = _ssh_key_locks.setdefault(loop, asyncio.Lock())
    return lock


async def get_ssh_key_id(client: VultrClient, key: ASSHKey) -> str:
    """Upload or reuse SSH key on Vultr, return its id."""
    key_name = get_key_name(key)
    pub_key = key.export_public_key("openssh").decode("utf-8").strip()

    async with _ssh_key_lock():
        async for existing in client.get_ssh_keys():
            if existing["ssh_key"].strip() == pub_key:
                return existing["id"]

        return await client.create_ssh_key(key_name, pub_key)


# endregion FUNC_get_ssh_key_id


# region FUNC_build_baremetal_user_data
# PURPOSE: Build a cloud-init user-data string for bare-metal provisioning so a freshly launched Vultr instance has /data, ulimit, apt packages, RAID0 NVMe (when need_raid), the ScaLAPACK symlinks, and authorized_keys for root (and a non-root SSH user when configured) ready before the scheduler connects.
# RATIONALE:
# - Q: Why is /data a fixed absolute path and not ~/data?
#   A: On bare metal /data is either a RAID0 NVMe mount (need_raid=True) or the root disk (need_raid=False); engines and tasks require a dedicated mount point (/data/engines, /data/tasks), and cloud-init must guarantee /data exists before the scheduler connects.
# - Q: Why emit a `users` section when sshkey_id already injects the key for root?
#   A: Vultr's sshkey_id only injects into /root/.ssh/authorized_keys. For non-root username, cloud-init must create the user, install the key, and grant passwordless sudo (setup_node runs `sudo apt-get ...`); root is also listed for determinism.
def build_baremetal_user_data(
    username: str,
    pub_key: str,
    cloud_config: CloudInitConfig | None,
    need_raid: bool = True,
) -> str:
    """Build a cloud-init user-data string for bare metal provisioning.

    Always creates /data, sets ulimit, installs apt packages, and adds the
    ScaLAPACK symlinks. When need_raid is True, also sets up RAID0 over NVMe
    drives and resizes /dev/shm — needed for vbm-24c-256gb-amd where NVMe
    disks ship unformatted. For plans where NVMe is already the main disk
    (e.g. vbm-8c-132gb), pass need_raid=False to skip RAID and /dev/shm.

    Engine packages and package_upgrade are sourced from cloud_config when
    provided; bare-metal base packages are always merged in (deduped).
    Always emits a `users` section (root, and a no-sudo non-root user when
    username != root) so cloud-init installs authorized_keys before the
    scheduler polls SSH auth as cfg.username.
    """
    base_packages = [
        "openmpi-bin",
        "openmpi-common",
        "libopenmpi-dev",
        "libscalapack-openmpi-dev",
        "libxml2-dev",
        "libblas-dev",
        "liblapack-dev",
        "build-essential",
        "gfortran",
        "cmake",
        "git",
    ]
    if need_raid:
        base_packages.append("mdadm")

    engine_packages = []
    package_upgrade = False
    engine_bootcmd: tuple[str | list[str], ...] = ()
    if cloud_config:
        engine_packages = list(cloud_config.packages)
        package_upgrade = cloud_config.package_upgrade
        engine_bootcmd = cloud_config.bootcmd

    packages = list(dict.fromkeys(base_packages + engine_packages))

    # /data is an absolute path: on bare metal it is either a RAID0 NVMe
    # mount (need_raid=True) or the root disk (need_raid=False). This is not
    # ~/data (the default remote.data_dir), because bare-metal instances
    # require a dedicated mount point for engines (/data/engines) and tasks
    # (/data/tasks). cloud-init guarantees /data exists before the scheduler
    # connects.
    runcmd = ["mkdir -p /data"]

    if need_raid:
        runcmd += [
            "mdadm --create /dev/md0 --level=0 --raid-devices=2 /dev/nvme0n1 /dev/nvme1n1 --force",
            "mkfs.ext4 -b 4096 -E stride=128,stripe-width=256 /dev/md0",
            (
                "UUID=$(blkid -s UUID -o value /dev/md0) && "
                'echo "UUID=$UUID /data ext4 defaults 0 2" >> /etc/fstab && mount /data'
            ),
            "mdadm --detail --scan >> /etc/mdadm/mdadm.conf",
            "update-initramfs -u",
            "echo 'tmpfs /dev/shm tmpfs defaults,size=200G 0 0' >> /etc/fstab",
            "mount -o remount /dev/shm",
        ]

    # Non-root ssh user can't write to root-owned /data.
    # chown it after the mount so the scheduler user owns /data/engines and /data/tasks.
    if username != "root":
        runcmd.append(f"chown -R {username}:{username} /data")

    runcmd += [
        (
            "printf '* soft nofile 65536\\n* hard nofile 65536\\n"
            "root soft nofile 65536\\nroot hard nofile 65536\\n' "
            ">> /etc/security/limits.conf"
        ),
        (
            "ln -sf /usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2.1 "
            "/usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.1"
        ),
        (
            "ln -sf /usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2.1 "
            "/usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2"
        ),
    ]

    return CloudInitConfig(
        bootcmd=engine_bootcmd,
        runcmd=tuple(runcmd),
        package_upgrade=package_upgrade,
        packages=packages,
        users=build_cloud_init_users(username, pub_key),
    ).render()


# endregion FUNC_build_baremetal_user_data


# region FUNC_vultr_create_node
# PURPOSE: Provision a Vultr bare-metal instance via the CloudAdapter interface so the generic provisioner can launch Vultr compute nodes.
# ENSURES: Returns CloudCreateNodeDTO with external_id = bare-metal instance id, hostname = instance public IP, once the instance is active and has a public IP. On any failure AFTER the instance is created (POST ambiguity included), the instance is best-effort reconciled/deleted before re-raising (no billable orphan).
# INVARIANTS:
# - external_id = bare-metal instance id
# - hostname = instance public IP
# - create_node returns as soon as the instance status is "active" with a non-zero public IP
# - Post-instance-create failures (poll timeout) trigger best-effort DELETE of the instance id before the exception propagates.
# - If the cleanup itself is interrupted (e.g. a second CancelledError during shutdown), the ORIGINAL create error still propagates; the instance id / label is logged (ERROR) for manual recovery since no DB row exists to retry against yet.
# - POST /bare-metals is NOT idempotent: a transport timeout/break AFTER the server accepted the create leaves an instance we have no id for. The label (generated pre-POST) is used to reconcile such orphans (see _reconcile_orphan_by_label) so no billable resource leaks when the POST call itself fails.
async def vultr_create_node(
    cfg: ConfigCloudVultr, key: ASSHKey, cloud_config: CloudInitConfig | None = None
) -> CloudCreateNodeDTO:
    """Provision a bare-metal instance and return its id + IP once active.

    Creates the instance via Vultr API and polls until it becomes active
    with a public IP.

    If any step after instance creation fails, the instance is best-effort
    deleted before re-raising so no billable orphan leaks.
    """
    label = get_rnd_name(cfg.label)
    pub_key = key.export_public_key("openssh").decode("utf-8")
    user_data = build_baremetal_user_data(
        cfg.username, pub_key, cloud_config, cfg.need_raid
    )
    user_data_b64 = base64.b64encode(user_data.encode()).decode()

    async with VultrClient(cfg.api_key) as client:
        ssh_key_id = await get_ssh_key_id(client, key)

        # POST /bare-metals is not idempotent: if the transport breaks after the
        # server accepted the create, an instance exists that we have no id for.
        # Reconcile by the label generated above before re-raising the POST error
        # so a created instance is deleted, not orphaned.
        # ponytail: catch BaseException, not Exception — CancelledError (task
        # cancel during shutdown) is a BaseException since Py3.8; a POST that
        # the server accepted but the client never finished reading would
        # otherwise leak a billable orphan on every daemon stop mid-provision.
        try:
            bm = await client.create_bare_metal(
                region=cfg.location,
                plan=cfg.server_type,
                os_id=cfg.image_name,
                label=label,
                hostname=label,
                sshkey_id=[ssh_key_id],
                user_data=user_data_b64,
                enable_ipv6=True,
            )
        except BaseException as err:
            logger.warning(
                "POST bare-metal failed (%s) — reconciling by label %s",
                err,
                label,
            )
            try:
                await _reconcile_orphan_by_label(client, label)
            except BaseException:
                logger.exception(
                    "RECONCILE_INTERRUPTED — bare-metal may still bill; "
                    "manual check needed for label %s",
                    label,
                )
            raise
        instance_id = bm["id"]

        logger.info("CREATING bare-metal %s (id=%s)", label, instance_id)

        # The instance now exists and bills. Any failure below MUST best-effort
        # delete it so create_node never leaks a billable orphan.
        try:
            deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT
            last_status: str | None = None
            ip_addr: str | None = None
            while asyncio.get_running_loop().time() < deadline:
                # Vultr API occasionally flaps with 5xx on an instance that actually
                # exists and is progressing. Treat transient errors as "no data this
                # tick" and keep polling; a permanent error (4xx) or a transport
                # failure that persists until deadline will surface as APIError below.
                try:
                    bm = await client.get_bare_metal(instance_id)
                except APIError as err:
                    if err.transient:
                        logger.debug(
                            "POLL_TRANSIENT_RETRY",
                            extra={"instance_id": instance_id, "error": str(err)},
                        )
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    raise
                status = bm.get("status", "")
                ip_addr = bm.get("main_ip", "")
                if status != last_status:
                    logger.debug(
                        "POLL_STATUS",
                        extra={
                            "instance_id": instance_id,
                            "status": status,
                            "ip": ip_addr,
                        },
                    )
                    last_status = status
                if status == "active" and ip_addr and ip_addr != "0.0.0.0":  # noqa: S104
                    break
                await asyncio.sleep(POLL_INTERVAL)
            else:
                msg = (
                    f"Bare-metal {instance_id} did not become active in {POLL_TIMEOUT}s"
                )
                raise APIError(msg)  # noqa: TRY301

            logger.info("CREATED %s (id=%s)", ip_addr, instance_id)
            return CloudCreateNodeDTO(
                external_id=instance_id,
                hostname=ip_addr,
                username=cfg.username,
                jump_host=cfg.jump_host,
                jump_port=cfg.jump_port,
                jump_username=cfg.jump_username or "root",
            )
        except BaseException:
            logger.exception(
                "Bare-metal %s create_node failed before returning", instance_id
            )
            try:
                await _delete_and_verify(client, instance_id)
            except BaseException:
                logger.exception(
                    "CLEANUP_INTERRUPTED — bare-metal %s may still bill; "
                    "manual deletion required via Vultr console",
                    instance_id,
                )
            raise


# endregion FUNC_vultr_create_node


# region FUNC__reconcile_orphan_by_label
# PURPOSE: Close the POST-ambiguity orphan window by matching and best-effort deleting an instance created during a failed/ambiguous POST, using the label generated pre-POST.
# ENSURES: Never raises — the original POST error propagates regardless. Retries the listing RECONCILE_ATTEMPTS times with a delay to cover listing-lag (instance not yet visible after POST) and transient listing failures; on each tick a label match triggers delete+verify, exhausting all attempts without a match logs a warning for manual reconciliation.
async def _reconcile_orphan_by_label(client: VultrClient, label: str) -> None:
    """Best-effort delete of an instance created during an ambiguous POST.

    Vultr POST /bare-metals is not idempotent: if we time out or the transport
    breaks after the server accepted the create, an instance exists that we
    never got an id for. Match it by the label generated pre-POST and delete
    it. Never raises.
    """
    for attempt in range(1, RECONCILE_ATTEMPTS + 1):
        orphan_id: str | None = None
        try:
            async for bm in client.get_bare_metals():
                if bm.get("label") == label and bm.get("id"):
                    orphan_id = bm["id"]
                    break
        # BaseException: CancelledError is a BaseException since Py3.8.
        except BaseException:
            logger.debug(
                "RECONCILE_LIST_TRANSIENT",
                extra={
                    "label": label,
                    "attempt": attempt,
                    "attempts": RECONCILE_ATTEMPTS,
                },
            )
            if attempt < RECONCILE_ATTEMPTS:
                await asyncio.sleep(RECONCILE_INTERVAL)
                continue
            logger.exception(
                "RECONCILE_LOOKUP_FAILED — potential orphan billing; manual check needed",
                extra={"label": label},
            )
            return
        if orphan_id is not None:
            logger.warning(
                "RECONCILE_DELETE_ORPHAN",
                extra={"label": label, "instance_id": orphan_id},
            )
            await _delete_and_verify(client, orphan_id)
            return
        logger.debug(
            "RECONCILE_NO_ORPHAN",
            extra={"label": label, "attempt": attempt, "attempts": RECONCILE_ATTEMPTS},
        )
        if attempt < RECONCILE_ATTEMPTS:
            await asyncio.sleep(RECONCILE_INTERVAL)
    logger.warning(
        "RECONCILE_NO_ORPHAN — %s listing attempts found no match for label %s",
        RECONCILE_ATTEMPTS,
        label,
    )


# endregion FUNC__reconcile_orphan_by_label


# region FUNC__delete_and_verify
# PURPOSE: Delete a Vultr bare-metal instance with transient retry and async-deletion verification so neither create_node cleanup nor deallocate leaks a billable orphan when Vultr flaps or returns 2xx without immediately removing the instance.
# ENSURES: Returns True iff the instance is confirmed gone (DELETE 404 or verify GET 404). Returns False if DELETE permanently fails, retries exhaust, or verify times out. Never raises. Timeout-verified still-present instances are escalated to ERROR for manual intervention. The log NEVER claims success without a 404 confirmation.
# MODEL: Vultr bare-metal DELETE is asynchronous — 2xx means "accepted", not "gone". A 404 on DELETE means already gone. A 5xx is transient and retried. A 4xx (non-404) is permanent. After an accepted DELETE, poll GET until 404 or CLEANUP_VERIFY_TIMEOUT.
async def _delete_and_verify(
    client: VultrClient,
    instance_id: str,
) -> bool:
    """Delete a bare-metal instance with retry + async-deletion verification.

    Returns True iff confirmed gone (404). Never raises.
    """
    # region BLOCK_delete
    for attempt in range(1, CLEANUP_DELETE_ATTEMPTS + 1):
        try:
            await client.delete_bare_metal(instance_id)
        except APIError as err:
            if err.status == _HTTP_NOT_FOUND_CODE:
                # Already gone — nothing to verify.
                logger.warning("Bare-metal %s already gone (DELETE 404)", instance_id)
                return True
            if err.transient and attempt < CLEANUP_DELETE_ATTEMPTS:
                logger.debug(
                    "DELETE_TRANSIENT_RETRY",
                    extra={
                        "instance_id": instance_id,
                        "attempt": attempt,
                        "attempts": CLEANUP_DELETE_ATTEMPTS,
                        "error": str(err),
                    },
                )
                await asyncio.sleep(CLEANUP_DELETE_INTERVAL)
                continue
            logger.exception(
                "Bare-metal %s delete failed after %s attempts", instance_id, attempt
            )
            return False
        except Exception:
            logger.exception("Bare-metal %s delete failed", instance_id)
            return False
        break
    # endregion BLOCK_delete

    # region BLOCK_verify
    return await _verify_instance_gone(client, instance_id)
    # endregion BLOCK_verify


# endregion FUNC__delete_and_verify


# region FUNC__verify_instance_gone
# PURPOSE: Poll GET /bare-metals/{id} until 404 (confirmed gone) so delete never claims success on a still-billing orphan after an async Vultr deletion.
# ENSURES: Returns True on 404 (logs success), False on CLEANUP_VERIFY_TIMEOUT expiry (logs ERROR for manual intervention). Never raises. Transient GET errors during polling are treated as "uncertain, keep polling".
async def _verify_instance_gone(client: VultrClient, instance_id: str) -> bool:
    """Poll GET until the instance returns 404 or CLEANUP_VERIFY_TIMEOUT expires.

    Returns True iff confirmed gone. Never raises.
    """
    deadline = asyncio.get_running_loop().time() + CLEANUP_VERIFY_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        try:
            await client.get_bare_metal(instance_id)
        except APIError as err:
            if err.status == _HTTP_NOT_FOUND_CODE:
                logger.warning("Bare-metal %s delete confirmed gone", instance_id)
                return True
            # Transient GET error or unexpected status — uncertain, keep polling.
            logger.debug(
                "VERIFY_GET_RETRY",
                extra={"instance_id": instance_id, "error": str(err)},
            )
        except Exception as err:
            logger.debug(
                "VERIFY_GET_RETRY",
                extra={"instance_id": instance_id, "error": str(err)},
            )
        await asyncio.sleep(CLEANUP_VERIFY_INTERVAL)
    # Timeout: instance still present after accepted DELETE + full verify window.
    logger.error(
        "Bare-metal %s STILL PRESENT %ss after accepted DELETE — "
        "manual deletion required via Vultr console",
        instance_id,
        CLEANUP_VERIFY_TIMEOUT,
    )
    return False


# endregion FUNC__verify_instance_gone


# region FUNC_vultr_delete_node
# PURPOSE: Tear down a Vultr bare-metal instance by its instance id (external_id) so billing stops and the node slot is freed for reallocation.
# INVARIANTS:
# - external_id = bare-metal instance id
# - Idempotent: a 404 on DELETE means already gone — logged and returned without raising.
# - delegates to _delete_and_verify (retry + async-deletion verify) so the public delete path inherits the same orphan-prevention guarantees as create_node cleanup.
# - On _delete_and_verify returning False RAISES APIError so the caller's failure handling kicks in.
async def vultr_delete_node(
    cfg: ConfigCloudVultr,
    external_id: str,
) -> None:
    """Delete a bare-metal instance by its instance id (stored as external_id).

    Raises APIError when deletion cannot be confirmed gone, so the caller can
    leave the DB row intact for cross-cycle retry. A 404 (already gone) is a
    no-op.
    """
    async with VultrClient(cfg.api_key) as client:
        if await _delete_and_verify(client, external_id):
            logger.info("DELETED %s", external_id)
            return
    msg = (
        f"Bare-metal {external_id} delete not confirmed gone "
        "— cloud VM may still bill; orchestrator will retry next cycle"
    )
    raise APIError(msg)


# endregion FUNC_vultr_delete_node
