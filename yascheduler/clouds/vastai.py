"""VastAI cloud methods"""

import asyncio
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from typing import Optional

import requests
from asyncssh.public_key import SSHKey

from ..config import ConfigCloudVastAI
from .protocols import PCloudConfig

BASE_URL = "https://console.vast.ai/api/v0"
executor = ThreadPoolExecutor(max_workers=5)


@cache
def get_client(api_key: str) -> str:
    """Validate API key by making a test request"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.get(f"{BASE_URL}/instances/", headers=headers, timeout=30)
    resp.raise_for_status()
    return api_key


def search_offers_sync(
    api_key: str,
    min_vram_mb: int = 80 * 1024,
    num_gpus: int = 1,
    max_price: float = 1.50,
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
    params = {"q": requests.utils.quote(str(query))}
    resp = requests.get(
        f"{BASE_URL}/bundles/", headers=headers, params=params, timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("offers", [])


def create_instance_sync(
    api_key: str,
    offer_id: int,
    image: str = "pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel",
    disk_gb: int = 80,
    onstart_script: str = "",
    docker_options: str = "",
    env: Optional[dict] = None,
) -> dict:
    """Create VastAI instance from offer"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "client_id": "me",
        "image": image,
        "disk": disk_gb,
        "onstart": onstart_script,
        "runtype": "ssh_direct",
        "docker_options": docker_options,
        "env": env or {},
        "force": False,
    }
    resp = requests.put(
        f"{BASE_URL}/asks/{offer_id}/",
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_instance_info_sync(api_key: str, instance_id: int) -> dict:
    """Get instance information"""
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


def find_instance_by_ip(api_key: str, host: str) -> Optional[dict]:
    """Find instance by IP address"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.get(f"{BASE_URL}/instances/", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    instances = data.get("instances", [])
    if isinstance(instances, dict):
        instances = [instances]
    for inst in instances:
        inst_host = inst.get("ssh_host") or inst.get("public_ipaddr")
        if inst_host == host:
            return inst
    return None


def delete_instance_sync(api_key: str, instance_id: int) -> None:
    """Delete VastAI instance"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.delete(
        f"{BASE_URL}/instances/{instance_id}/", headers=headers, timeout=30
    )
    resp.raise_for_status()


def wait_for_ssh(host: str, port: int = 22, timeout: int = 120) -> bool:
    """Wait for SSH port to become available"""
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
    """Create node on VastAI - returns IP address when ready"""
    # Validate API key
    get_client(cfg.api_key)

    # Search for offers
    log.info("Searching for VastAI offers...")
    offers = search_offers_sync(
        api_key=cfg.api_key,
        min_vram_mb=cfg.min_vram_mb,
        num_gpus=cfg.num_gpus,
        max_price=cfg.max_price_per_hr,
    )

    if not offers:
        raise RuntimeError(
            "No matching VastAI offers found. "
            f"Try raising max_price_per_hr (current: ${cfg.max_price_per_hr}/hr) "
            "or relaxing filters."
        )

    # Log available offers
    for i, o in enumerate(offers[:5], 1):
        vram_gb = o.get("gpu_ram", 0) / 1024
        gpu_name = o.get("gpu_name", "unknown")
        price = o.get("dph_total", 0)
        log.info(f"  Offer {i}: {gpu_name} ({vram_gb:.0f}GB) - ${price:.3f}/hr")

    # Create instance from first (cheapest) offer
    offer = offers[0]
    offer_id = offer.get("id")
    log.info(f"Creating instance from offer {offer_id}...")

    result = create_instance_sync(
        api_key=cfg.api_key,
        offer_id=offer_id,
        image=cfg.image,
        disk_gb=cfg.disk_gb,
        onstart_script=cfg.onstart_script,
        docker_options=cfg.docker_options,
        env=cfg.env,
    )

    instance_id = result.get("new_contract")
    if not instance_id:
        raise RuntimeError(f"Failed to create instance: {result}")

    log.info(f"Instance created: {instance_id}, waiting for running state...")

    # Wait for instance to be running and get SSH info
    deadline = time.time() + 600  # 10 minutes timeout
    last_status = None
    while time.time() < deadline:
        info = get_instance_info_sync(cfg.api_key, instance_id)
        status = info.get("actual_status") or info.get("status") or "unknown"

        if status != last_status:
            log.info(f"Instance status: {status}")
            last_status = status

        if status == "running":
            ssh_host = info.get("ssh_host") or info.get("public_ipaddr")
            ssh_port = info.get("ssh_port", 22)

            if not ssh_host:
                raise RuntimeError("Instance running but no SSH host found")

            log.info(f"Instance running at {ssh_host}:{ssh_port}")

            # Wait for SSH to be available
            log.info("Waiting for SSH...")
            ssh_ready = wait_for_ssh(ssh_host, ssh_port, timeout=120)
            if not ssh_ready:
                raise RuntimeError("SSH not available after timeout")

            log.info(f"SSH ready at {ssh_host}:{ssh_port}")
            return ssh_host

        time.sleep(8)

    raise TimeoutError(f"Instance {instance_id} did not reach 'running' within timeout")


async def vastai_create_node(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node on VastAI (async wrapper)"""
    return await asyncio.get_running_loop().run_in_executor(
        executor, vastai_create_node_sync, log, cfg, key, cloud_config
    )


def vastai_delete_node_sync(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    host: str,
) -> None:
    """Delete node on VastAI by IP address"""
    log.info(f"Looking for instance with IP {host}...")

    instance = find_instance_by_ip(cfg.api_key, host)
    if instance:
        instance_id = instance.get("id")
        log.info(f"Deleting instance {instance_id}...")
        delete_instance_sync(cfg.api_key, instance_id)
        log.info(f"Instance {instance_id} deleted")
    else:
        log.warning(f"No instance found with IP {host}")


async def vastai_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    host: str,
) -> None:
    """Delete node on VastAI (async wrapper)"""
    return await asyncio.get_running_loop().run_in_executor(
        executor, vastai_delete_node_sync, log, cfg, host
    )
