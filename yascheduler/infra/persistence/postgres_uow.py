"""Unit of Work implementation for PostgreSQL using pg8000."""
# region MODULE_CONTRACT
# PURPOSE: Bound repository access to a single database transaction and dispatch collected domain events on commit, so the orchestrator's writes are atomic and side-effects are delivered exactly once.
# SCOPE: PostgresUnitOfWork managing transaction lifecycle, repository wiring, event collection and dispatch.
# DEPENDENCIES: USES API: pg8000.Connection
# KEYWORDS: unit of work, uow, postgres, transaction, event dispatch
# endregion MODULE_CONTRACT

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, TypeVar

from pg8000.native import Connection

from .exceptions import UnitOfWorkNotInitializedError
from .postgres import PostgresNodeRepository, PostgresTaskRepository

if TYPE_CHECKING:
    import types
    from collections.abc import Callable

    from yascheduler.application import MessageBus
    from yascheduler.domain import AnyTask, DomainEvent
    from yascheduler.shared import Self

    from .db_config import PostgresDbConfig

__all__ = ["PostgresUnitOfWork"]

logger = logging.getLogger(__name__)

T = TypeVar("T")

_cached_ssl_context: "ssl.SSLContext | None" = None


def _get_ssl_context() -> "ssl.SSLContext":
    """Return the process-wide cached SSL context for pg8000 connections.

    pg8000's ``Connection`` builds a fresh default SSL context per connect when
    no context is supplied, loading the full CA bundle each time. In a
    connection-retry loop this starves the GIL until ``Thread.start`` blocks
    and the orchestrator's event loop deadlocks (observed in production: every
    ``PostgresUnitOfWork`` reconnect piled up in
    ``SSLContext.load_default_certs``, ~2.7M one-thread executors). Creating one
    context per process removes the storm's fuel while keeping TLS semantics.
    """
    global _cached_ssl_context
    if _cached_ssl_context is None:
        _cached_ssl_context = ssl.create_default_context()
    return _cached_ssl_context


