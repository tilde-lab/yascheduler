"""INI config parsing — adapter between ConfigParser and domain/infra types."""
# region MODULE_CONTRACT
# PURPOSE: Adapt `ConfigParser` to the application's frozen typed-configuration model so the rest of the system consumes validated value objects and never touches raw INI proxies.
# RATIONALE:
# - Q: Why does INI parsing live in `entrypoints/config_parser.py` while the typed value objects (Engine, LocalSettings, RemoteDefaults, PostgresDbConfig, ConfigCloud*) live in `yascheduler.domain` and `yascheduler.infra`?
#   A: Keeping the typed value objects in domain/infra lets use cases and the orchestrator depend on business types without importing the parser (the spec's "domain does not reference an entrypoints module" rule).
# SCOPE: INI config parsing — engine sections, cloud provider sections, DB config, local/remote settings, and the top-level parse_config assembly.
# KEYWORDS: config, ini, parser, engine, cloud, database, settings
# endregion MODULE_CONTRACT

from __future__ import annotations

import dataclasses
import logging
import re
from configparser import ConfigParser
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from yascheduler.domain.engine import (
    Deploy,
    Engine,
    EngineRepository,
    LocalArchiveDeploy,
    LocalFilesDeploy,
    RemoteArchiveDeploy,
)
from yascheduler.domain.settings import LocalSettings, RemoteDefaults
from yascheduler.entrypoints.config import Config
from yascheduler.infra.cloud.cloud_configs import (
    AzureImageReference,
    ConfigCloudAzure,
    ConfigCloudHetzner,
    ConfigCloudUpcloud,
    ConfigCloudVastAI,
    ConfigCloudVultr,
)
from yascheduler.infra.persistence import PostgresDbConfig

from ._config_utils import warn_unknown_fields

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from configparser import SectionProxy

    from yascheduler.infra.cloud.cloud_configs import ConfigCloud

__all__ = ["parse_config"]
logger = logging.getLogger(__name__)


# region FUNC__check_spawn
# PURPOSE: Reject malformed spawn templates at parse time so a misconfigured engine fails fast at config load instead of producing a cryptic `KeyError` during task spawn on a remote node.
def _check_spawn(engine: Engine, value: str) -> None:
    try:
        value.format(task_path="", engine_path="", ncpus="")
    except KeyError as err:
        msg = "Engine {name} has unknown template placeholder `{placeholder}` in *spawn* command"
        raise ValueError(msg.format(name=engine.name, placeholder=err.args[0])) from err


# endregion FUNC__check_spawn


# region FUNC__check_check_
# PURPOSE: Enforce that every engine declares at least one liveness-check method so the daemon can detect task completion on a node — an engine with neither `check_cmd` nor `check_pname` is unusable and must fail at config load, not at first scheduling cycle.
def _check_check_(engine: Engine) -> None:
    if not engine.check_cmd and not engine.check_pname:
        msg = f"Engine {engine.name} has no *check_cmd* or *check_pname* set"
        raise ValueError(msg)


# endregion FUNC__check_check_


# region FUNC__check_at_least_one_elem
# PURPOSE: Reject engines that ship no input files or no output files so a task cannot be queued for an engine that would have nothing to upload or download — a misconfigured engine fails at config load, not at task dispatch.
def _check_at_least_one_elem(
    engine: Engine,
    field_name: str,
    value: Sequence[object] | None,
) -> None:
    if not value or len(value) < 1:
        msg = f"Engine {engine.name} has no *{field_name}* config set"
        raise ValueError(msg)


# endregion FUNC__check_at_least_one_elem


def _require_str(key: str, sec: SectionProxy) -> str:
    value = sec.get(key)
    if not value:
        msg = f"{key} is required"
        raise ValueError(msg)
    return value


