"""Migration base class for .py migrations with injected config/conn/log and begin()/commit() helpers."""
# region MODULE_CONTRACT
# PURPOSE: Provide a minimal contract for Python-based migrations — inject config, connection, logger, and an optional engine input-declarations context so subclasses implement only the migration step without plumbing infrastructure.
# SCOPE: Migration base class for .py migration subclasses; runner instantiates with (config, conn, log, engine_inputs=None).
# DEPENDENCIES: USES API: pg8000.Connection
# KEYWORDS: migration, base class, database migration, engine inputs context
# endregion MODULE_CONTRACT

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from logging import Logger

    from pg8000.native import Connection

    from yascheduler.shared import TypeAlias

    from .db_config import PostgresDbConfig

__all__ = ["EngineInputsContext", "Migration"]


# Engine name -> its declared input file selections, as plain domain values
# (ADR-0019). Plain tuples keep infra independent of the entrypoints config
# parser: initialization derives the mapping from the parsed engines config
# before invoking migrations.
EngineInputsContext: TypeAlias = Mapping[str, tuple[str, ...]]


# region CLASS_Migration
# PURPOSE: Define the contract for Python-based migrations — inject config, connection, logger, and the optional engine input-declarations context so subclasses implement only the migration logic without wiring infrastructure.
# RATIONALE:
# - Q: Why do begin() and commit() exist as helper methods when the runner already opens a transaction before calling migrate()?
#   A: Certain PostgreSQL operations cannot run inside an open transaction (CREATE INDEX CONCURRENTLY, VACUUM, REINDEX); the intended pattern is self.commit() to close the runner's transaction, run the non-transactional command, then self.begin() to reopen a transaction before the runner records the tracker — without these helpers a migration could not safely perform such operations.
# - Q: Why is engine_inputs optional with a None default?
#   A: Only input-converting migrations need engine declarations. Historical .py migrations keep their existing constructor dependencies (the fourth argument is keyword-optional), and callers without engine context (fresh bootstrap, already-applied migrations) omit it; a migration that requires declarations fails clearly when the context is None or lacks its engine.
class Migration:
    """Base class for ``.py`` database migrations.

    The runner instantiates exactly one subclass per ``.py`` migration file
    with ``(config, conn, log, engine_inputs=None)`` and calls ``migrate()``
    inside an open transaction. Subclasses use ``self.config``,
    ``self.conn``, ``self.log``, and ``self.engine_inputs`` directly.

    For migrations needing non-transactional operations
    (``CREATE INDEX CONCURRENTLY``, ``VACUUM``), use ``self.commit()`` to close
    the runner's transaction, run the command, then ``self.begin()`` to reopen
    one. Migrations are NOT required to be idempotent: the
    ``yascheduler_migrations`` tracker guards against re-application.
    """

    def __init__(
        self,
        config: PostgresDbConfig,
        conn: Connection,
        log: Logger,
        engine_inputs: EngineInputsContext | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.log = log
        self.engine_inputs = engine_inputs

    def begin(self) -> None:
        """Issue ``BEGIN`` on the wrapped connection."""
        self.conn.run("BEGIN")

    def commit(self) -> None:
        """Issue ``COMMIT`` on the wrapped connection."""
        self.conn.run("COMMIT")

    def migrate(self) -> None:
        """Run a single migration step."""
        raise NotImplementedError


# endregion CLASS_Migration
