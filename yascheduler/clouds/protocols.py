#!/usr/bin/env python3

import asyncio
import logging
from abc import abstractmethod
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union, TypeVar

from asyncssh.public_key import SSHKey
from attr import define
from typing_extensions import Protocol, Self

from ..config import ConfigCloud, ConfigLocal, EngineRepository
from ..db import DB

SupportedPlatformChecker = Callable[[str], bool]

TConfigCloud = TypeVar("TConfigCloud", bound=ConfigCloud, contravariant=True)


class PCloudConfig(Protocol):
    bootcmd: Sequence[Union[str, Sequence[str]]]
    package_upgrade: bool
    packages: Sequence[str]

    @abstractmethod
    def render(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def render_base64(self) -> str:
        "Render to user-data format as base64 string"


class CreateNodeCallable(Protocol[TConfigCloud]):
    @abstractmethod
    async def __call__(
        self,
        log: logging.Logger,
        cfg: TConfigCloud,
        key: SSHKey,
        cloud_config: Optional[PCloudConfig] = None,
    ) -> str:
        raise NotImplementedError


class DeleteNodeCallable(Protocol[TConfigCloud]):
    @abstractmethod
    async def __call__(
        self,
        log: logging.Logger,
        cfg: TConfigCloud,
        host: str,
    ) -> None:
        raise NotImplementedError


class PCloudAdapter(Protocol[TConfigCloud]):
    name: str
    supported_platform_checks: Sequence[SupportedPlatformChecker]
    create_node: CreateNodeCallable[TConfigCloud]
    create_node_conn_timeout: int
    delete_node: DeleteNodeCallable[TConfigCloud]
    op_limit: int

    @classmethod
    @abstractmethod
    def create(
        cls,
        name: str,
        supported_platform_checks: Sequence[SupportedPlatformChecker],
        create_node: CreateNodeCallable[TConfigCloud],
        delete_node: DeleteNodeCallable[TConfigCloud],
        create_node_conn_timeout: Optional[int],
        op_limit: int = 1,
    ) -> Self:
        raise NotImplementedError

    @abstractmethod
    def get_op_semaphore(self) -> asyncio.Semaphore:
        """
        Cached semaphore getter.
        It's because you cannot create async semaphore outside the loop.
        "attached to a different loop" error.
        """
        raise NotImplementedError


class PCloudAPI(Protocol[TConfigCloud]):
    name: str
    config: TConfigCloud
    local_config: ConfigLocal
    engines: EngineRepository
    log: logging.Logger

    @classmethod
    @abstractmethod
    async def create(
        cls,
        adapter: PCloudAdapter[TConfigCloud],
        config: TConfigCloud,
        local_config: ConfigLocal,
        engines: EngineRepository,
        log: Optional[logging.Logger] = None,
    ) -> Self:
        raise NotImplementedError

    @abstractmethod
    def get_op_semaphore(self) -> asyncio.Semaphore:
        raise NotImplementedError

    @abstractmethod
    async def get_cloud_config_data(self) -> PCloudConfig:
        raise NotImplementedError

    @abstractmethod
    def is_platform_supported(self, platform: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def create_node(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def delete_node(self, host: str):
        raise NotImplementedError


@define(frozen=True)
class CloudCapacity:
    name: str
    max: int
    current: int


class PCloudAPIManager(Protocol):
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
        cloud_configs: Sequence[ConfigCloud],
        engines: EngineRepository,
        log: Optional[logging.Logger] = None,
    ) -> Self:
        raise NotImplementedError

    @abstractmethod
    def __bool__(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def mark_task_done(self, on_task: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_capacity(self) -> Mapping[str, CloudCapacity]:
        raise NotImplementedError

    @abstractmethod
    async def allocate(
        self,
        on_task: Optional[int] = None,
        want_platforms: Optional[Sequence[str]] = None,
        throttle: bool = True,
    ) -> Union[str, None]:
        raise NotImplementedError

    @abstractmethod
    async def deallocate(self, ip: str):
        raise NotImplementedError