# region FUNC_engine_valid_fields
# PURPOSE: Tell the unknown-field warning which `[engine.*]` INI keys are legitimate so a typo in an engine section surfaces as a warning at config load instead of silently being dropped on the floor.
def engine_valid_fields() -> Sequence[str]:
    """Return valid INI keys for an [engine.*] section."""
    exclude_names = ["name", "deployable"]
    include_names = [
        "deploy_local_files",
        "deploy_local_archive",
        "deploy_remote_archive",
    ]
    return [
        f.name for f in dataclasses.fields(Engine) if f.name not in exclude_names
    ] + include_names


# endregion FUNC_engine_valid_fields


def _engine_default(name: str) -> int:
    """Return the Engine dataclass field default for `name`."""
    return cast(
        "int", next(f for f in dataclasses.fields(Engine) if f.name == name).default
    )


# Regex matching versioned platform tags that normalize to their bare family.
_VERSIONED_PLATFORM = re.compile(r"^(debian|windows)-\d+$")


# region FUNC__normalize_platforms
# PURPOSE: Strip version suffixes from known versioned platform tags (debian-N, windows-N) at config load, warning once per tag, so engine.platforms carries only family-level tags that the platform-intersection matcher and cloud supported_platform_checks understand.
def _normalize_platforms(raw: tuple[str, ...], engine_name: str) -> tuple[str, ...]:
    """Normalize versioned platform tags to their family, warning on each rewrite."""
    normalized: list[str] = []
    for tag in raw:
        match = _VERSIONED_PLATFORM.match(tag)
        if match:
            family = match.group(1)
            logger.warning(
                "Engine %s: versioned platform tag %r normalized to %r; version pinning is not supported",
                engine_name,
                tag,
                family,
            )
            normalized.append(family)
        else:
            normalized.append(tag)
    # Dedup while preserving order, so debian-10 debian-11 -> debian (not debian debian).
    seen: set[str] = set()
    deduped: list[str] = []
    for tag in normalized:
        if tag not in seen:
            seen.add(tag)
            deduped.append(tag)
    return tuple(deduped)


# endregion FUNC__normalize_platforms


# region FUNC_parse_engine_section
# PURPOSE: Turn one INI `[engine.*]` section into a frozen `Engine` value object the orchestrator can match against task requirements, with every malformed config (unknown spawn placeholder, missing check method, empty input/output list, missing spawn) surfacing as `ValueError` at config load rather than as a cryptic failure during task scheduling.
def parse_engine_section(sec: SectionProxy, engines_dir: PurePath) -> Engine:
    """Build a frozen Engine from a single [engine.*] section."""
    warn_unknown_fields(engine_valid_fields(), sec)

    def gettuple(key: str) -> tuple[str, ...]:
        return tuple(x.strip() for x in filter(None, sec.get(key, fallback="").split()))

    name = sec.name[7:]
    engine_dir = engines_dir / name

    deployable: list[Deploy] = []
    deploy_local_files = [
        engine_dir / x.strip() for x in gettuple("deploy_local_files")
    ]
    if deploy_local_files:
        deployable.append(LocalFilesDeploy(files=tuple(deploy_local_files)))
    deploy_local_archive = sec.get("deploy_local_archive", None)
    if deploy_local_archive:
        deployable.append(LocalArchiveDeploy(file=engine_dir / deploy_local_archive))
    deploy_remote_archive = sec.get("deploy_remote_archive", None)
    if deploy_remote_archive:
        deployable.append(RemoteArchiveDeploy(url=deploy_remote_archive))

    spawn = sec.get("spawn")
    if spawn is None:
        msg = f"Engine {name} has no spawn command"
        raise ValueError(msg)
    input_files = gettuple("input_files")
    output_files = gettuple("output_files")

    engine = Engine(
        name=name,
        spawn=spawn,
        check_cmd=sec.get("check_cmd"),
        check_cmd_code=sec.getint(
            "check_cmd_code", fallback=_engine_default("check_cmd_code")
        ),
        check_pname=sec.get("check_pname"),
        deployable=tuple(deployable),
        input_files=input_files,
        output_files=output_files,
        sleep_interval=sec.getint(
            "sleep_interval", fallback=_engine_default("sleep_interval")
        ),
        platforms=_normalize_platforms(gettuple("platforms"), name),
        platform_packages=gettuple("platform_packages"),
        exclude_nodes=frozenset(gettuple("exclude_nodes")),
    )

    # region BLOCK_validate_engine
    _check_spawn(engine, engine.spawn)
    _check_check_(engine)
    _check_at_least_one_elem(engine, "input_files", engine.input_files)
    _check_at_least_one_elem(engine, "output_files", engine.output_files)
    # endregion BLOCK_validate_engine
    return engine


