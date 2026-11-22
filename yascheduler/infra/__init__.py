"""Adapters layer facade — sole public surface for cross-layer consumers (application, composition root)."""
# region MODULE_CONTRACT
# PURPOSE: Provide a single, stable import surface for all infrastructure adapters so application code and entrypoints depend on one package boundary instead of scattered subpackages.
# SCOPE: Re-exports from infra subpackages for cross-layer consumers.
# KEYWORDS: adapters, facade, infra, re-export
# endregion MODULE_CONTRACT

from .cloud import CloudAdapter, CloudProvisionerImpl, resolve_adapter
from .local_inputs import capture_input_snapshot
from .notifier import webhook_handler
from .persistence import (
    MigrationState,
    MigrationStatus,
    PostgresUnitOfWork,
    apply_migrations,
    apply_schema,
    check_migration_status,
)
from .ssh import (
    AllSSHRetryExc,
    OccupancyChecker,
    OutputDownloader,
    SFTPRetryExc,
    SSHMachineRepository,
    TaskDeployer,
)
from .ssh.keys import list_private_keys

__all__ = [
    "AllSSHRetryExc",
    "CloudAdapter",
    "CloudProvisionerImpl",
    "MigrationState",
    "MigrationStatus",
    "OccupancyChecker",
    "OutputDownloader",
    "PostgresUnitOfWork",
    "SFTPRetryExc",
    "SSHMachineRepository",
    "TaskDeployer",
    "apply_migrations",
    "apply_schema",
    "capture_input_snapshot",
    "check_migration_status",
    "list_private_keys",
    "resolve_adapter",
    "webhook_handler",
]
