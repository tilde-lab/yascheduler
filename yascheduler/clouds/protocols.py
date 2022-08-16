"""Cloud protocols"""

import asyncio
import logging
from abc import abstractmethod
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TypeVar, Union

from asyncssh.public_key import SSHKey
from attr import define
from typing_extensions import Protocol, Self

from ..config import ConfigCloud, ConfigLocal, ConfigRemote, EngineRepository
from ..db import DB

SupportedPlatformChecker = Callable[[str], bool]

TConfigCloud_contra = TypeVar(
    "TConfigCloud_contra", bound=ConfigCloud, contravariant=True
)


class PCloudConfig(Protocol):
    "Cloud config init protocol"
    bootcmd: Sequence[Union[str, Sequence[str]]]
    package_upgrade: bool
    packages: Sequence[str]

    @abstractmethod
    def render(self) -> str:
        "Render config to string"
        raise NotImplementedError

    @abstractmethod
    def render_base64(self) -> str:
        "Render to user-data format as base64 string"


class CreateNodeCallable(Protocol[TConfigCloud_contra]):
    "Create node in the cloud protocol"

    @abstractmethod
    async def __call__(
        self,
        log: logging.Logger,
        cfg: TConfigCloud_contra,
        key: SSHKey,
        cloud_config: Optional[PCloudConfig] = None,
    ) -> str:
        raise NotImplementedError


class DeleteNodeCallable(Protocol[TConfigCloud_contra]):
    "Delete node in the cloud protocol"

    @abstractmethod
    async def __call__(
        self,
        log: logging.Logger,
        cfg: TConfigCloud_contra,
        host: str,
    ) -> None:
        raise NotImplementedError


class PCloudAdapter(Protocol[TConfigCloud_contra]):
    "Cloud adapter protocol"
    name: str
    supported_platform_checks: Sequence[SupportedPlatformChecker]
    create_node: CreateNodeCallable[TConfigCloud_contra]
    create_node_conn_timeout: int
    create_node_timeout: int
    delete_node: DeleteNodeCallable[TConfigCloud_contra]
    op_limit: int

    @classmethod
    @abstractmethod
    def create(
        cls,
        name: str,
        supported_platform_checks: Sequence[SupportedPlatformChecker],
        create_node: CreateNodeCallable[TConfigCloud_contra],
        delete_node: DeleteNodeCallable[TConfigCloud_contra],
        create_node_conn_timeout: Optional[int],
        create_node_timeout: Optional[int],
        op_limit: int = 1,
    ) -> Self:
        "Create adapter"
        raise NotImplementedError

    @abstractmethod
    def get_op_semaphore(self) -> asyncio.Semaphore:
        """
        Cached semaphore getter.
        It's because you cannot create async semaphore outside the loop.
        "attached to a different loop" error.
        """
        raise NotImplementedError


class PCloudAPI(Protocol[TConfigCloud_contra]):
    "Cloud API protocol"
    name: str
    config: TConfigCloud_contra
    local_config: ConfigLocal
    remote_config: ConfigRemote
    engines: EngineRepository
    log: logging.Logger

    @classmethod
    @abstractmethod
    async def create(
        cls,
        adapter: PCloudAdapter[TConfigCloud_contra],
        config: TConfigCloud_contra,
        local_config: ConfigLocal,
        remote_config: ConfigRemote,
        engines: EngineRepository,
        ssh_key_lock: Optional[asyncio.Lock] = None,
        log: Optional[logging.Logger] = None,
    ) -> Self:
        "Create cloud API"
        raise NotImplementedError

    @abstractmethod
    def get_op_semaphore(self) -> asyncio.Semaphore:
        """
        Cached semaphore getter.
        It's because you cannot create async semaphore outside the loop.
        "attached to a different loop" error.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_cloud_config_data(self) -> PCloudConfig:
        "Return cloud config init"
        raise NotImplementedError

    @abstractmethod
    def is_platform_supported(self, platform: str) -> bool:
        "Is platform is supported by cloud?"
        raise NotImplementedError

    @abstractmethod
    async def create_node(self) -> str:
        "Create new node"
        raise NotImplementedError

    @abstractmethod
    async def delete_node(self, host: str):
        "Delete node"
        raise NotImplementedError


@define(frozen=True)
class CloudCapacity:
    "Cloud capacity object"
    name: str
    max: int
    current: int


class PCloudAPIManager(Protocol):
    "Cloud API manager protocol"
    apis: Mapping[str, PCloudAPI]
    db: DB
    log: logging.Logger
    keys_dir: Path

    @classmethod
    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def __bool__(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        "Stop cloud api manager"
        raise NotImplementedError

    @abstractmethod
    def mark_task_done(self, on_task: int) -> None:
        "Mark the task for which the creation of a node was requested as completed"
        raise NotImplementedError

    @abstractmethod
    async def get_capacity(self) -> Mapping[str, CloudCapacity]:
        "Get clouds capacity"
        raise NotImplementedError

    @abstractmethod
    async def allocate(
        self,
        on_task: Optional[int] = None,
        want_platforms: Optional[Sequence[str]] = None,
        throttle: bool = True,
    ) -> Union[str, None]:
        "Allocate new node"
        raise NotImplementedError

    @abstractmethod
    async def deallocate(self, ip_addr: str):
        "Deallocate the node"
        raise NotImplementedError