# endregion FUNC_parse_engine_section


# region FUNC_parse_engines
# PURPOSE: Collect every `[engine.*]` section in the INI into one frozen `EngineRepository` so the orchestrator and allocator have a single read-only registry to match task platforms against, built once at config load and never re-parsed.
# ENSURES:
# - Returns an `EngineRepository` whose `data` maps each section suffix (the engine name) to the `Engine` returned by `parse_engine_section`
# - Iterates only sections whose name starts with the literal `engine.` prefix (other sections are invisible)
def parse_engines(cfg: ConfigParser, engines_dir: PurePath) -> EngineRepository:
    """Parse all engine.* sections from an INI config into an EngineRepository."""
    snames = filter(lambda x: x.startswith("engine."), cfg.sections())
    data: dict[str, Engine] = {}
    for sname in snames:
        engine = parse_engine_section(cfg[sname], engines_dir)
        data[engine.name] = engine
    return EngineRepository(data=data)


# endregion FUNC_parse_engines

# ============================================================================
# Cloud config parsers
# ============================================================================


# region FUNC__read_fields
# PURPOSE: Stop duplicating field defaults in the parser — read every dataclass
# field straight from INI, defaulting each to its own DTO default, so the DTOs
# stay the single source of truth and the parser cannot drift.
# INVARIANTS:
# - A field with no default is required; absence raises naming the INI key.
# - Numeric/bool fields are coerced via the matching ConfigParser getter.
# - `coerce` overrides handle fields whose value needs transformation.
def _field_default(f: dataclasses.Field[object]) -> object:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:
        return f.default_factory()
    return dataclasses.MISSING


def _read_fields(
    sec: SectionProxy,
    dto_cls: type,
    *,
    prefix: str = "",
    aliases: Mapping[str, str] | None = None,
    coerce: Mapping[str, Callable[[SectionProxy, str, object], object]] | None = None,
) -> dict[str, object]:
    aliases = aliases or {}
    coerce = coerce or {}
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(dto_cls):
        suffix = aliases.get(f.name, f.name)
        key = f"{prefix}_{suffix}" if prefix else suffix
        if f.name in coerce:
            kwargs[f.name] = coerce[f.name](sec, key, _field_default(f))
            continue
        default = _field_default(f)
        if default is dataclasses.MISSING:
            kwargs[f.name] = _require_str(key, sec)
            continue
        if f.type == "int":
            kwargs[f.name] = sec.getint(key, fallback=cast("int", default))
        elif f.type == "float":
            kwargs[f.name] = sec.getfloat(key, fallback=cast("float", default))
        elif f.type == "bool":
            kwargs[f.name] = sec.getboolean(key, fallback=cast("bool", default))
        else:
            kwargs[f.name] = sec.get(key, default)
    return kwargs


# endregion FUNC__read_fields


# INI key suffix when it differs from the dataclass field name. `user`/`jump_user`
# are the INI shorthand; Python keeps `username`/`jump_username` everywhere
# (Node, RemoteDefaults, conn_opts)e.
# `image`/`size` are Azure's vm_image/vm_size INI names.
_CLOUD_ALIASES: Mapping[str, str] = {
    "username": "user",
    "jump_username": "jump_user",
    "vm_size": "size",
    "vm_image": "image",
}


def _coerce_azure_image(
    sec: SectionProxy, key: str, default: object
) -> AzureImageReference:
    urn = sec.get(key)
    return (
        AzureImageReference.from_urn(urn)
        if urn
        else cast("AzureImageReference", default)
    )


