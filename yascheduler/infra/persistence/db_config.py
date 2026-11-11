"""PostgreSQL connection configuration as a frozen stdlib dataclass."""
# region MODULE_CONTRACT
# PURPOSE: Supply type-safe, immutable connection parameters so all persistence consumers (UoW, migrations, schema applier, CLI) connect to the same database without repeating defaults or parsing config ad-hoc.
# SCOPE: PostgresDbConfig frozen dataclass with user/password/database/host/port; no INI parsing.
# KEYWORDS: postgres, config, database connection, dataclass
# endregion MODULE_CONTRACT

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PostgresDbConfig"]


@dataclass(frozen=True)
class PostgresDbConfig:
    """PostgreSQL connection configuration."""

    user: str = "yascheduler"
    password: str = "password"  # noqa:  S105 not a real password
    database: str = "database"
    host: str = "localhost"
    port: int = 5432
    ssl: str = "prefer"

    # region BLOCK_validate
    def __post_init__(self) -> None:
        """Validate port >= 1 and the ssl mode."""
        if not self.port >= 1:
            msg = f"port must be >= 1, got {self.port}"
            raise ValueError(msg)
        if self.ssl not in ("prefer", "on", "off"):
            msg = f"ssl must be one of prefer/on/off, got {self.ssl!r}"
            raise ValueError(msg)

    # endregion BLOCK_validate
