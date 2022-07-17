#!/usr/bin/env python3

import asyncio
from functools import lru_cache
from typing import Sequence, Tuple

from attrs import define, field

from .protocols import (
    PCloudAdapter,
    SupportedPlatformChecker,
    TConfigCloud,
    CreateNodeCallable,
    DeleteNodeCallable,
)
from .az import az_create_node, az_delete_node
from .hetzner import hetzner_create_node, hetzner_delete_node
from .upcloud import upcload_delete_node, upcloud_create_node


def can_debian_buster(platform: str) -> bool:
    return platform in ["debian-10", "debian", "debian-like", "linux"]


def can_debian_bullseye(platform: str) -> bool:
    return platform in ["debian-11", "debian", "debian-like", "linux"]


def can_win10(platform: str) -> bool:
    return platform in ["windows-10", "windows"]


def can_win11(platform: str) -> bool:
    return platform in ["windows-11", "windows"]


@define(frozen=True)
class CloudAdapter(PCloudAdapter[TConfigCloud]):
    name: str = field()
    supported_platform_checks: Tuple[SupportedPlatformChecker] = field()
    create_node: CreateNodeCallable[TConfigCloud] = field()
    create_node_conn_timeout: int = field()
    delete_node: DeleteNodeCallable[TConfigCloud] = field()
    op_limit: int = field(default=1)

    @classmethod
    def create(
        cls,
        name: str,
        supported_platform_checks: Sequence[SupportedPlatformChecker],
        create_node: CreateNodeCallable[TConfigCloud],
        delete_node: DeleteNodeCallable[TConfigCloud],
        create_node_conn_timeout: int = 10,
        op_limit: int = 1,
    ):
        return cls(
            name=name,
            supported_platform_checks=tuple(supported_platform_checks),
            create_node=create_node,
            create_node_conn_timeout=create_node_conn_timeout,
            delete_node=delete_node,
            op_limit=op_limit,
        )

    @lru_cache()
    def get_op_semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self.op_limit)


azure_adapter = CloudAdapter.create(
    name="az",
    supported_platform_checks=[can_debian_bullseye, can_win11],
    create_node=az_create_node,
    delete_node=az_delete_node,
    op_limit=5,
)
hetzner_adapter = CloudAdapter.create(
    name="hetzner",
    supported_platform_checks=[can_debian_buster],
    create_node=hetzner_create_node,
    create_node_conn_timeout=10,
    delete_node=hetzner_delete_node,
    op_limit=5,
)
upcloud_adapter = CloudAdapter.create(
    name="upcloud",
    supported_platform_checks=[can_debian_buster],
    create_node=upcloud_create_node,
    delete_node=upcload_delete_node,
    op_limit=1,
)