def _coerce_onstart_script(sec: SectionProxy, key: str, _default: object) -> str | None:
    path = sec.get(key)
    if not path:
        return None
    if not Path(path).exists():
        msg = f"{key} must be a readable file path or empty, got {path}"
        raise ValueError(msg)
    return Path(path).read_text()


# every cloud provider shares the same field-alias shape — DTOs name
# the field `username`/`jump_username`, the INI shorthand is `user`/`jump_user`.
# Three providers (hetzner/upcloud/vultr) share it verbatim; only Azure (adds
# vm_image/vm_size aliases) and VastAI (adds an `env` field excluded from INI)
# diverge. One base pair + per-provider deltas, not five copy-pasted pairs.
_BASE_EXCL = {"prefix", "username", "jump_username"}
_BASE_INCL = ["user", "jump_user"]


def _ban_root_user(sec: SectionProxy, prefix: str) -> None:
    # Spec-mandated at parse time; must fire before required-credential checks.
    if sec.get(f"{prefix}_user") == "root":
        msg = "Root user is forbidden on Azure"
        raise ValueError(msg)


class _CloudSpec(NamedTuple):
    dto_cls: type
    excludes: set[str]
    includes: list[str]
    coerce: Mapping[str, Callable[[SectionProxy, str, object], object]]
    pre_check: Callable[[SectionProxy, str], None] | None


# Single source of truth per provider: DTO, valid-field rules, optional
# per-field coercion, optional parse-time pre-check. Adding a provider is one
# row — no parallel registry (parsers / DTOs / field-rules) to keep in sync.
_CLOUD_SPECS: dict[str, _CloudSpec] = {
    "az": _CloudSpec(
        ConfigCloudAzure,
        _BASE_EXCL | {"vm_image", "vm_size"},
        ["user", "jump_user", "image", "size"],
        {"vm_image": _coerce_azure_image},
        _ban_root_user,
    ),
    "hetzner": _CloudSpec(ConfigCloudHetzner, _BASE_EXCL, _BASE_INCL, {}, None),
    "upcloud": _CloudSpec(ConfigCloudUpcloud, _BASE_EXCL, _BASE_INCL, {}, None),
    "vastai": _CloudSpec(
        ConfigCloudVastAI,
        _BASE_EXCL | {"env"},
        _BASE_INCL,
        {"onstart_script": _coerce_onstart_script},
        None,
    ),
    "vultr": _CloudSpec(ConfigCloudVultr, _BASE_EXCL, _BASE_INCL, {}, None),
}


# region FUNC_cloud_valid_fields
# PURPOSE: Return valid INI keys for a [clouds] sub-section keyed by a cloud provider prefix.
def cloud_valid_fields(prefix: str) -> Sequence[str]:
    """Return valid INI keys for a [clouds] sub-section keyed by a cloud provider prefix."""
    spec = _CLOUD_SPECS[prefix]
    return [
        f"{prefix}_{name}"
        for name in (
            [
                f.name
                for f in dataclasses.fields(spec.dto_cls)
                if f.name not in spec.excludes
            ]
            + spec.includes
        )
    ]


# endregion FUNC_cloud_valid_fields


# Every [clouds] sub-section shares one option namespace, so unknown-field
# warnings check the union of all providers' valid keys.
_ALL_CLOUD_VALID_FIELDS: list[str] = [
    field for prefix in _CLOUD_SPECS for field in cloud_valid_fields(prefix)
]


# region FUNC_parse_cloud_section
# PURPOSE: Build a ConfigCloud DTO from a [clouds] sub-section via the per-prefix spec table — one code path replaces five near-identical provider parsers.
def parse_cloud_section(sec: SectionProxy, prefix: str) -> ConfigCloud:
    """Dispatch a [clouds] sub-section to its per-prefix spec and build the DTO.

    Pre-checks are intentionally NOT run here: they are invoked by
    ``parse_clouds`` on the raw section before inheritance injection, where the
    explicit-vs-inherited distinction (e.g. the Azure root ban) still exists.
    """
    spec = _CLOUD_SPECS[prefix]
    warn_unknown_fields(_ALL_CLOUD_VALID_FIELDS, sec)
    return spec.dto_cls(
        **cast(
            "dict[str, Any]",
            _read_fields(
                sec,
                spec.dto_cls,
                prefix=prefix,
                aliases=_CLOUD_ALIASES,
                coerce=spec.coerce,
            ),
        )
    )