# region CLASS_PostgresUnitOfWork
# PURPOSE: Wrap repository access in a single transaction per request and dispatch collected domain events on commit, so the orchestrator's writes are atomic and side-effects fire exactly once.
class PostgresUnitOfWork:
    """Async context manager for PostgreSQL transactional boundaries."""

    def __init__(self, config: PostgresDbConfig, bus: MessageBus) -> None:
        """Initialise UoW with config and a single-worker thread pool."""
        # TODO(knopki): #001 no backoff.on_exception on InterfaceError
        self._config = config
        self._bus = bus
        self._saved_tasks: list[AnyTask] = []
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._conn: Connection | None = None
        self._tasks: PostgresTaskRepository | None = None
        self._nodes: PostgresNodeRepository | None = None

    @property
    def tasks(self) -> PostgresTaskRepository:
        """Tasks."""
        if self._tasks is None:
            msg = "UoW not entered; use 'async with' to access repositories"
            raise UnitOfWorkNotInitializedError(msg)
        return self._tasks

    @property
    def nodes(self) -> PostgresNodeRepository:
        """Nodes."""
        if self._nodes is None:
            msg = "UoW not entered; use 'async with' to access repositories"
            raise UnitOfWorkNotInitializedError(msg)
        return self._nodes

    # region METHOD___aenter__
    # PURPOSE: Open a transactional boundary around the orchestrator's unit of work so every repository write within the async with block participates in the same BEGIN/COMMIT cycle.
    # ENSURES: Connection is open with an active transaction; repositories are available via .tasks / .nodes.
    async def __aenter__(self) -> Self:
        """Open connection, begin transaction, wire repositories."""
        self._saved_tasks = []
        loop = asyncio.get_running_loop()
        try:
            self._conn = await loop.run_in_executor(
                self._executor,
                self._create_connection,
            )
            await loop.run_in_executor(self._executor, lambda: self._conn.run("BEGIN"))
            self._tasks = PostgresTaskRepository(
                self._conn,
                self._executor,
                self._saved_tasks,
            )
            self._nodes = PostgresNodeRepository(self._conn, self._executor)
        except BaseException:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    await loop.run_in_executor(self._executor, self._conn.close)
            self._executor.shutdown(wait=False)
            raise
        return self

    # endregion METHOD___aenter__

    # region METHOD___aexit__
    # PURPOSE: Rollback on error, close the connection, and release the thread-pool so resources never leak regardless of the exit path.
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> bool:
        """Rollback on error, close connection, shutdown executor."""
        if exc_type is not None and self._conn is not None:
            with contextlib.suppress(Exception):
                await self.rollback()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._run_sync(self._conn.close)
        self._executor.shutdown(wait=False)
        self._conn = None
        return False

    # endregion METHOD___aexit__

    # region METHOD_commit
    # PURPOSE: Persist all repository writes and deliver domain event side-effects atomically — commit fails = no writes + no events.
    async def commit(self) -> None:
        """Commit the transaction and dispatch collected events."""
        conn = self._require_conn()
        await self._run_sync(lambda: conn.run("COMMIT"))
        try:
            await self.publish_events()
        except Exception:
            logger.exception("event dispatch failed after commit")

    # endregion METHOD_commit

    # region METHOD_rollback
    # PURPOSE: Discard all uncommitted writes and collected events so a failed unit of work leaves the database in its pre-transaction state.
    async def rollback(self) -> None:
        """Rollback the transaction and discard events."""
        conn = self._require_conn()
        await self._run_sync(lambda: conn.run("ROLLBACK"))
        self._saved_tasks.clear()

    # endregion METHOD_rollback

    # region METHOD_collect_events
    # PURPOSE: Gather domain events emitted by saved aggregates so they can be dispatched after commit, ensuring side-effects match persisted state.
    async def collect_events(self) -> list[DomainEvent]:
        """Read events from all saved aggregates via the public events field and clear _saved_tasks."""
        events: list[DomainEvent] = []
        for task in self._saved_tasks:
            events.extend(task.events)
        self._saved_tasks.clear()
        return events

    # endregion METHOD_collect_events

    # region METHOD_publish_events
    # PURPOSE: Dispatch collected domain events through the message bus so registered handlers (webhooks, logging) react after a successful commit.
    async def publish_events(self) -> None:
        """Collect events and dispatch them via the message bus."""
        events = await self.collect_events()
        await self._bus.dispatch(events)
        self._saved_tasks.clear()

    # endregion METHOD_publish_events

    def _require_conn(self) -> Connection:
        """Return active connection, or raise if not yet entered."""
        if self._conn is None:
            msg = "Connection not initialized; use 'async with' to enter the UoW"
            raise UnitOfWorkNotInitializedError(
                msg,
            )
        return self._conn

    # region METHOD__create_connection
    # PURPOSE: Bootstrap a pg8000 connection from the frozen config so the UoW can start a transaction against the configured database.
    def _create_connection(self) -> Connection:
        """Create a new pg8000 connection from config."""
        kwargs: dict[str, object] = {}
        if self._config.ssl == "off":
            # Plaintext: no SSLContext is built at all. With ssl_context=None
            # pg8000 eagerly creates a default context (full CA-bundle load) on
            # every attempt — even when the server refuses SSL — so a
            # connection-retry loop starves the GIL until Thread.start blocks
            # and the orchestrator deadlocks (production incident: ~2.7M
            # one-thread executors piled in load_default_certs).
            kwargs["ssl_context"] = False
        elif self._config.ssl == "on":
            # One cached context per process instead of a fresh CA load per connect.
            kwargs["ssl_context"] = _get_ssl_context()
        # "prefer" keeps pg8000's default behaviour (try TLS, fall back to plaintext).
        return Connection(
            user=self._config.user,
            host=self._config.host,
            database=self._config.database,
            port=self._config.port,
            password=self._config.password,
            **kwargs,
        )

    # endregion METHOD__create_connection

    async def _run_sync(self, fn: Callable[[], T]) -> T:
        """Execute fn in the thread pool, return its result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)


# endregion CLASS_PostgresUnitOfWork
