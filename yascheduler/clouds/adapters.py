#!/usr/bin/env python3

from typing import Sequence, Tuple

from attrs import define, field

from .protocols import (
    PCloudAdapter,
    SupportedPlatformChecker,
    TConfigCloud,
    CreateNodeCallable,
    DeleteNodeCallable,
)
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
    create_node_max_time: int = field()
    delete_node: DeleteNodeCallable[TConfigCloud] = field()

    @classmethod
    def create(
        cls,
        name: str,
        supported_platform_checks: Sequence[SupportedPlatformChecker],
        create_node: CreateNodeCallable[TConfigCloud],
        delete_node: DeleteNodeCallable[TConfigCloud],
        create_node_conn_timeout: int = 10,
        create_node_max_time: int = 60,
    ):
        return cls(
            name=name,
            supported_platform_checks=tuple(supported_platform_checks),
            create_node=create_node,
            create_node_conn_timeout=create_node_conn_timeout,
            create_node_max_time=create_node_max_time,
            delete_node=delete_node,
        )


# azure_adapter = CloudAdapter.create(
#     name="az",
#     supported_platform_checks=[can_debian_buster, can_win11],
#     create_node_max_time=600,
#     # test_generics=test_concrete_config,
# )
hetzner_adapter = CloudAdapter.create(
    name="hetzner",
    supported_platform_checks=[can_debian_buster],
    create_node=hetzner_create_node,
    create_node_conn_timeout=5,
    delete_node=hetzner_delete_node,
)
upcloud_adapter = CloudAdapter.create(
    name="upcloud",
    supported_platform_checks=[can_debian_buster],
    create_node=upcloud_create_node,
    create_node_max_time=90,
    delete_node=upcload_delete_node,
)
