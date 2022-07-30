#!/usr/bin/env python3

import asyncio
import base64
import json
import logging
from typing import Optional, Sequence, Union

from asyncssh.public_key import SSHKey, generate_private_key, read_private_key
from asyncstdlib import lru_cache
from attrs import asdict, define, field

from ..config import ConfigLocal, EngineRepository
from ..remote_machine import PRemoteMachine, RemoteMachine
from .protocols import PCloudAdapter, PCloudAPI, PCloudConfig, TConfigCloud
from .utils import get_rnd_name


@define(frozen=True)
class CloudConfig(PCloudConfig):
    bootcmd: Sequence[Union[str, Sequence[str]]] = field(factory=tuple)
    package_upgrade: bool = field(default=False)
    packages: Sequence[str] = field(factory=list)

    def render(self) -> str:
        "Render to user-data format"
        return "#cloud-config\n" + json.dumps(asdict(self))

    def render_base64(self) -> str:
        "Render to user-data format as base64 string"
        return base64.b64encode(self.render().encode()).decode()


@define(frozen=True)
class CloudAPI(PCloudAPI[TConfigCloud]):
    adapter: PCloudAdapter[TConfigCloud] = field()
    config: TConfigCloud = field()
    local_config: ConfigLocal = field()
    engines: EngineRepository = field()
    log: logging.Logger = field()

    @property
    def name(self) -> str:
        return self.adapter.name

    @classmethod
    async def create(
        cls,
        adapter: PCloudAdapter,
        config: TConfigCloud,
        local_config: ConfigLocal,
        engines: EngineRepository,
        log: Optional[logging.Logger] = None,
    ):
        if log:
            log = log.getChild(adapter.name)
        else:
            log = logging.getLogger(adapter.name)

        return cls(
            adapter=adapter,
            config=config,
            local_config=local_config,
            engines=engines,
            log=log,
        )

    def get_op_semaphore(self) -> asyncio.Semaphore:
        return self.adapter.get_op_semaphore()

    def is_platform_supported(self, platform: str) -> bool:
        return any(map(lambda x: x(platform), self.adapter.supported_platform_checks))

    def get_ssh_key_sync(self) -> SSHKey:
        prefix = "yakey"
        # try to load
        for filepath in self.local_config.keys_dir.iterdir():
            if not filepath.name.startswith(prefix) or not filepath.is_file():
                continue
            ssh_key = read_private_key(filepath)
            ssh_key.set_comment(filepath.name)
            self.log.info("LOADED KEY %s" % filepath)
            return ssh_key

        key_name = get_rnd_name(prefix)
        filepath = self.local_config.keys_dir / key_name
        ssh_key = generate_private_key(
            alg_name="ssh-rsa", key_size=2048, exponents=65537
        )
        ssh_key.write_private_key(filepath)
        ssh_key.set_comment(key_name)
        self.log.info("WRITTEN KEY %s" % filepath)
        return ssh_key

    @lru_cache()
    async def get_ssh_key(self) -> SSHKey:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.get_ssh_key_sync
        )

    async def get_cloud_config_data(self) -> PCloudConfig:
        "Common cloud-config"
        engines = self.engines.filter(
            lambda e: any(map(lambda p: self.is_platform_supported(p), e.platforms))
        )
        pkgs = engines.get_platform_packages()
        return CloudConfig(package_upgrade=True, packages=pkgs)

    async def mk_machine(self, ip: str) -> PRemoteMachine:
        keys = await asyncio.get_running_loop().run_in_executor(
            None, self.local_config.get_private_keys
        )
        return await RemoteMachine.create(
            host=ip,
            username=self.config.username,
            client_keys=keys,
            logger=self.log,
            connect_timeout=self.adapter.create_node_conn_timeout,
            jump_host=self.config.jump_host,
            jump_username=self.config.jump_username,
        )

    async def create_node(self):
        async with self.adapter.get_op_semaphore():
            ip = await self.adapter.create_node(
                log=self.log,
                cfg=self.config,
                key=await self.get_ssh_key(),
                cloud_config=await self.get_cloud_config_data(),
            )
            machine = await self.mk_machine(ip)
            await machine.run("cloud-init status --wait")
            await machine.setup_node(self.engines)
            return ip

    async def delete_node(self, host: str):
        async with self.adapter.get_op_semaphore():
            return await self.adapter.delete_node(
                log=self.log, cfg=self.config, host=host
            )
