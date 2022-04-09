#!/usr/bin/env python3

import inspect
import logging
import os
import random
import string

from configparser import ConfigParser
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import (
    default_backend as crypto_default_backend,
)
from datetime import timedelta, datetime
from importlib import import_module
from time import sleep
from typing import Any, Callable, Dict, Optional, TypeVar

from fabric import Connection as SSH_Connection
from paramiko.rsakey import RSAKey
import yascheduler.scheduler
from yascheduler import DEFAULT_NODES_PER_PROVIDER

T = TypeVar("T")


class AbstractCloudAPI(object):

    _log: logging.Logger
    name: str = "abstract"
    config: ConfigParser
    yascheduler: "Optional['yascheduler.scheduler.Yascheduler']"

    def __init__(
        self,
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

        self.public_key = None
        self.ssh_custom_key = None

    @property
    def ssh_user(self) -> str:
        "Default SSH user"
        return self.config.get(
            "clouds",
            f"{self.name}_user",
            fallback=self.config.get("remote", "user", fallback="root"),
        )

    def init_key(self):
        if self.ssh_custom_key:
            return

        for filename in os.listdir(self.config.get("local", "data_dir")):
            if not filename.startswith("yakey") or not os.path.isfile(
                os.path.join(self.config.get("local", "data_dir"), filename)
            ):
                continue
            key_path = os.path.join(
                self.config.get("local", "data_dir"), filename
            )
            self.key_name = key_path.split(os.sep)[-1]
            pmk_key = RSAKey.from_private_key_file(key_path)
            self._log.info("LOADED KEY %s" % key_path)
            break

        else:
            self.key_name = self.get_rnd_name("yakey")
            key = rsa.generate_private_key(
                backend=crypto_default_backend(),
                public_exponent=65537,
                key_size=2048,
            )
            pmk_key = RSAKey(key=key)
            key_path = os.path.join(
                self.config.get("local", "data_dir"), self.key_name
            )
            pmk_key.write_private_key_file(key_path)
            self._log.info("WRITTEN KEY %s" % key_path)

        self.public_key = "%s %s" % (pmk_key.get_name(), pmk_key.get_base64())

        self.ssh_custom_key = {"pkey": pmk_key}
        if self.yascheduler:
            self.yascheduler.ssh_custom_key = self.ssh_custom_key

    def get_rnd_name(self, prefix):
        return (
            prefix
            + "-"
            + "".join(
                [random.choice(string.ascii_lowercase) for _ in range(8)]
            )
        )

    @property
    def cloud_config_data(self) -> Dict[str, Any]:
        "Common cloud-config"
        return {
            "package_upgrade": True,
            "packages": ["openmpi-bin"],
        }

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
            ssh_conn = SSH_Connection(
                host=host,
                user=self.ssh_user,
                connect_kwargs=self.ssh_custom_key,
            )
            return ssh_conn.run(cmd, hide=True)

        return self._retry_with_backoff(run_cmd, max_time, max_interval)

    def create_node(self) -> str:
        raise NotImplementedError()

    def setup_node(self, ip):
        ssh_conn = SSH_Connection(
            host=ip, user=self.ssh_user, connect_kwargs=self.ssh_custom_key
        )
        sudo_prefix = "" if self.ssh_user == "root" else "sudo "
        apt_cmd = f"{sudo_prefix}apt-get -o DPkg::Lock::Timeout=600"
        ssh_conn.run(f"{apt_cmd} -y update && {apt_cmd} -y upgrade", hide=True)
        ssh_conn.run(f"{apt_cmd} -y install openmpi-bin", hide=True)

        ssh_conn.run("mkdir -p ~/bin", hide=True)
        if self.config.get("local", "deployable").startswith("http"):
            # downloading binary from a trusted non-public address
            ssh_conn.run(
                "cd ~/bin && wget %s" % self.config.get("local", "deployable"),
                hide=True,
            )
        else:
            # uploading binary from local; requires broadband connection
            # ssh_conn.put(
            #     self.config.get('local', 'deployable'), 'bin/dummyengine'
            # ) # TODO
            ssh_conn.put(
                self.config.get("local", "deployable"), "bin/Pcrystal"
            )  # TODO

        if self.config.get("local", "deployable").endswith(".gz"):
            # binary may be gzipped, without subfolders, with an arbitrary
            # archive name, but the name of the binary must remain Pcrystal
            ssh_conn.run(
                "cd ~/bin && tar xvf %s"
                % self.config.get("local", "deployable").split("/")[-1],
                hide=True,
            )
        # ssh_conn.run('ln -sf ~/bin/Pcrystal /usr/bin/Pcrystal', hide=True)

        # print and ensure versions
        result = ssh_conn.run(
            "/usr/bin/mpirun --allow-run-as-root -V", hide=True
        )
        self._log.info(result.stdout)
        result = ssh_conn.run("cat /etc/issue", hide=True)
        self._log.info(result.stdout)
        result = ssh_conn.run("grep -c ^processor /proc/cpuinfo", hide=True)
        self._log.info(result.stdout)

    def delete_node(self, ip: str):
        raise NotImplementedError()


def load_cloudapi(name):
    cloudapi_mod = import_module("." + name, package="yascheduler.clouds")
    for _, cls in inspect.getmembers(cloudapi_mod):
        if inspect.isclass(cls) and cls.__base__ == AbstractCloudAPI:
            return cls
    raise ImportError
