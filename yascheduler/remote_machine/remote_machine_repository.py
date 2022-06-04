#!/usr/bin/env python3

import asyncio
import logging
from collections import UserDict
from datetime import timedelta
from operator import itemgetter
from typing import Callable, MutableMapping, Set, Sequence, Optional

from attrs import define, field, evolve
from typing_extensions import Self

from .protocol import PRemoteMachine


@define
class RemoteMachineRepository(UserDict, MutableMapping[str, PRemoteMachine]):
    log: Optional[logging.Logger]
    data: MutableMapping[str, PRemoteMachine] = field(factory=dict)
    connect_in_flight: Set[str] = field(factory=set, init=False)

    def get(
        self, __key: str, default: Optional[PRemoteMachine] = None
    ) -> Optional[PRemoteMachine]:
        return self.data.get(__key, default)

    def keys(self):
        return self.data.keys()

    def values(self):
        return self.data.values()

    def items(self):
        return self.data.items()

    async def disconnect_many(self, ips: Sequence[str]) -> None:
        "Disconnect from many remote machines and remove them from registry"
        if not ips:
            return
        if self.log:
            self.log.info("Disconnecting from machines: {}".format(", ".join(ips)))

        tasks = []
        for ip, machine in list(self.data.items()):
            # guard
            if machine.meta.busy or machine.meta.result:
                continue
            if ip in ips:
                tasks.append(machine.close())
                del self.data[ip]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def disconnect_all(self) -> None:
        "Disconnect from all remotes"
        await self.disconnect_many(list(self.data.keys()))

    def filter(
        self,
        setup: Optional[bool] = None,
        busy: Optional[bool] = None,
        result: Optional[bool] = None,
        platforms: Optional[Sequence[str]] = None,
        free_since_gt: Optional[timedelta] = None,
        reverse_sort: bool = False,
    ) -> Self:
        "Return machines filtered and sorted by `free_since`"

        checks: Sequence[Callable[[PRemoteMachine], bool]] = []
        if setup is True:
            checks.append(lambda x: x.meta.setup)
        if setup is False:
            checks.append(lambda x: not x.meta.setup)
        if busy is True:
            checks.append(lambda x: x.meta.busy)
        if busy is False:
            checks.append(lambda x: not x.meta.busy)
        if result is True:
            checks.append(lambda x: x.meta.result)
        if result is False:
            checks.append(lambda x: not x.meta.result)
        if platforms:
            checks.append(lambda x: bool(set(platforms) & set(x.platforms)))
        if free_since_gt:
            checks.append(lambda x: x.meta.is_free_longer_than(free_since_gt))

        return evolve(
            self,
            data={
                ip: m
                for ip, m in sorted(
                    self.data.items(), key=itemgetter(1), reverse=reverse_sort
                )
                if all([x(m) for x in checks])
            },
        )
