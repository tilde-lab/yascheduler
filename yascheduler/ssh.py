#!/usr/bin/env python3

from functools import partial
from pathlib import Path
from typing import Optional

from paramiko.client import AutoAddPolicy
from paramiko.ssh_exception import AuthenticationException
from plumbum.machines.paramiko_machine import ParamikoMachine


class MyParamikoMachine(ParamikoMachine):
    @classmethod
    def create_machine(
        cls,
        host: str,
        user: str,
        keys_dir: Optional[Path] = None,
        missing_host_policy=AutoAddPolicy,
        **kwargs,
    ) -> "MyParamikoMachine":
        keys_paths = []
        if keys_dir:
            keys_paths = list(filter(lambda x: x.is_file(), keys_dir.iterdir()))

        connect = partial(
            cls,
            host=host,
            user=user,
            missing_host_policy=missing_host_policy,
            **kwargs,
        )

        for keyfile in keys_paths:
            try:
                return connect(keyfile=str(keyfile))
            except AuthenticationException:
                pass

        return connect()