# endregion FUNC_parse_cloud_section


# region FUNC_parse_clouds
# PURPOSE: Build the list of ConfigCloud DTOs from a [clouds] section, inheriting remote.username for missing prefix users.
# RATIONALE:
# - Q: Why derive prefixes from [clouds] option names instead of an explicit list?
#   A: So adding a provider is one parser + one registry entry, with no separate prefix-list key to forget.
def parse_clouds(cfg: ConfigParser, remote: RemoteDefaults) -> list[ConfigCloud]:
    """Build the list of ConfigCloud DTOs from a [clouds] section, inheriting remote.username for missing prefix users."""
    if not cfg.has_section("clouds"):
        cfg.add_section("clouds")
    sec = cfg["clouds"]

    # Derive cloud prefixes from [clouds] option names (first segment of `{prefix}_*`).
    cloud_prefixes = {name.split("_")[0] for name in cfg.options("clouds")}
    known = [prefix for prefix in cloud_prefixes if prefix in _CLOUD_SPECS]

    # Pre-checks run on the raw section BEFORE inheritance injection, so they
    # see only explicitly-set values — e.g. the Azure root ban must not fire on
    # a username copied from [remote] (spec targets an explicitly-set az_user).
    for prefix in known:
        pre_check = _CLOUD_SPECS[prefix].pre_check
        if pre_check is not None:
            pre_check(sec, prefix)

    # Inherit remote.username into [clouds] for any prefix whose {prefix}_user is absent.
    for prefix in cloud_prefixes:
        user_key = f"{prefix}_user"
        if user_key not in cfg.options("clouds"):
            sec[user_key] = remote.username

    # Dispatch each known prefix to its spec; unknown prefixes are silently
    # skipped (they would warn via warn_unknown_fields inside every parser call).
    return [parse_cloud_section(sec, prefix) for prefix in known]


# endregion FUNC_parse_clouds

# ============================================================================
# db / local / remote section parsers + parse_config assembly
# ============================================================================


def _db_valid_fields() -> Sequence[str]:
    return [f.name for f in dataclasses.fields(PostgresDbConfig)]


# region FUNC__parse_db_section
# PURPOSE: Build a frozen PostgresDbConfig from a [db] INI section.
# INVARIANTS: A [db] section without an explicit `password` key emits a
# logger.warning naming the insecure default, so production deploys don't
# silently run against the literal default password.
def _parse_db_section(sec: SectionProxy) -> PostgresDbConfig:
    warn_unknown_fields(_db_valid_fields(), sec)
    if "password" not in sec:
        logger.warning(
            "[db] password not set — using insecure default; "
            "set `password` under [db] for production",
        )
    return PostgresDbConfig(
        **cast("dict[str, Any]", _read_fields(sec, PostgresDbConfig))
    )


# endregion FUNC__parse_db_section


def _local_valid_fields() -> Sequence[str]:
    return [f.name for f in dataclasses.fields(LocalSettings)]


def _local_default(name: str) -> object:
    """Return the LocalSettings dataclass field default for `name`."""
    return _field_default(
        next(f for f in dataclasses.fields(LocalSettings) if f.name == name)
    )


