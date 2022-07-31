"""Database utils"""

import asyncio
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from enum import Enum, unique
from typing import Any, List, Mapping, Optional, Sequence, cast

import backoff
from attrs import asdict, define, field
from pg8000.native import Connection, InterfaceError
from typing_extensions import Self

from .config import ConfigDb


@unique
class TaskStatus(int, Enum):
    """Tast status enum"""

    TO_DO = 0
    RUNNING = 1
    DONE = 2


@define(frozen=True, hash=True)
class NodeModel:
    """Node model"""

    ip: str = field()
    ncpus: Optional[int] = field()
    enabled: bool = field(default=True)
    cloud: Optional[str] = field(default=None)
    username: str = field(default="root")


@define(frozen=True)
class TaskModel:
    """Task model"""

    task_id: int = field()
    label: str = field()
    ip: str = field()
    status: TaskStatus = field(converter=TaskStatus)
    metadata: Mapping[str, Any] = field(factory=dict)
    cloud: Optional[str] = field(default=None)

    def __hash__(self) -> int:
        return hash(json.dumps(asdict(self), sort_keys=True))


@define(frozen=True)
class DB:
    """Database abstraction"""

    loop: asyncio.AbstractEventLoop = field()
    executor: ThreadPoolExecutor = field()
    conn: Connection = field()

    @staticmethod
    def create_connection(config: ConfigDb) -> Connection:
        """Create database connection"""
        return Connection(
            user=config.user,
            host=config.host,
            database=config.database,
            port=config.port,
            password=config.password,
        )

    @classmethod
    async def create(cls, config: ConfigDb, automigrate=True) -> Self:
        """Async init"""
        loop = asyncio.get_running_loop()
        exe = ThreadPoolExecutor(max_workers=1)  # pg8000 is not thread safe
        conn = await loop.run_in_executor(exe, cls.create_connection, config)
        ins = cls(loop=loop, executor=exe, conn=conn)
        if automigrate:
            await ins.migrate()
        return ins

    async def run(self, sql: str, **params):
        """Run query async with backoff"""

        @backoff.on_exception(backoff.fibo, InterfaceError, max_time=60)
        def run_fn():
            return self.conn.run(sql, **params)

        return await self.loop.run_in_executor(self.executor, run_fn)

    async def migrate(self) -> None:
        """Migrate database scheme"""
        await self.run(
            """ALTER TABLE yascheduler_nodes
            ADD COLUMN IF NOT EXISTS username VARCHAR(255) DEFAULT 'root';"""
        )

    async def commit(self):
        """Commit"""
        await self.run("COMMIT;")

    async def close(self):
        """Close connection"""
        await self.loop.run_in_executor(self.executor, self.conn.close)
        self.executor.shutdown()

    async def has_node(self, ip_addr: str) -> bool:
        """Check if node exist"""
        await self.run("SELECT ip FROM yascheduler_nodes WHERE ip=:ip;", ip=ip_addr)
        return bool(self.conn.row_count)

    async def update_task_status(self, task_id: int, status: TaskStatus) -> None:
        """Update task status"""
        await self.run(
            "UPDATE yascheduler_tasks SET status=:status WHERE task_id=:task_id;",
            task_id=task_id,
            status=status.value,
        )

    async def get_all_nodes(self) -> Sequence[NodeModel]:
        """Get all nodes"""
        rows = await self.run(
            """SELECT ip, ncpus, enabled, cloud, username FROM yascheduler_nodes;"""
        )
        return [NodeModel(*x) for x in (rows or [])]

    async def get_enabled_nodes(self) -> Sequence[NodeModel]:
        """Get all enabled nodes"""
        rows = await self.run(
            """SELECT ip, ncpus, enabled, cloud, username
            FROM yascheduler_nodes WHERE enabled=TRUE;"""
        )
        return [y for y in [NodeModel(*x) for x in (rows or [])] if "." in y.ip]

    async def get_disabled_nodes(self) -> Sequence[NodeModel]:
        """Get all disabled nodes"""
        rows = await self.run(
            """SELECT ip, ncpus, enabled, cloud, username
            FROM yascheduler_nodes WHERE enabled=FALSE;"""
        )
        return [y for y in [NodeModel(*x) for x in (rows or [])] if "." in y.ip]

    async def get_node(self, ip_addr: str) -> Optional[NodeModel]:
        """Get node by ip"""
        rows = await self.run(
            """SELECT ip, ncpus, enabled, cloud, username
            FROM yascheduler_nodes
            WHERE ip=:ip;""",
            ip=ip_addr,
        )
        for row in rows or []:
            return NodeModel(*row)

    async def count_nodes_clouds(self) -> Mapping[str, int]:
        """Count nodes in clouds"""
        rows = await self.run(
            """SELECT cloud, COUNT(cloud) FROM yascheduler_nodes
            WHERE cloud IS NOT NULL GROUP BY cloud;"""
        )
        data = {}
        for row in rows or []:
            data[row[0]] = row[1]
        return data

    async def count_nodes_by_status(self) -> Mapping[bool, int]:
        """Count nodes by status"""
        rows = await self.run(
            """SELECT enabled, COUNT(ip) FROM yascheduler_nodes
            GROUP BY enabled ORDER BY enabled;"""
        )
        data = defaultdict(lambda: 0)
        for row in rows or []:
            data[bool(row[0])] = row[1]
        return data

    async def add_tmp_node(self, cloud: str, username: str) -> str:
        """Add temporary node"""
        rows = await self.run(
            """INSERT INTO yascheduler_nodes (ip, enabled, cloud, username)
            VALUES ('prov' || SUBSTR(MD5(RANDOM()::TEXT), 0, 11),
              FALSE, :cloud, :username)
            RETURNING ip;""",
            cloud=cloud,
            username=username,
        )
        rows = cast(List[List[str]], rows)
        return rows[0][0]

    async def add_node(
        self,
        ip_addr: str,
        username: str,
        ncpus: Optional[int] = None,
        cloud: Optional[str] = None,
        enabled: bool = False,
    ) -> NodeModel:
        """Add new node"""
        await self.run(
            """INSERT INTO yascheduler_nodes (ip, ncpus, enabled, cloud, username)
            VALUES (:ip, :ncpus, :enabled, :cloud, :username);""",
            ip=ip_addr,
            ncpus=ncpus,
            cloud=cloud,
            username=username,
            enabled=enabled,
        )
        return NodeModel(
            ip_addr, ncpus, enabled=enabled, cloud=cloud, username=username
        )

    async def enable_node(self, ip_addr: str) -> None:
        """Enable node"""
        await self.run(
            "UPDATE yascheduler_nodes SET enabled=TRUE WHERE ip=:ip;",
            ip=ip_addr,
        )

    async def disable_node(self, ip_addr: str) -> None:
        """Disable node"""
        await self.run(
            "UPDATE yascheduler_nodes SET enabled=FALSE WHERE ip=:ip;",
            ip=ip_addr,
        )

    async def remove_node(self, ip_addr: str) -> None:
        """Remove node"""
        await self.run("DELETE FROM yascheduler_nodes WHERE ip=:ip;", ip=ip_addr)

    async def get_task(self, task_id: int) -> Optional[TaskModel]:
        """Get task"""
        rows = await self.run(
            """SELECT task_id, label, ip, status, metadata
                FROM yascheduler_tasks
                WHERE task_id=:task_id;""",
            task_id=task_id,
        )
        for row in rows or []:
            return TaskModel(*row)

    async def get_task_ids_by_ip_and_status(
        self, ip_addr: str, status: TaskStatus
    ) -> Sequence[int]:
        """Get task ids by ip and status"""
        rows = await self.run(
            "SELECT task_id FROM yascheduler_tasks WHERE ip=:ip AND status=:status;",
            ip=ip_addr,
            status=status.value,
        )
        return [cast(int, x[0]) for x in (rows or [])]

    async def get_tasks_by_jobs(self, jobs: Sequence[int]) -> Sequence[TaskModel]:
        """Get tasks by ids"""
        rows = await self.run(
            """SELECT task_id, label, ip, status, metadata
            FROM yascheduler_tasks
            WHERE task_id IN (SELECT unnest(CAST (:task_ids AS int[])));""",
            task_ids=jobs,
        )
        return [TaskModel(*x) for x in (rows or [])]

    async def get_tasks_by_status(
        self, statuses: Sequence[TaskStatus], limit: Optional[int] = None
    ) -> Sequence[TaskModel]:
        """Get tasks by status"""
        rows = await self.run(
            """SELECT task_id, label, ip, status, metadata
            FROM yascheduler_tasks
            WHERE status IN (SELECT unnest(CAST (:statuses AS int[])))
            LIMIT :lim;""",
            statuses=[x.value for x in statuses],
            lim=limit,
        )
        return [TaskModel(*x) for x in (rows or [])]

    async def get_tasks_with_cloud_by_id_status(
        self, ids: Sequence[int], status: TaskStatus
    ) -> Sequence[TaskModel]:
        """Get tasks with cloud by id and status"""
        rows = await self.run(
            """SELECT t.task_id, t.label, t.ip, t.status, t.metadata, n.cloud
            FROM yascheduler_tasks AS t
            JOIN yascheduler_nodes AS n ON n.ip=t.ip
            WHERE status=:status AND
            task_id IN (SELECT unnest(CAST (:ids AS int[])));""",
            ids=ids,
            status=status.value,
        )
        return [TaskModel(*x) for x in (rows or [])]

    async def count_tasks_by_status(self) -> Mapping[TaskStatus, int]:
        """Count tasks by status"""
        rows = await self.run(
            """SELECT status, COUNT(task_id) FROM yascheduler_tasks
            GROUP BY status ORDER BY status;"""
        )
        data = defaultdict(lambda: 0)
        for row in rows or []:
            data[TaskStatus(row[0])] = row[1]
        return data

    async def add_task(
        self,
        label: Optional[str] = None,
        ip_addr: Optional[str] = None,
        status: TaskStatus = TaskStatus.TO_DO,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> TaskModel:
        """Add new task"""
        rows = await self.run(
            """INSERT INTO yascheduler_tasks (label, metadata, ip, status)
            VALUES (:label, :metadata, :ip, :status)
            RETURNING task_id, label, ip, status, metadata;""",
            label=label or "",
            metadata=metadata,
            ip=ip_addr,
            status=status.value,
        )
        return TaskModel(*cast(list, rows)[0])

    async def update_task_meta(self, task_id: int, metadata: Mapping[str, Any]):
        """Update task metadata"""
        await self.run(
            "UPDATE yascheduler_tasks SET metadata=:metadata WHERE task_id=:task_id;",
            task_id=task_id,
            metadata=metadata,
        )

    async def set_task_running(self, task_id: int, ip_addr: str):
        """Set task running"""
        await self.run(
            """UPDATE yascheduler_tasks
            SET status=:status, ip=:ip
            WHERE task_id=:task_id;""",
            task_id=task_id,
            status=TaskStatus.RUNNING.value,
            ip=ip_addr,
        )

    async def set_task_done(self, task_id: int, metadata: Mapping[str, Any]):
        """Set task done"""
        await self.run(
            """UPDATE yascheduler_tasks
            SET status=:status, metadata=:metadata
            WHERE task_id=:task_id;""",
            task_id=task_id,
            metadata=metadata,
            status=TaskStatus.DONE.value,
        )

    async def set_task_error(
        self, task_id: int, metadata: Mapping[str, Any], error: Optional[str] = None
    ):
        """Set task error"""
        new_meta = (
            dict(list(metadata.items()) + [("error", error)]) if error else metadata
        )
        await self.run(
            """UPDATE yascheduler_tasks
            SET status=:status, metadata=:metadata
            WHERE task_id=:task_id;""",
            task_id=task_id,
            metadata=new_meta,
            status=TaskStatus.DONE.value,
        )
