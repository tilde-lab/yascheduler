"""VastAI cloud methods"""

import asyncio
import logging
import socket
import time
from concurrent.futures.thread import ThreadPoolExecutor
from typing import Optional

import requests
from asyncssh.public_key import SSHKey

from ..config import ConfigCloudVastAI
from .protocols import PCloudConfig

BASE_URL = "https://console.vast.ai/api/v0"
executor = ThreadPoolExecutor(max_workers=5)


def get_client(api_key: str) -> str:
    """Validate API key"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.get(f"{BASE_URL}/instances/", headers=headers, timeout=30)
    resp.raise_for_status()
    return api_key


def search_offers(
    api_key: str, min_vram_mb: int, num_gpus: int, max_price: float
) -> list:
    """Search for available GPU offers"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    query = {
        "gpu_ram": {"gte": min_vram_mb},
        "num_gpus": {"eq": num_gpus},
        "gpu_frac": {"eq": 1.0},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "dph_total": {"lte": max_price},
        "type": "on-demand",
        "order": [["dph_total", "asc"]],
        "limit": 20,
    }
    params = {"q": str(query)}
    resp = requests.get(
        f"{BASE_URL}/bundles/", headers=headers, params=params, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("offers", [])


def create_instance(
    api_key: str,
    offer_id: int,
    image: str,
    disk_gb: int,
    onstart_script: str,
    docker_options: str,
    env: dict,
) -> dict:
    """Create VastAI instance"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "client_id": "me",
        "image": image,
        "disk": disk_gb,
        "onstart": onstart_script,
        "runtype": "ssh_direct",
        "docker_options": docker_options,
        "env": env,
        "force": False,
    }
    resp = requests.put(
        f"{BASE_URL}/asks/{offer_id}/", headers=headers, json=payload, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def get_instance_info(api_key: str, instance_id: int) -> dict:
    """Get instance info"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.get(
        f"{BASE_URL}/instances/{instance_id}/", headers=headers, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    inner = data.get("instances")
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, list) and inner:
        return inner[0]
    return data


def find_instance_by_ip(api_key: str, host: str):
    """Find instance by IP"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.get(f"{BASE_URL}/instances/", headers=headers, timeout=30)
    resp.raise_for_status()
    instances = resp.json().get("instances", [])
    if isinstance(instances, dict):
        instances = [instances]
    for inst in instances:
        if inst.get("ssh_host") == host or inst.get("public_ipaddr") == host:
            return inst
    return None


def delete_instance(api_key: str, instance_id: int):
    """Delete instance"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    requests.delete(f"{BASE_URL}/instances/{instance_id}/", headers=headers, timeout=30)


def wait_for_ssh(host: str, port: int = 22, timeout: int = 120) -> bool:
    """Wait for SSH"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            return True
        except Exception:
            time.sleep(3)
    return False


def vastai_create_node_sync(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node - returns IP when ready"""
    get_client(cfg.api_key)
    log.info("Searching VastAI offers...")
    offers = search_offers(
        cfg.api_key, cfg.min_vram_mb, cfg.num_gpus, cfg.max_price_per_hr
    )
    if not offers:
        raise RuntimeError("No VastAI offers found")

    offer = offers[0]
    log.info(f"Creating instance from offer {offer.get('id')}")
    result = create_instance(
        cfg.api_key,
        offer["id"],
        cfg.image,
        cfg.disk_gb,
        cfg.onstart_script,
        cfg.docker_options,
        cfg.env,
    )
    instance_id = result.get("new_contract")
    if not instance_id:
        raise RuntimeError("Failed to create instance")

    deadline = time.time() + 600
    while time.time() < deadline:
        info = get_instance_info(cfg.api_key, instance_id)
        status = info.get("actual_status") or info.get("status")
        log.info(f"Instance status: {status}")
        if status == "running":
            ssh_host = info.get("ssh_host") or info.get("public_ipaddr")
            ssh_port = info.get("ssh_port", 22)
            if ssh_host and wait_for_ssh(ssh_host, ssh_port, 120):
                log.info(f"SSH ready at {ssh_host}:{ssh_port}")
                return ssh_host
        time.sleep(8)
    raise TimeoutError("Instance did not start in time")


async def vastai_create_node(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node"""
    return await asyncio.get_running_loop().run_in_executor(
        executor, vastai_create_node_sync, log, cfg, key, cloud_config
    )


def vastai_delete_node_sync(
    log: logging.Logger, cfg: ConfigCloudVastAI, host: str
) -> None:
    """Delete node by IP"""
    inst = find_instance_by_ip(cfg.api_key, host)
    if inst:
        delete_instance(cfg.api_key, inst["id"])
        log.info(f"Deleted instance {inst['id']}")
    else:
        log.warning(f"No instance found with IP {host}")


async def vastai_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    host: str,
) -> None:
    """Delete node"""
    await asyncio.get_running_loop().run_in_executor(
        executor, vastai_delete_node_sync, log, cfg, host
    )
