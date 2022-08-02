"""Cloud API module"""

import asyncio
import base64
import json
import logging
from typing import Optional, Sequence, Union

from asyncssh.public_key import SSHKey, generate_private_key, read_private_key
from attrs import asdict, define, field

from ..config import ConfigLocal, EngineRepository
from ..remote_machine import PRemoteMachine, RemoteMachine
from .protocols import PCloudAdapter, PCloudAPI, PCloudConfig, TConfigCloud_contra
from .utils import get_rnd_name

SSH_KEY_LOCK = asyncio.Lock()


class CloudCreateNodeError(Exception):
    """Cloud node allocation error"""


class CloudSetupNodeError(Exception):
    """Cloud node setup error"""


@define(frozen=True)
class CloudConfig(PCloudConfig):
    "Cloud config init"
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
class CloudAPI(PCloudAPI[TConfigCloud_contra]):
    "Cloud API protocol"
    adapter: PCloudAdapter[TConfigCloud_contra] = field()
    config: TConfigCloud_contra = field()
    local_config: ConfigLocal = field()
    engines: EngineRepository = field()
    log: logging.Logger = field()

    @property
    def name(self) -> str:
        "Cloud name"
        return self.adapter.name

    @classmethod
    async def create(
        cls,
        adapter: PCloudAdapter,
        config: TConfigCloud_contra,
        local_config: ConfigLocal,
        engines: EngineRepository,
        log: Optional[logging.Logger] = None,
    ):
        "Create cloud API"
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
        "Load or generate new SSHKey"
        prefix = "yakey"
        # try to load
        for filepath in self.local_config.keys_dir.iterdir():
            if not filepath.name.startswith(prefix) or not filepath.is_file():
                continue
            ssh_key = read_private_key(filepath)
            ssh_key.set_comment(filepath.name)
            self.log.debug(
                "LOADED KEY %s: %s", filepath.name, ssh_key.get_fingerprint("md5")
            )
            return ssh_key

        key_name = get_rnd_name(prefix)
        filepath = self.local_config.keys_dir / key_name
        ssh_key = generate_private_key(
            alg_name="ssh-rsa", key_size=2048, exponent=65537
        )
        ssh_key.write_private_key(filepath)
        filepath.chmod(0o600)
        ssh_key.set_comment(key_name)
        self.log.info("WRITTEN KEY %s: %s", key_name, ssh_key.get_fingerprint("md5"))
        return ssh_key

    async def get_ssh_key(self) -> SSHKey:
        "Load or generate ssh key (cached)"
        async with SSH_KEY_LOCK:
            return await asyncio.get_running_loop().run_in_executor(
                None, self.get_ssh_key_sync
            )

    async def get_cloud_config_data(self) -> PCloudConfig:
        "Common cloud-config"
        engines = self.engines.filter(
            lambda e: any(map(self.is_platform_supported, e.platforms))
        )
        pkgs = engines.get_platform_packages()
        return CloudConfig(package_upgrade=True, packages=pkgs)

    async def mk_machine(self, ip_addr: str) -> PRemoteMachine:
        "Create RemoteMachine"
        keys = await asyncio.get_running_loop().run_in_executor(
            None, self.local_config.get_private_keys
        )
        return await RemoteMachine.create(
            host=ip_addr,
            username=self.config.username,
            client_keys=keys,
            logger=self.log,
            connect_timeout=self.adapter.create_node_conn_timeout,
            jump_host=self.config.jump_host,
            jump_username=self.config.jump_username,
        )

    async def create_node(self):
        async with self.adapter.get_op_semaphore():
            try:
                ip_addr = await self.adapter.create_node(
                    log=self.log,
                    cfg=self.config,
                    key=await self.get_ssh_key(),
                    cloud_config=await self.get_cloud_config_data(),
                )
            except Exception as err:
                raise CloudCreateNodeError(f"Create node error: {err}") from err

            try:
                machine = await self.mk_machine(ip_addr)
                await machine.run("cloud-init status --wait")
                await machine.setup_node(self.engines)
            except Exception as err:
                self.log.warn("Setup node %s failed - deallocate", ip_addr)
                await self.delete_node(ip_addr)
                raise CloudSetupNodeError(f"Setup node error: {err}") from err
            return ip_addr

    async def delete_node(self, host: str):
        async with self.adapter.get_op_semaphore():
            return await self.adapter.delete_node(
                log=self.log, cfg=self.config, host=host
            )
