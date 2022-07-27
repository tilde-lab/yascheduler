#!/usr/bin/env python3

import asyncio
import logging
from concurrent.futures.thread import ThreadPoolExecutor
from functools import lru_cache, partial
from typing import Optional

from asyncssh.public_key import SSHKey as ASSHKey
from hcloud import Client as HClient, APIException
from hcloud.images.domain import Image
from hcloud.server_types.domain import ServerType
from hcloud.servers.client import BoundServer
from hcloud.ssh_keys.domain import SSHKey as HSSHKey

from .protocols import PCloudConfig
from .utils import get_rnd_name, get_key_name
from ..config import ConfigCloudHetzner

executor = ThreadPoolExecutor(max_workers=5)


@lru_cache()
def get_client(cfg: ConfigCloudHetzner) -> HClient:
    return HClient(cfg.token)


@lru_cache()
def get_ssh_key_id(client: HClient, key: ASSHKey) -> int:
    key_name = get_key_name(key)

    try:

        return client.ssh_keys.create(
            name=key_name, public_key=key.export_public_key().decode("utf-8")
        ).id
    except APIException as ex:
        if "already" in str(ex):
            for hkey in client.ssh_keys.get_all(fingerprint=key.get_fingerprint()):
                return hkey.id
            for hkey in client.ssh_keys.get_all(name=key_name):
                return hkey.id
            prefix = "yakey"
            name_len = len(get_rnd_name(prefix))
            for hkey in client.ssh_keys.get_all():
                if hkey.name.startswith(prefix) and len(hkey.name) == name_len:
                    return hkey.id
        raise ex


async def hetzner_create_node(
    log: logging.Logger,
    cfg: ConfigCloudHetzner,
    key: ASSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
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
    ip = server.public_net.ipv4.ip
    log.info("CREATED %s" % ip)
    return ip


def find_srv(client: HClient, host: str) -> Optional[BoundServer]:
    for s in client.servers.get_all():
        if s.public_net.ipv4.ip == host:
            return client.servers.get_by_id(s.id)


async def hetzner_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudHetzner,
    host: str,
):
    loop = asyncio.get_running_loop()
    client = await loop.run_in_executor(executor, get_client, cfg)
    server = await loop.run_in_executor(executor, find_srv, client, host)

    if server:
        await loop.run_in_executor(executor, server.delete)
        log.info("DELETED %s" % host)

    else:
        log.info("NODE %s NOT DELETED AS UNKNOWN" % host)
