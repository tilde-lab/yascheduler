"""Cross-layer application settings as frozen stdlib dataclasses — local daemon config and remote SSH defaults."""
# region MODULE_CONTRACT
# PURPOSE: Carry daemon and remote-SSH defaults as validated, immutable values shared across layers without re-parsing INI at each use site.
# SCOPE:
# - LocalSettings (daemon data paths, webhook, concurrency limits) and RemoteDefaults (remote SSH paths, username, jump host).
# - NOT: INI parsing (entrypoints.config_parser) or cloud-provider config (infra.cloud.cloud_configs).
# INVARIANTS: After construction, concurrency-limit fields are >= 1, webhook_reqs_limit >= 0, and RemoteDefaults.jump_port is in 1..MAX_PORT.
# KEYWORDS: settings, config, daemon, concurrency limits, webhook, jump host, LocalSettings, RemoteDefaults
# endregion MODULE_CONTRACT

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePath

from yascheduler.shared import MAX_PORT, validate_interval

__all__ = ["LocalSettings", "RemoteDefaults"]


# region CLASS_LocalSettings
# PURPOSE: Freeze the daemon's runtime configuration (paths, webhook, concurrency limits) so it is validated once and shared safely across async components.
# INVARIANTS: Concurrency-limit fields >= 1; webhook_reqs_limit >= 0.
@dataclass(frozen=True)
class LocalSettings:
    """Local daemon settings: data paths, concurrency limits, webhook config."""

    data_dir: Path = Path("./data")
    tasks_dir: Path = Path("./data/tasks")
    engines_dir: Path = Path("./data/engines")
    keys_dir: Path = Path("./data/keys")
    webhook_url: str | None = None
    webhook_reqs_limit: int = 5
    conn_machine_limit: int = 10
    conn_machine_pending: int = 10
    allocate_limit: int = 20
    allocate_pending: int = 1
    allocate_wave_min: int = 10
    consume_limit: int = 20
    consume_pending: int = 1
    deallocate_limit: int = 5
    deallocate_pending: int = 1

    def __post_init__(self) -> None:
        validate_interval("conn_machine_limit", self.conn_machine_limit, 1)
        validate_interval("conn_machine_pending", self.conn_machine_pending, 1)
        validate_interval("allocate_limit", self.allocate_limit, 1)
        validate_interval("allocate_pending", self.allocate_pending, 1)
        validate_interval("allocate_wave_min", self.allocate_wave_min, 1)
        validate_interval("consume_limit", self.consume_limit, 1)
        validate_interval("consume_pending", self.consume_pending, 1)
        validate_interval("deallocate_limit", self.deallocate_limit, 1)
        validate_interval("deallocate_pending", self.deallocate_pending, 1)
        validate_interval("webhook_reqs_limit", self.webhook_reqs_limit, 0)


# endregion CLASS_LocalSettings


# region CLASS_RemoteDefaults
# PURPOSE: Give every SSH-remote consumer a single immutable bundle of remote FS + jump-host defaults so they never re-derive paths or bastion identity at each call site.
# INVARIANTS: jump_port is in 1..MAX_PORT (mirrors the yascheduler_nodes.jump_port DB CHECK).
@dataclass(frozen=True)
class RemoteDefaults:
    """Remote machine defaults: data directories, SSH username, jump host."""

    data_dir: PurePath = field(default_factory=lambda: PurePath("./data"))
    tasks_dir: PurePath = field(default_factory=lambda: PurePath("./data/tasks"))
    engines_dir: PurePath = field(default_factory=lambda: PurePath("./data/engines"))
    username: str = "root"
    jump_username: str | None = None
    jump_host: str | None = None
    jump_port: int = 22

    def __post_init__(self) -> None:
        validate_interval("jump_port", self.jump_port, 1, MAX_PORT)


# endregion CLASS_RemoteDefaults
