#!/usr/bin/env python3

import asyncio
import logging
from collections import UserDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from operator import itemgetter
from typing import Optional

from attrs import define, evolve, field

from ..compat import Self
from .remote_machine import RemoteMachine


@define
class RemoteMachineRepository(UserDict[str, RemoteMachine]):
    log: Optional[logging.Logger]
    data: dict[str, RemoteMachine] = field(factory=dict)
    connect_in_flight: set[str] = field(factory=set, init=False)

    async def disconnect_many(self, ips: Sequence[str]) -> None:
        "Disconnect from many remote machines and remove them from registry"
        if not ips:
            return
        if self.log:
            self.log.info("Disconnecting from machines: {}".format(", ".join(ips)))

        tasks: list[Awaitable[None]] = []
        for ip, machine in list(self.data.items()):
            # guard
            if machine.meta.busy:
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
        busy: Optional[bool] = None,
        platforms: Optional[Sequence[str]] = None,
        free_since_gt: Optional[timedelta] = None,
        reverse_sort: bool = False,
    ) -> Self:
        "Return machines filtered and sorted by `free_since`"

        checks: Sequence[Callable[[RemoteMachine], bool]] = []
        if busy is True:
            checks.append(lambda x: x.meta.busy is True)
        if busy is False:
            checks.append(lambda x: not x.meta.busy)
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
