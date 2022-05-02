#!/usr/bin/env python3

import inspect
import json
import logging
import random
import string

from configparser import ConfigParser
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import (
    default_backend as crypto_default_backend,
)
from dataclasses import asdict, dataclass, field
from datetime import timedelta, datetime
from importlib import import_module
from pathlib import Path
from time import sleep
from typing import Callable, List, Optional, Tuple, TypeVar, Union

from paramiko.rsakey import RSAKey
from yascheduler.engine import EngineRepository
from yascheduler.ssh import MyParamikoMachine
import yascheduler.scheduler
from yascheduler import DEFAULT_NODES_PER_PROVIDER

T = TypeVar("T")


@dataclass
class CloudConfig:
    bootcmd: List[Union[str, List[str]]] = field(default_factory=lambda: [])
    package_upgrade: bool = False
    packages: List[str] = field(default_factory=lambda: [])

    def render(self) -> str:
        "Render to user-data format"
        return "#cloud-config\n" + json.dumps(asdict(self))


class AbstractCloudAPI(object):

    _log: logging.Logger
    local_keys_dir: Path
    name: str = "abstract"
    ssh_user: str
    _key_name: Optional[str] = None
    _public_key: Optional[str] = None
    max_nodes: Optional[int] = None
    yascheduler: "Optional['yascheduler.scheduler.Yascheduler']" = None

    def __init__(
        self,
        config: ConfigParser,
        max_nodes: Optional[int] = None,
        logger: Optional[logging.Logger] = None,
    ):
        if logger:
            self.log = logger.getChild(self.name)
        else:
            self._log = logging.getLogger(self.name)
        self.max_nodes = int(
            max_nodes if max_nodes is not None else DEFAULT_NODES_PER_PROVIDER
        )
        self.yascheduler = None

        self.ssh_user = config.get(
            "clouds",
            f"{self.name}_user",
            fallback=config.get("remote", "user", fallback="root"),
        )

        local_data_dir = Path(config.get("local", "data_dir", fallback="./data"))
        self.local_keys_dir = Path(
            config.get("local", "keys_dir", fallback=local_data_dir / "keys")
        )

    def _init_key(self) -> Tuple[str, str]:
        # try to load
        for filepath in self.local_keys_dir.iterdir():
            if not filepath.name.startswith("yakey") or not filepath.is_file():
                continue
            key_name = filepath.name
            pmk_key = RSAKey.from_private_key_file(str(filepath))
            self._log.info("LOADED KEY %s" % filepath)
            break

        # generate new
        else:
            key_name = self.get_rnd_name("yakey")
            key = rsa.generate_private_key(
                backend=crypto_default_backend(),
                public_exponent=65537,
                key_size=2048,
            )
            filepath = self.local_keys_dir / self.key_name
            pmk_key = RSAKey(key=key)
            pmk_key.write_private_key_file(str(filepath))
            self._log.info("WRITTEN KEY %s" % filepath)

        return (key_name, "%s %s" % (pmk_key.get_name(), pmk_key.get_base64()))

    @property
    def key_name(self) -> str:
        if not self._key_name:
            self._key_name, self._public_key = self._init_key()
        return self._key_name

    @property
    def public_key(self) -> str:
        if not self._public_key:
            self._key_name, self._public_key = self._init_key()
        return self._public_key

    def get_rnd_name(self, prefix: str) -> str:
        return (
            prefix
            + "-"
            + "".join([random.choice(string.ascii_lowercase) for _ in range(8)])
        )

    @property
    def cloud_config_data(self) -> CloudConfig:
        "Common cloud-config"
        # currently we support only debian-like platforms
        engines = self.yascheduler and self.yascheduler.engines or EngineRepository()
        pkgs = engines.filter_platforms(["debian", "ubuntu"]).get_platform_packages()
        return CloudConfig(
            package_upgrade=True,
            packages=pkgs,
        )

    def _retry_with_backoff(
        self,
        fn: Callable[[], T],
        max_time: float = 60,
        max_interval: float = 10,
    ) -> T:
        "Retry with random backoff"
        end_time = datetime.now() + timedelta(seconds=max_time)
        while True:
            try:
                return fn()
            except Exception as e:
                if datetime.now() >= end_time:
                    raise e
                sleep(
                    min(
                        random.random() * max_interval,
                        max(0, (datetime.now() - end_time).total_seconds()),
                    )
                )

    def _run_ssh_cmd_with_backoff(
        self,
        host: str,
        cmd: str,
        max_time: float = 60,
        max_interval: float = 10,
    ):
        "Run ssh command with retries on errors"

        def run_cmd():
            machine = MyParamikoMachine.create_machine(
                host=host,
                user=self.ssh_user,
                keys_dir=self.local_keys_dir,
                connect_timeout=max_interval,
            )
            return machine.session().run(cmd)[1]

        return self._retry_with_backoff(run_cmd, max_time, max_interval)

    def create_node(self) -> str:
        raise NotImplementedError()

    def setup_node(self, ip):
        """Provision a debian-like node"""
        if self.yascheduler:
            return self.yascheduler.setup_node(ip, self.ssh_user)

    def delete_node(self, ip: str):
        raise NotImplementedError()


def load_cloudapi(name):
    cloudapi_mod = import_module("." + name, package="yascheduler.clouds")
    for _, cls in inspect.getmembers(cloudapi_mod):
        if inspect.isclass(cls) and cls.__base__ == AbstractCloudAPI:
            return cls
    raise ImportError
