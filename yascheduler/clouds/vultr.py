"""Vultr cloud methods (bare metal via REST API v2)"""

import asyncio
import base64
import hashlib
import json
import logging
from functools import cache
from typing import Optional, cast

import aiohttp
import asyncssh
from asyncssh.public_key import SSHKey as ASSHKey

from ..config import ConfigCloudVultr
from .protocols import PCloudConfig
from .utils import get_key_name, get_rnd_name

API_BASE = "https://api.vultr.com/v2"
POLL_INTERVAL = 20
POLL_TIMEOUT = 1200


class APIError(Exception):
    """Vultr API error"""


class VultrClient:
    """Async Vultr REST API client (aiohttp-based)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=60),
            )
        return self._session

    async def request(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> dict:
        """Send an async HTTP request to the Vultr API v2 and return parsed JSON."""
        url = API_BASE + path
        session = await self._get_session()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            async with session.request(method, url, data=data) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    raise APIError(f"HTTP {resp.status}: {raw}")
                if not raw:
                    return {}
                return cast(dict, json.loads(raw))
        except aiohttp.ClientError as err:
            raise APIError(f"HTTP request failed: {err}") from err


@cache
def get_client(cfg: ConfigCloudVultr) -> VultrClient:
    """Get Vultr client (cached per api key).

    The client creates its aiohttp session lazily on the first request,
    so it is safe to construct it outside of an event loop.
    """
    return VultrClient(cfg.api_key)


def ssh_key_fingerprint_md5(pubkey: str) -> str:
    """Compute MD5 fingerprint of an OpenSSH public key string.

    NOTE: MD5 is required here to match the Vultr API fingerprint format,
    not for cryptographic security.
    """
    parts = pubkey.split()
    if len(parts) < 2:
        return ""
    key_bytes = base64.b64decode(parts[1])
    md5_hex = hashlib.md5(key_bytes).hexdigest()
    return ":".join(md5_hex[i : i + 2] for i in range(0, len(md5_hex), 2))


async def get_ssh_key_id(client: VultrClient, key: ASSHKey) -> str:
    """Upload or reuse SSH key on Vultr, return its id"""
    key_name = get_key_name(key)
    pub_key = key.export_public_key("openssh").decode("utf-8")
    fingerprint = ssh_key_fingerprint_md5(pub_key)

    data = await client.request("GET", "/ssh-keys?per_page=500")
    for existing in data.get("ssh_keys", []):
        existing_fp = existing.get("fingerprint", "")
        if existing_fp and existing_fp.lower() == fingerprint.lower():
            return cast(str, existing["id"])

    data = await client.request(
        "POST", "/ssh-keys", {"name": key_name, "ssh_key": pub_key}
    )
    ssh_key = data.get("ssh_key", {})
    if "id" not in ssh_key:
        raise APIError(f"Cannot create SSH key: {data}")
    return cast(str, ssh_key["id"])


def build_baremetal_user_data(
    cloud_config: Optional[PCloudConfig],
    need_raid: bool = True,
) -> str:
    """Build a cloud-init user-data string for bare metal provisioning.

    Always creates /data, sets ulimit, installs apt packages, and adds the
    ScaLAPACK symlink. When need_raid is True, also sets up RAID0 over NVMe
    drives and resizes /dev/shm — needed for vbm-24c-256gb-amd where NVMe
    disks ship unformatted. For plans where NVMe is already the main disk
    (e.g. vbm-8c-132gb), pass need_raid=False to skip RAID and /dev/shm.
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

    engine_packages: list[str] = []
    package_upgrade = False
    if cloud_config:
        engine_packages = list(cloud_config.packages)
        package_upgrade = cloud_config.package_upgrade

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
            "UUID=$(blkid -s UUID -o value /dev/md0) && "
            'echo "UUID=$UUID /data ext4 defaults 0 2" >> /etc/fstab && mount /data',
            "mdadm --detail --scan >> /etc/mdadm/mdadm.conf",
            "update-initramfs -u",
            "echo 'tmpfs /dev/shm tmpfs defaults,size=200G 0 0' >> /etc/fstab",
            "mount -o remount /dev/shm",
        ]

    runcmd += [
        "printf '* soft nofile 65536\\n* hard nofile 65536\\n"
        "root soft nofile 65536\\nroot hard nofile 65536\\n' "
        ">> /etc/security/limits.conf",
        "ln -sf /usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2.1 "
        "/usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.1",
        "ln -sf /usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2.1 "
        "/usr/lib/x86_64-linux-gnu/libscalapack-openmpi.so.2.2",
    ]

    config = {
        "package_upgrade": package_upgrade,
        "packages": packages,
        "runcmd": runcmd,
    }
    return "#cloud-config\n" + json.dumps(config)


SSH_AUTH_ATTEMPTS = 12
SSH_AUTH_INTERVAL = 15


