"""Cloud API manager"""

import asyncio
import logging
from asyncio.locks import Lock
from pathlib import Path
from typing import Mapping, Optional, Sequence, Set, Union

from attrs import define, field
from typing_extensions import Self

from ..config import ConfigCloud, ConfigLocal, ConfigRemote, EngineRepository
from ..db import DB
from .adapters import azure_adapter, hetzner_adapter, upcloud_adapter
from .cloud_api import CloudAPI
from .protocols import CloudCapacity, PCloudAdapter, PCloudAPI, PCloudAPIManager


@define(frozen=True)
class CloudAPIManager(PCloudAPIManager):
    """Cloud API manager"""

    apis: Mapping[str, PCloudAPI] = field()
    db: DB = field()
    log: logging.Logger = field()
    on_tasks: Set[int] = field(init=False, factory=set)
    keys_dir: Path = field(factory=Path)
    allocation_lock: Lock = field(factory=Lock, init=False)

    @classmethod
    async def create(
        cls,
        db: DB,
        local_config: ConfigLocal,
        remote_config: ConfigRemote,
        cloud_configs: Sequence[ConfigCloud],
        engines: EngineRepository,
        log: Optional[logging.Logger] = None,
    ) -> Self:
        "Create cloud API manager"
        if log:
            log = log.getChild(cls.__name__)
        else:
            log = logging.getLogger(cls.__name__)

        adapters: Sequence[PCloudAdapter] = [
            azure_adapter,
            hetzner_adapter,
            upcloud_adapter,
        ]
        apis: Mapping[str, PCloudAPI] = {}

        def filter_adapters(prefix: str):
            return filter(lambda x: x.name == prefix, adapters)

        ssh_key_lock = asyncio.Lock()
        for cfg in cloud_configs:
            if cfg.max_nodes <= 0:
                log.debug("Cloud %s is skipped because of <1 max nodes", cfg.prefix)
                continue
            for adapter in filter_adapters(cfg.prefix):
                apis[adapter.name] = await CloudAPI.create(
                    adapter=adapter,
                    config=cfg,
                    local_config=local_config,
                    remote_config=remote_config,
                    engines=engines,
                    ssh_key_lock=ssh_key_lock,
                    log=log,
                )
        log.info("Active cloud APIs: %s", (", ".join(apis.keys()) or "-"))

        return cls(
            apis=apis,
            db=db,
            log=log,
            keys_dir=local_config.keys_dir,
        )

    def __bool__(self) -> bool:
        return bool(len(self.apis))

    async def stop(self) -> None:
        self.log.info("Stopping clouds...")

    def mark_task_done(self, on_task: int) -> None:
        self.on_tasks.discard(on_task)

    async def get_capacity(self) -> Mapping[str, CloudCapacity]:
        data = {}
        for name, count in (await self.db.count_nodes_clouds()).items():
            api = self.apis.get("name")
            data[name] = CloudCapacity(
                name=name,
                current=count,
                max=api.config.max_nodes if api else 0,
            )

        for api in self.apis.values():
            if api.name not in data:
                data[api.name] = CloudCapacity(
                    name=api.name, current=0, max=api.config.max_nodes
                )
        return data

    async def select_best_provider(
        self, want_platforms: Optional[Sequence[str]] = None
    ) -> Optional[PCloudAPI]:
        """Select best cloud API"""
        self.log.debug("Enabled providers: %s", ", ".join(self.apis.keys()))
        used_providers = []
        suitable_providers = list(self.apis.keys())

        cap = await self.get_capacity()

        for name, capacity in cap.items():
            used_providers.append((name, capacity.current))
            api = self.apis.get(name)
            if not api:
                continue
            # remove maxed out providers
            if capacity.current >= api.config.max_nodes:
                suitable_providers.remove(api.name)
                continue
            # remove not supported platforms
            if want_platforms:
                if not any(map(api.is_platform_supported, want_platforms)):
                    suitable_providers.remove(api.name)

        self.log.debug("Used providers: %s", used_providers)
        if not suitable_providers:
            self.log.debug("No suitable cloud provides")
            return
        ok_apis = filter(lambda x: x.name in suitable_providers, self.apis.values())
        ok_apis_sorted = sorted(ok_apis, key=lambda x: x.config.priority, reverse=True)
        api = ok_apis_sorted[0]
        self.log.debug("Chosen: %s", api.name)
        return api

    async def allocate_node(
        self, want_platforms: Optional[Sequence[str]] = None, throttle: bool = False
    ):
        """Allocate new node"""
        async with self.allocation_lock:
            api = await self.select_best_provider(want_platforms)
            if not api:
                return
            if throttle and api.get_op_semaphore().locked():
                self.log.debug(f"Cloud {api.name} is overloaded by requests")
                await asyncio.sleep(1)
                return

            tmp_ip = await self.db.add_tmp_node(api.name, api.config.username)
            await self.db.commit()
        try:
            ip_addr = await api.create_node()
        finally:
            await self.db.remove_node(tmp_ip)
            await self.db.commit()

        await self.db.add_node(ip_addr, api.config.username, None, api.name, True)
        await self.db.commit()
        return ip_addr

    async def allocate(
        self,
        on_task: Optional[int] = None,
        want_platforms: Optional[Sequence[str]] = None,
        throttle: bool = True,
    ) -> Union[str, None]:
        if on_task in self.on_tasks:
            return
        if on_task:
            self.on_tasks.add(on_task)
        try:
            return await self.allocate_node(want_platforms, throttle)
        except Exception as err:
            self.log.error(f"Can't allocate node: {err}")
            if on_task:
                self.mark_task_done(on_task)
        return

    async def deallocate(self, ip_addr: str):
        node = await self.db.get_node(ip_addr)
        if not node or not node.cloud:
            return
        if node.cloud not in self.apis:
            self.log.warning(
                f"Can't deallocate node {node.ip} - unsupported cloud {node.cloud}"
            )
        await self.db.disable_node(ip_addr)
        await self.db.commit()
        try:
            await self.apis[node.cloud].delete_node(node.ip)
        except Exception as err:
            self.log.error(f"Can't deallocate node {node.ip}: {err}")
            return False
        await self.db.remove_node(node.ip)
        await self.db.commit()
