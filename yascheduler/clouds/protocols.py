"""Cloud protocols"""

import logging
from abc import abstractmethod
from collections.abc import Callable
from typing import Optional, Protocol, TypeVar, Union

from asyncssh.public_key import SSHKey
from attr import define

from ..config import ConfigCloud

SupportedPlatformChecker = Callable[[str], bool]

TConfigCloud_inv = TypeVar("TConfigCloud_inv", bound=ConfigCloud)
TConfigCloud_co = TypeVar("TConfigCloud_co", bound=ConfigCloud, covariant=True)
TConfigCloud_contra = TypeVar(
    "TConfigCloud_contra", bound=ConfigCloud, contravariant=True
)


class PCloudConfig(Protocol):
    "Cloud config init protocol"

    bootcmd: tuple[Union[str, list[str]], ...]
    package_upgrade: bool
    packages: list[str]

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


@define(frozen=True)
class CloudCapacity:
    "Cloud capacity object"

    name: str
    max: int
    current: int
