"""Cloud adapters"""

import asyncio
import logging
from functools import cache
from typing import Callable, Generic, Optional

from attrs import define, field

from .protocols import (
    CreateNodeCallable,
    DeleteNodeCallable,
    SupportedPlatformChecker,
    TConfigCloud_co,
)


RecoverNodeCallable = Optional[Callable[..., "object"]]


def can_debian_buster(platform: str) -> bool:
    "Platform is compatible with Debian Buster"
    return platform in ["debian-10", "debian", "debian-like", "linux"]


def can_debian_bullseye(platform: str) -> bool:
    "Platform is compatible with Debian Bullseye"
    return platform in ["debian-11", "debian", "debian-like", "linux"]


def can_debian_bookworm(platform: str) -> bool:
    "Platform is compatible with Debian Bookworm (Debian 12)"
    return platform in ["debian-12", "debian", "debian-like", "linux"]


def can_win10(platform: str) -> bool:
    "Platform is compatible with Windows 10"
    return platform in ["windows-10", "windows"]


def can_win11(platform: str) -> bool:
    "Platform is compatible with Windows 11"
    return platform in ["windows-11", "windows"]


@define(frozen=True)
class CloudAdapter(Generic[TConfigCloud_co]):
    """Cloud adapter"""

    name: str = field()
    supported_platform_checks: tuple[SupportedPlatformChecker, ...] = field()
    create_node: CreateNodeCallable[TConfigCloud_co] = field()
    delete_node: DeleteNodeCallable[TConfigCloud_co] = field()
    op_limit: int = field(default=1)
    create_node_conn_timeout: int = field(default=10)
    create_node_timeout: int = field(default=300)
    recover_node: RecoverNodeCallable = field(default=None)

    @cache
    def get_op_semaphore(self):
        """
        Cached semaphore getter.
        It's because you cannot create async semaphore outside the loop.
        "attached to a different loop" error.
        """
        return asyncio.Semaphore(self.op_limit)


def get_azure_adapter(name: str):
    from .az import az_create_node, az_delete_node

    return CloudAdapter(
        name=name,
        supported_platform_checks=(can_debian_bullseye, can_win11),
        create_node=az_create_node,
        delete_node=az_delete_node,
        op_limit=5,
    )


def get_hetzner_adapter(name: str):
    from .hetzner import hetzner_create_node, hetzner_delete_node

    return CloudAdapter(
        name=name,
        supported_platform_checks=(can_debian_buster,),
        create_node=hetzner_create_node,
        delete_node=hetzner_delete_node,
        op_limit=5,
    )


def get_upcloud_adapter(name: str):
    from .upcloud import upcload_delete_node, upcloud_create_node

    return CloudAdapter(
        name=name,
        supported_platform_checks=(can_debian_buster,),
        create_node=upcloud_create_node,
        delete_node=upcload_delete_node,
        op_limit=1,
    )


def get_vultr_adapter(name: str):
    """Build a CloudAdapter for Vultr bare metal.

    op_limit is 2 (bare metal provisions slowly), create_node_timeout is
    1200 s (~20 min) to accommodate long boot times. ``recover_node`` finds
    orphaned bare-metal instances left behind when ``create_node`` raises
    after provisioning.
    """
    from .vultr import vultr_create_node, vultr_delete_node, vultr_recover_node

    return CloudAdapter(
        name=name,
        supported_platform_checks=(can_debian_bookworm,),
        create_node=vultr_create_node,
        delete_node=vultr_delete_node,
        op_limit=2,
        create_node_timeout=1200,
        recover_node=vultr_recover_node,
    )
