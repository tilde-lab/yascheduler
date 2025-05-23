"""Yascheduler client"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import PurePath
from typing import Any, Optional, TypeVar, Union

from attrs import asdict

from .compat import ParamSpec
from .config import Config
from .db import DB, TaskStatus
from .variables import CONFIG_FILE

ReturnT_co = TypeVar("ReturnT_co", covariant=True)
ParamT = ParamSpec("ParamT")


def to_sync(
    func: Callable[ParamT, Coroutine[Any, Any, ReturnT_co]],
) -> Callable[ParamT, ReturnT_co]:
    """
    Wraps async function and run it sync in thread.
    """

    @wraps(func)
    def outer(*args: ParamT.args, **kwargs: ParamT.kwargs):
        """
        Execute the async method synchronously in sync and async runtime.
        """
        coro = func(*args, **kwargs)
        try:
            asyncio.get_running_loop()  # Triggers RuntimeError if no running event loop

            # Create a separate thread so we can block before returning
            with ThreadPoolExecutor(1) as pool:
                return pool.submit(lambda: asyncio.run(coro)).result()
        except RuntimeError:
            return asyncio.run(coro)

    return outer


class Yascheduler:
    """Yascheduler client"""

    STATUS_TO_DO = TaskStatus.TO_DO.value
    STATUS_RUNNING = TaskStatus.RUNNING.value
    STATUS_DONE = TaskStatus.DONE.value

    config: Config
    _logger: Optional[logging.Logger] = None

    def __init__(
        self,
        config_path: Union[PurePath, str] = CONFIG_FILE,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = Config.from_config_parser(config_path)
        self._logger = logger

    async def queue_submit_task_async(
        self,
        label: str,
        metadata: Mapping[str, Any],
        engine_name: str,
        webhook_onsubmit=False,
    ) -> int:
        """Submit new task"""
        from .scheduler import Scheduler

        yac = await Scheduler.create(config=self.config, log=self._logger)
        task = await yac.create_new_task(
            label=label,
            metadata=metadata,
            engine_name=engine_name,
            webhook_onsubmit=webhook_onsubmit,
        )
        await yac.stop()
        return task.task_id

    def queue_submit_task(
        self,
        label: str,
        metadata: Mapping[str, Any],
        engine_name: str,
        webhook_onsubmit=False,
    ) -> int:
        """Submit new task"""
        fn = to_sync(self.queue_submit_task_async)
        return fn(label, metadata, engine_name, webhook_onsubmit)

    async def queue_get_tasks_async(
        self,
        jobs: Optional[Sequence[int]] = None,
        status: Optional[Sequence[int]] = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Get tasks by ids or statuses"""
        if jobs is not None and status is not None:
            raise ValueError("jobs can be selected only by status or by task ids")
        # raise ValueError if unknown task status
        status = [TaskStatus(x) for x in status] if status else None
        db = await DB.create(self.config.db)
        if status:
            tasks = await db.get_tasks_by_status(status)
        elif jobs:
            tasks = await db.get_tasks_by_jobs(jobs)
        else:
            return []
        return [asdict(t) for t in tasks]

    def queue_get_tasks(
        self,
        jobs: Optional[Sequence[int]] = None,
        status: Optional[Sequence[int]] = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Get tasks by ids or statuses"""
        return to_sync(self.queue_get_tasks_async)(jobs, status)

    async def queue_get_task_async(self, task_id: int) -> Optional[Mapping[str, Any]]:
        """Get task by id"""
        for task_dict in await self.queue_get_tasks_async(jobs=[task_id]):
            return task_dict

    def queue_get_task(self, task_id: int) -> Optional[Mapping[str, Any]]:
        """Get task by id"""
        for task_dict in self.queue_get_tasks(jobs=[task_id]):
            return task_dict
