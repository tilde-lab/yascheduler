"""VastAI cloud methods"""

import asyncio
import logging
from typing import Any, Optional, cast

import aiohttp
from asyncssh.public_key import SSHKey

from ..config import ConfigCloudVastAI
from .protocols import PCloudConfig

BASE_URL = "https://console.vast.ai/api/v0"


def _get_headers(api_key: str) -> dict[str, str]:
    """Get headers for VastAI API requests"""
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


async def _api_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    api_key: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Make an API request and handle errors"""
    headers = _get_headers(api_key)
    timeout = aiohttp.ClientTimeout(total=30)

    async with session.request(
        method, url, headers=headers, timeout=timeout, **kwargs
    ) as resp:
        if not resp.ok:
            text = await resp.text()
            raise RuntimeError(f"VastAI API error: {resp.status} {resp.reason}: {text}")
        return await resp.json()


async def _search_offers(
    session: aiohttp.ClientSession,
    api_key: str,
    min_vram_mb: int,
    num_gpus: int,
    max_price: float,
) -> list[dict[str, Any]]:
    """Search for available GPU offers"""
    query = {
        "gpu_ram": {"gte": min_vram_mb},
        "num_gpus": {"eq": num_gpus},
        "gpu_frac": {"gte": 1.0},
        "rentable": {"eq": True},
        "rented": {"eq": False},
        "dph_total": {"lte": max_price},
        "type": "on-demand",
        "order": [["dph_total", "asc"]],
        "limit": 20,
    }
    params = {"q": str(query)}
    data = await _api_request(
        session, "GET", f"{BASE_URL}/bundles/", api_key, params=params
    )
    return data.get("offers", [])


async def _create_instance(
    session: aiohttp.ClientSession,
    api_key: str,
    offer_id: int,
    image: str,
    disk_gb: int,
    onstart_script: str,
    docker_options: str,
    env: dict[str, str],
) -> dict[str, Any]:
    """Create VastAI instance"""
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
    return await _api_request(
        session, "PUT", f"{BASE_URL}/asks/{offer_id}/", api_key, json=payload
    )


async def _get_instance_info(
    session: aiohttp.ClientSession, api_key: str, instance_id: int
) -> dict[str, Any]:
    """Get instance info"""
    data = await _api_request(
        session, "GET", f"{BASE_URL}/instances/{instance_id}/", api_key
    )
    inner = data.get("instances")
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, list) and inner:
        return inner[0]
    return data


async def _find_instance_by_ip(
    session: aiohttp.ClientSession, api_key: str, host: str
) -> Optional[dict[str, Any]]:
    """Find instance by IP address"""
    data = await _api_request(session, "GET", f"{BASE_URL}/instances/", api_key)
    instances = data.get("instances", [])
    if isinstance(instances, dict):
        instances = [instances]
    for inst in instances:
        if inst.get("public_ipaddr") == host:
            return inst
    return None


async def _delete_instance(
    session: aiohttp.ClientSession, api_key: str, instance_id: int
) -> None:
    """Delete instance"""
    headers = _get_headers(api_key)
    timeout = aiohttp.ClientTimeout(total=30)
    url = f"{BASE_URL}/instances/{instance_id}/"

    async with session.delete(url, headers=headers, timeout=timeout) as resp:
        if not resp.ok:
            text = await resp.text()
            raise RuntimeError(
                f"VastAI API error deleting instance {instance_id}: "
                f"{resp.status} {resp.reason}: {text}"
            )


async def vastai_create_node(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create node - returns IP address when ready"""
    async with aiohttp.ClientSession() as session:
        log.info("Searching VastAI offers...")
        offers = await _search_offers(
            session, cfg.api_key, cfg.min_vram_mb, cfg.num_gpus, cfg.max_price_per_hr
        )
        if not offers:
            raise RuntimeError("No VastAI offers found matching criteria")

        offer = offers[0]
        offer_id = offer.get("id")
        if not isinstance(offer_id, int):
            raise RuntimeError("Offer missing required 'id' field")
        log.info(f"Creating instance from offer {offer_id}")

        result = await _create_instance(
            session,
            cfg.api_key,
            cast(int, offer_id),
            cfg.image,
            cfg.disk_gb,
            cfg.onstart_script,
            cfg.docker_options,
            cfg.env,
        )
        instance_id = result.get("new_contract")
        if not isinstance(instance_id, int):
            raise RuntimeError("Failed to create instance - no contract ID returned")
        instance_id = cast(int, instance_id)

        max_wait = 600  # 10 minutes
        poll_interval = 8
        for _ in range(max_wait // poll_interval):
            await asyncio.sleep(poll_interval)

            info = await _get_instance_info(session, cfg.api_key, instance_id)
            status = info.get("actual_status") or info.get("status")
            log.info(f"Instance {instance_id} status: {status}")

            if status == "running":
                ip_addr = info.get("public_ipaddr")
                if ip_addr:
                    log.info(f"Instance running at {ip_addr}")
                    return ip_addr
                log.warning("Instance running but no public IP address yet")

        raise TimeoutError(
            f"Instance {instance_id} did not become ready within {max_wait} seconds"
        )


async def vastai_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudVastAI,
    host: str,
) -> None:
    """Delete node by IP address"""
    async with aiohttp.ClientSession() as session:
        inst = await _find_instance_by_ip(session, cfg.api_key, host)
        if inst:
            instance_id = inst.get("id")
            if not isinstance(instance_id, int):
                raise RuntimeError(f"Instance for {host} has invalid ID")
            await _delete_instance(session, cfg.api_key, cast(int, instance_id))
            log.info(f"Deleted VastAI instance {instance_id}")
        else:
            log.warning(f"No VastAI instance found with IP {host}")
