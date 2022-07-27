#!/usr/bin/env python3

import asyncio
from collections import deque
from typing import Hashable, Set, Type, TypeVar, Deque, Generic

from attrs import define, field

TUMsgId = TypeVar("TUMsgId", bound=Hashable)
TUMsgPayload = TypeVar("TUMsgPayload")


@define(frozen=True)
class UMessage(Generic[TUMsgId, TUMsgPayload]):
    id: TUMsgId = field()
    payload: TUMsgPayload = field(hash=False)


class UniqueQueue(asyncio.Queue, Generic[TUMsgId, TUMsgPayload]):
    name: str
    _queue: Deque[UMessage[TUMsgId, TUMsgPayload]]
    _done_pending: Set[UMessage[TUMsgId, TUMsgPayload]]

    def __init__(self, name: str, maxsize: int = 0, *argv, loop=None):
        self.name = name
        self._done_pending = set()
        super().__init__(maxsize, *argv, loop=loop)

    def _get(self):
        item = self._queue.popleft()
        self._done_pending.add(item)
        return item

    async def get(self) -> UMessage[TUMsgId, TUMsgPayload]:
        return await super().get()

    async def put(self, item: UMessage[TUMsgId, TUMsgPayload]) -> None:
        # skip already added
        if item in self._queue or item in self._done_pending:
            return
        await super().put(item)

    def task_done(self, item: UMessage):
        self._done_pending.remove(item)
        super().task_done()

    def psize(self):
        """Number of items not done but not in queue."""
        return len(self._done_pending)
