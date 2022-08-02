"""Hetzner cloud methods"""

import asyncio
import logging
from concurrent.futures.thread import ThreadPoolExecutor
from functools import lru_cache, partial
from typing import Optional

from asyncssh.public_key import SSHKey as ASSHKey
from hcloud import APIException
from hcloud import Client as HClient
from hcloud.images.domain import Image
from hcloud.server_types.domain import ServerType
from hcloud.servers.client import BoundServer
from hcloud.ssh_keys.domain import SSHKey as HSSHKey

from ..config import ConfigCloudHetzner
from .protocols import PCloudConfig
from .utils import get_key_name, get_rnd_name

executor = ThreadPoolExecutor(max_workers=5)


@lru_cache(maxsize=None)
def get_client(cfg: ConfigCloudHetzner) -> HClient:
    "Get Hetzner client"
    return HClient(cfg.token)


@lru_cache()
def get_ssh_key_id(client: HClient, key: ASSHKey) -> int:
    "Get Hetzner ssh id"
    key_name = get_key_name(key)
    pub_key = key.export_public_key("openssh").decode("utf-8")

    try:
        return client.ssh_keys.create(name=key_name, public_key=pub_key).id
    except APIException as err:
        if "already" in str(err):
            hkey = client.ssh_keys.get_by_fingerprint(
                key.get_fingerprint("md5").split(":", maxsplit=1)[1]
            ) or client.ssh_keys.get_by_name(key_name)
            if hkey:
                return hkey.id
            prefix = "yakey"
            name_len = len(get_rnd_name(prefix))
            for hkey in client.ssh_keys.get_all():
                if hkey.name.startswith(prefix) and len(hkey.name) == name_len:
                    return hkey.id
        raise err


async def hetzner_create_node(
    log: logging.Logger,
    cfg: ConfigCloudHetzner,
    key: ASSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node"""
    loop = asyncio.get_running_loop()
    client = await loop.run_in_executor(executor, get_client, cfg)
    ssh_key_id = await loop.run_in_executor(executor, get_ssh_key_id, client, key)

    create_server = partial(
        client.servers.create,
        name=get_rnd_name("node"),
        server_type=ServerType(cfg.server_type),
        image=Image(name=cfg.image_name),
        ssh_keys=[HSSHKey(id=ssh_key_id, name=get_key_name(key))],
        user_data=cloud_config.render() if cloud_config else None,
    )
    response = await loop.run_in_executor(executor, create_server)
    server = response.server
    ip_addr = server.public_net.ipv4.ip
    log.info("CREATED %s", ip_addr)
    return ip_addr


def find_srv(client: HClient, host: str) -> Optional[BoundServer]:
    """Find BoundServer by IP addr"""
    for server in client.servers.get_all():
        if server.public_net.ipv4.ip == host:
            return client.servers.get_by_id(server.id)
    return None


async def hetzner_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudHetzner,
    host: str,
):
    """Delete node"""
    loop = asyncio.get_running_loop()
    client = await loop.run_in_executor(executor, get_client, cfg)
    server = await loop.run_in_executor(executor, find_srv, client, host)

    if server:
        await loop.run_in_executor(executor, server.delete)
        log.info("DELETED %s", host)

    else:
        log.info("NODE %s NOT DELETED AS UNKNOWN", host)
