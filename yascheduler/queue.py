"""Async queue with message deduplication"""

import asyncio
from typing import Deque, Generic, Hashable, Set, TypeVar

from attrs import define, field

TUMsgId = TypeVar("TUMsgId", bound=Hashable)
TUMsgPayload = TypeVar("TUMsgPayload")


@define(frozen=True)
class UMessage(Generic[TUMsgId, TUMsgPayload]):
    """Async queue message"""

    id: TUMsgId = field()
    payload: TUMsgPayload = field(hash=False)


class UniqueQueue(asyncio.Queue, Generic[TUMsgId, TUMsgPayload]):
    """Async queue with message deduplication"""

    name: str
    _queue: Deque[UMessage[TUMsgId, TUMsgPayload]]
    _done_pending: Set[UMessage[TUMsgId, TUMsgPayload]]

    def __init__(self, name: str, *argv, maxsize: int = 0, **kwargs):
        self.name = name
        self._done_pending = set()
        super().__init__(maxsize, *argv, **kwargs)

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

    def task_done(self):
        raise NotImplementedError("task_done() not implemented, use item_done()")

    def item_done(self, item: UMessage):
        """Indicate that a enqueued task is complete."""
        self._done_pending.remove(item)
        super().task_done()

    def psize(self):
        """Number of items not done but not in queue."""
        return len(self._done_pending)
