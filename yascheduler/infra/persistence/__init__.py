"""Persistence subpackage facade — re-exports persistence symbols for the adapters layer facade."""
# region MODULE_CONTRACT
# PURPOSE: Expose a stable, single-import surface for all persistence symbols so application and entrypoint layers depend on one package boundary instead of internal submodule paths.
# SCOPE: Persistence subpackage facade: UoW, repositories, schema applier, migration runner, SQL loader, exceptions.
# KEYWORDS: persistence, facade, uow, repository, schema, migration
# endregion MODULE_CONTRACT

from .db_config import PostgresDbConfig
from .exceptions import (
    NodeRowNotFoundError,
    TaskRowNotFoundError,
    UnitOfWorkNotInitializedError,
)
from .migration_base import EngineInputsContext
from .postgres_migrations import (
    MigrationState,
    MigrationStatus,
    apply_migrations,
    check_migration_status,
)
from .postgres_schema import apply_schema
from .postgres_uow import PostgresUnitOfWork

__all__ = [
    "EngineInputsContext",
    "MigrationState",
    "MigrationStatus",
    "NodeRowNotFoundError",
    "PostgresDbConfig",
    "PostgresUnitOfWork",
    "TaskRowNotFoundError",
    "UnitOfWorkNotInitializedError",
    "apply_migrations",
    "apply_schema",
    "check_migration_status",
]