# region FUNC__parse_local_section
# PURPOSE: Build a frozen LocalSettings from a [local] INI section.
# INVARIANTS: A data_dir that does not exist on the filesystem at parse time emits a logger.warning naming the missing path.
def _parse_local_section(sec: SectionProxy) -> LocalSettings:
    warn_unknown_fields(_local_valid_fields(), sec)
    data_dir = Path(sec.get("data_dir", str(_local_default("data_dir")))).resolve()
    # region BLOCK_warn_missing_data_dir
    # data_dir is the parent of keys_dir/tasks_dir/engines_dir; if it does not
    # exist, the keys_dir is created lazily by list_private_keys() and
    # get_or_create_ssh_key() on first use; do not raise, so cloud flows keep working.
    if not data_dir.exists():
        logger.warning("[local] data_dir does not exist: %s", data_dir)
    # endregion BLOCK_warn_missing_data_dir
    return LocalSettings(
        data_dir=data_dir,
        tasks_dir=Path(sec.get("tasks_dir", str(data_dir / "tasks"))).resolve(),
        engines_dir=Path(sec.get("engines_dir", str(data_dir / "engines"))).resolve(),
        keys_dir=Path(sec.get("keys_dir", str(data_dir / "keys"))).resolve(),
        webhook_url=sec.get("webhook_url"),
        # ponytail: filter Nones so dataclass defaults apply; keep explicit 0 so
        # __post_init__ ge(1) still rejects it (sec.getint returns None for absent keys).
        **{
            k: v
            for k, v in {
                "webhook_reqs_limit": sec.getint("webhook_reqs_limit"),
                "conn_machine_limit": sec.getint("conn_machine_limit"),
                "conn_machine_pending": sec.getint("conn_machine_pending"),
                "allocate_limit": sec.getint("allocate_limit"),
                "allocate_pending": sec.getint("allocate_pending"),
                "consume_limit": sec.getint("consume_limit"),
                "consume_pending": sec.getint("consume_pending"),
                "deallocate_limit": sec.getint("deallocate_limit"),
                "deallocate_pending": sec.getint("deallocate_pending"),
            }.items()
            if v is not None
        },
    )


# endregion FUNC__parse_local_section


def _remote_valid_fields() -> Sequence[str]:
    exclude_names = ["username", "jump_username"]
    include_names = ["user", "jump_user"]
    return [
        f.name
        for f in dataclasses.fields(RemoteDefaults)
        if f.name not in exclude_names
    ] + include_names


# region FUNC__parse_remote_section
# PURPOSE: Turn a `[remote]` INI section into a `RemoteDefaults` value object so the rest of the system consumes immutable typed values instead of re-reading `ConfigParser` proxies at every SSH call site.
# INVARIANTS: `user`/`jump_user` are INI aliases for `username`/`jump_username`; `jump_port` range is enforced by `RemoteDefaults.__post_init__` (mirrors the `yascheduler_nodes.jump_port` DB CHECK). `engines_dir`/`tasks_dir` derive from `data_dir` at runtime, so they are computed after `_read_fields` resolves the fixed defaults.
def _parse_remote_section(sec: SectionProxy) -> RemoteDefaults:
    warn_unknown_fields(_remote_valid_fields(), sec)
    kwargs = _read_fields(
        sec,
        RemoteDefaults,
        aliases={"username": "user", "jump_username": "jump_user"},
        coerce={"data_dir": lambda s, k, d: PurePath(s.get(k, str(d)))},
    )
    data_dir = cast("PurePath", kwargs["data_dir"])
    kwargs["engines_dir"] = PurePath(sec.get("engines_dir", str(data_dir / "engines")))
    kwargs["tasks_dir"] = PurePath(sec.get("tasks_dir", str(data_dir / "tasks")))
    return RemoteDefaults(**cast("dict[str, Any]", kwargs))


# endregion FUNC__parse_remote_section


# region FUNC_parse_config
# PURPOSE: Read an INI file, parse each section via per-section parser functions, and return a frozen Config aggregate.
def parse_config(path: str | bytes | PurePath) -> Config:
    """Parse an INI config file (path or contents) into a frozen Config aggregate."""
    cfg = ConfigParser()
    cfg.read(path)

    for sec_name in ("db", "local", "remote", "clouds"):
        if not cfg.has_section(sec_name):
            cfg.add_section(sec_name)

    local = _parse_local_section(cfg["local"])
    remote = _parse_remote_section(cfg["remote"])
    clouds = parse_clouds(cfg, remote)

    return Config(
        db=_parse_db_section(cfg["db"]),
        local=local,
        remote=remote,
        clouds=clouds,
        engines=parse_engines(cfg, local.engines_dir),
    )


# endregion FUNC_parse_config
