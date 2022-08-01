"""Yascheduler client"""

import asyncio
import logging
from pathlib import PurePath
from typing import Any, Mapping, MutableMapping, Optional, Sequence, Union

from attrs import asdict

from .config import Config
from .db import DB, TaskModel, TaskStatus
from .scheduler import Scheduler
from .variables import CONFIG_FILE


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

    def queue_submit_task(
        self,
        label: str,
        metadata: Mapping[str, Any],
        engine_name: str,
        webhook_onsubmit=False,
    ) -> int:
        """Submit new task"""

        async def async_fn() -> TaskModel:
            yac = await Scheduler.create(config=self.config, log=self._logger)
            task = await yac.create_new_task(
                label=label,
                metadata=metadata,
                engine_name=engine_name,
                webhook_onsubmit=webhook_onsubmit,
            )
            await yac.stop()
            return task

        task = asyncio.run(async_fn())
        return task.task_id

    def queue_get_tasks(
        self,
        jobs: Optional[Sequence[int]] = None,
        status: Optional[Sequence[int]] = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Get tasks by ids or statuses"""
        if jobs is not None and status is not None:
            raise ValueError("jobs can be selected only by status or by task ids")
        # raise ValueError if unknown task status
        status = [TaskStatus(x) for x in status] if status else None

        async def fn_get_by_statuses(statuses: Sequence[TaskStatus]):
            db = await DB.create(self.config.db)
            return await db.get_tasks_by_status(statuses)

        async def fn_get_by_ids(ids: Sequence[int]):
            db = await DB.create(self.config.db)
            return await db.get_tasks_by_jobs(ids)

        if status:
            tasks = asyncio.run(fn_get_by_statuses(status))
        elif jobs:
            tasks = asyncio.run(fn_get_by_ids(jobs))
        else:
            return []

        return [asdict(t) for t in tasks]

    def queue_get_task(self, task_id: int) -> Optional[Mapping[str, Any]]:
        """Get task by id"""
        for task_dict in self.queue_get_tasks(jobs=[task_id]):
            return task_dict