async def _check_ssh_auth(
    log: logging.Logger,
    instance_id: str,
    ip_addr: str,
    key: ASSHKey,
    username: str,
    attempts: int = SSH_AUTH_ATTEMPTS,
    interval: int = SSH_AUTH_INTERVAL,
) -> bool:
    """Poll SSH auth until it succeeds or attempts run out.

    On bare metal the SSH port may open before cloud-init has installed
    authorized_keys, so the first few connects get Permission denied.

    This check is necessary because asyncssh.PermissionDenied is NOT in
    SSHRetryExc (see remote_machine/protocol.py), so mk_machine /
    RemoteMachine.create does not retry it — without this poll the node
    would be deleted on the first Permission denied, triggering a
    redundant provisioning cycle.
    """
    for attempt in range(1, attempts + 1):
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    ip_addr,
                    port=22,
                    username=username,
                    client_keys=[key],
                    known_hosts=None,
                    connect_timeout=10,
                ),
                timeout=15,
            )
            conn.close()
            log.info(
                "Bare-metal %s SSH auth OK on attempt %s/%s",
                instance_id,
                attempt,
                attempts,
            )
            return True
        except Exception as exc:
            log.debug(
                "Bare-metal %s SSH auth attempt %s/%s failed: %s",
                instance_id,
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(interval)
    return False


async def _wait_ssh_port(log: logging.Logger, instance_id: str, ip_addr: str) -> None:
    """Wait until the SSH port (22) accepts a TCP connection."""
    deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip_addr, 22), timeout=10
            )
            writer.close()
            await writer.wait_closed()
            return
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            await asyncio.sleep(10)
    raise APIError(f"Bare-metal {instance_id} SSH not ready on {ip_addr} in time")


async def vultr_create_node(
    log: logging.Logger,
    cfg: ConfigCloudVultr,
    key: ASSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Provision a bare-metal instance and wait until SSH is ready.

    Creates the instance via Vultr API, polls until it becomes active,
    then waits for the SSH port to open and for key-based auth to succeed
    (cloud-init may not have installed authorized_keys yet when the port
    first opens). Returns the instance IP address.
    """
    client = get_client(cfg)
    ssh_key_id = await get_ssh_key_id(client, key)

    label = get_rnd_name("node")
    user_data = build_baremetal_user_data(cloud_config, cfg.need_raid)
    user_data_b64 = base64.b64encode(user_data.encode()).decode()

    body = {
        "region": cfg.location,
        "plan": cfg.server_type,
        "os_id": cfg.image_name,
        "label": label,
        "hostname": label,
        "sshkey_id": [ssh_key_id],
        "user_data": user_data_b64,
        "enable_ipv6": True,
    }
    data = await client.request("POST", "/bare-metals", body)
    bm = data.get("bare_metal", data)
    instance_id = bm.get("id")
    if not instance_id:
        raise APIError(f"No instance id in response: {data}")

    log.info("CREATING bare-metal %s (id=%s)", label, instance_id)

    deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT
    last_status: Optional[str] = None
    ip_addr: Optional[str] = None
    while asyncio.get_running_loop().time() < deadline:
        data = await client.request("GET", f"/bare-metals/{instance_id}")
        bm = data.get("bare_metal", data)
        status = bm.get("status", "")
        ip_addr = bm.get("main_ip", "")
        if status != last_status:
            log.info("bare-metal %s status=%s ip=%s", instance_id, status, ip_addr)
            last_status = status
        if status == "active" and ip_addr and ip_addr != "0.0.0.0":
            break
        await asyncio.sleep(POLL_INTERVAL)
    else:
        raise APIError(
            f"Bare-metal {instance_id} did not become active in {POLL_TIMEOUT}s"
        )

    assert ip_addr is not None
    log.info("Bare-metal %s active, waiting for SSH on %s", instance_id, ip_addr)
    await _wait_ssh_port(log, instance_id, ip_addr)

    # SSH port may open before cloud-init finishes installing authorized_keys,
    # causing Permission denied on first connect. Poll auth with the configured
    # key so create_node doesn't fail and trigger redundant instance creation.
    log.info(
        "Bare-metal %s SSH port open, waiting for cloud-init to install keys",
        instance_id,
    )
    ssh_ok = await _check_ssh_auth(log, instance_id, ip_addr, key, cfg.username)
    if not ssh_ok:
        raise APIError(
            f"Bare-metal {instance_id} SSH auth failed on {ip_addr} "
            f"after {SSH_AUTH_ATTEMPTS} attempts"
        )

    log.info("CREATED %s", ip_addr)
    return cast(str, ip_addr)


async def find_baremetal(client: VultrClient, host: str) -> Optional[str]:
    """Find a bare-metal instance id by its IP address."""
    data = await client.request("GET", "/bare-metals?per_page=500")
    for bm in data.get("bare_metals", []):
        if bm.get("main_ip") == host and bm.get("id"):
            return cast(str, bm["id"])
    return None


async def vultr_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudVultr,
    host: str,
) -> None:
    """Delete a bare-metal instance by its IP address."""
    client = get_client(cfg)
    instance_id = await find_baremetal(client, host)
    if instance_id:
        await client.request("DELETE", f"/bare-metals/{instance_id}")
        log.info("DELETED %s", host)
    else:
        log.info("NODE %s NOT DELETED AS UNKNOWN", host)


async def vultr_recover_node(
    log: logging.Logger,
    cfg: ConfigCloudVultr,
) -> Optional[str]:
    """Find an orphaned bare-metal instance and return its IP address.

    Lists all bare-metal instances on Vultr and returns the IP of the first
    one that is ``active`` (or ``pending``) with a valid IP. This is used
    when ``create_node`` raised after the instance was already provisioned
    on the cloud side, so the node is alive but missing from the scheduler
    DB. Returns ``None`` when no orphan is found.
    """
    client = get_client(cfg)
    data = await client.request("GET", "/bare-metals?per_page=500")
    for bm in data.get("bare_metals", []):
        status = bm.get("status", "")
        ip = bm.get("main_ip", "")
        if status in ("active", "pending") and ip and ip != "0.0.0.0":
            log.info("RECOVERED orphan bare-metal %s (id=%s) ip=%s",
                     bm.get("label"), bm.get("id"), ip)
            return cast(str, ip)
    return None
