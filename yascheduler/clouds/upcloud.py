"""Upcloud cloud methods"""

import asyncio
import logging
import time
from concurrent.futures.thread import ThreadPoolExecutor
from functools import lru_cache
from typing import Optional

from asyncssh.public_key import SSHKey
from upcloud_api import CloudManager, Server, Storage, login_user_block

from ..config import ConfigCloudUpcloud
from .protocols import PCloudConfig
from .utils import get_rnd_name

executor = ThreadPoolExecutor(max_workers=5)


@lru_cache(maxsize=None)
def get_client(cfg: ConfigCloudUpcloud) -> CloudManager:
    """Get Upcloud client"""
    client = CloudManager(cfg.login, cfg.password)
    client.authenticate()
    return client


def upcloud_create_node_sync(
    log: logging.Logger,
    cfg: ConfigCloudUpcloud,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node"""
    client = get_client(cfg)

    login_user = login_user_block(
        username=cfg.username,
        ssh_keys=[key.export_public_key("openssh").decode("utf-8")],
        create_password=False,
    )
    server = client.create_server(
        Server(
            core_number=8,
            memory_amount=4096,
            hostname=get_rnd_name("node"),
            zone="uk-lon1",
            storage_devices=[Storage(os="Debian 10.0", size=40)],
            login_user=login_user,
            user_data=cloud_config.render() if cloud_config else None,
        )
    )
    ip_addr = server.get_public_ip()
    log.info("CREATED %s", ip_addr)
    return ip_addr


async def upcloud_create_node(
    log: logging.Logger,
    cfg: ConfigCloudUpcloud,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node"""
    return await asyncio.get_running_loop().run_in_executor(
        executor, upcloud_create_node_sync, log, cfg, key, cloud_config
    )


def upcload_delete_node_sync(
    log: logging.Logger,
    cfg: ConfigCloudUpcloud,
    host: str,
):
    """Delete node"""
    client = get_client(cfg)
    for server in client.get_servers():
        if server.get_public_ip() == host:
            server.stop()
            log.info("WAITING FOR STOP...")
            time.sleep(20)
            while True:
                try:
                    server.destroy()
                except Exception:
                    time.sleep(5)
                else:
                    break
            for storage in server.storage_devices:
                storage.destroy()
            log.info("DELETED %s", host)
            break
    else:
        log.info("NODE %s NOT DELETED AS UNKNOWN", host)


async def upcload_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudUpcloud,
    host: str,
):
    """Delete node"""
    return await asyncio.get_running_loop().run_in_executor(
        executor, upcload_delete_node_sync, log, cfg, host
    )
