# region MODULE_CONTRACT
# PURPOSE: Unit tests for INI → config parsing across all yascheduler config sub-modules.
# SCOPE: PostgresDbConfig, LocalSettings, RemoteDefaults, cloud configs, Engine, EngineRepository, warn_unknown_fields, Config top-level.
# KEYWORDS: INI config parsing, PostgresDbConfig, Engine, CloudConfigs
# endregion MODULE_CONTRACT

import warnings
from configparser import ConfigParser
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path, PurePath
from typing import cast

import pytest

from yascheduler.domain import (
    CloudConfig,
    Engine,
    EngineRepository,
    LocalSettings,
    RemoteDefaults,
)
from yascheduler.entrypoints._config_utils import ConfigWarning, warn_unknown_fields
from yascheduler.entrypoints.config_parser import (
    _parse_db_section,
    _parse_local_section,
    _parse_remote_section,
    parse_cloud_section,
    parse_clouds,
    parse_config,
    parse_engine_section,
)
from yascheduler.infra.cloud import (
    AzureImageReference,
    ConfigCloudAzure,
    ConfigCloudHetzner,
    ConfigCloudUpcloud,
    ConfigCloudVastAI,
    ConfigCloudVultr,
)
from yascheduler.infra.persistence import PostgresDbConfig


def test_config_db_full_overrides() -> None:
    """Parses full INI section with overrides"""
    cfg = ConfigParser()
    cfg.read_string(
        "[db]\nuser=myuser\npassword=secret\ndatabase=mydb\nhost=db.example.com\nport=5433\n",
    )
    db = _parse_db_section(cfg["db"])
    assert db.user == "myuser"
    assert db.password == "secret"
    assert db.database == "mydb"
    assert db.host == "db.example.com"
    assert db.port == 5433


def test_config_db_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Applies defaults when section has no keys.

    An absent `password` warns so production deploys don't silently run against
    the insecure default.
    """
    import logging

    cfg = ConfigParser()
    cfg.read_string("[db]\n")
    with caplog.at_level(
        logging.WARNING, logger="yascheduler.entrypoints.config_parser"
    ):
        db = _parse_db_section(cfg["db"])
    assert db.user == "yascheduler"
    assert db.password == "password"
    assert db.database == "database"
    assert db.host == "localhost"
    assert db.port == 5432
    assert any("password not set" in r.getMessage() for r in caplog.records)


def test_config_db_explicit_password_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicitly set `password` (even the literal default) does not warn."""
    import logging

    cfg = ConfigParser()
    cfg.read_string("[db]\npassword=password\n")
    with caplog.at_level(
        logging.WARNING, logger="yascheduler.entrypoints.config_parser"
    ):
        db = _parse_db_section(cfg["db"])
    assert db.password == "password"
    assert not any("password not set" in r.getMessage() for r in caplog.records)


def test_config_local_custom_data_dir(tmp_path: Path) -> None:
    """Derived paths resolve under custom data_dir; no warn when data_dir exists"""
    cfg = ConfigParser()
    cfg.read_string(f"[local]\ndata_dir={tmp_path}\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigWarning)
        local = _parse_local_section(cfg["local"])
    assert str(local.data_dir) == str(tmp_path)


def test_config_local_missing_data_dir_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A data_dir that does not exist on disk emits a logger.warning naming the path.

    Routed through the module logger (not warnings.warn) so it joins the
    daemon's configured log stream; ConfigWarning is reserved for structural
    config errors (unknown INI keys), not operational/environment conditions.
    """
    import logging

    missing = tmp_path / "does-not-exist"
    cfg = ConfigParser()
    cfg.read_string(f"[local]\ndata_dir={missing}\n")
    with caplog.at_level(
        logging.WARNING, logger="yascheduler.entrypoints.config_parser"
    ):
        local = _parse_local_section(cfg["local"])
    # parser still returns a LocalSettings so cloud flows (lazy keys_dir) keep working
    assert str(local.data_dir) == str(missing)
    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "data_dir does not exist" in r.getMessage()
    ]
    assert len(warning_records) == 1
    assert str(missing) in warning_records[0].getMessage()


def test_config_local_defaults() -> None:
    """Applies numeric defaults for empty section"""
    cfg = ConfigParser()
    cfg.read_string("[local]\n")
    local = _parse_local_section(cfg["local"])
    assert local.webhook_reqs_limit == 5
    assert local.conn_machine_limit == 10
    assert local.allocate_limit == 20
    assert local.consume_limit == 20
    assert local.deallocate_limit == 5
    assert local.webhook_url is None


def test_remote_defaults_jump_port_default() -> None:
    """RemoteDefaults.jump_port defaults to 22"""
    remote = RemoteDefaults()
    assert remote.jump_port == 22


def test_remote_defaults_importable_from_domain() -> None:
    """RemoteDefaults importable from domain facade"""
    from yascheduler.domain import RemoteDefaults as RemoteDefaultsFromDomain

    assert RemoteDefaultsFromDomain is RemoteDefaults


def test_remote_defaults_frozen() -> None:
    """RemoteDefaults raises FrozenInstanceError on field assignment"""
    remote = RemoteDefaults()
    with pytest.raises(FrozenInstanceError):
        remote.username = "ops"  # type: ignore[misc,assignment]


def test_config_remote_jump_port_defaults_to_22_when_absent() -> None:
    """jump_port defaults to 22 when [remote] key absent"""
    cfg = ConfigParser()
    cfg.read_string("[remote]\nuser=root\n")
    remote = _parse_remote_section(cfg["remote"])
    assert remote.jump_port == 22


def test_config_remote_jump_port_read_from_section() -> None:
    """jump_port read from [remote] section"""
    cfg = ConfigParser()
    cfg.read_string("[remote]\nuser=root\njump_port=2222\n")
    remote = _parse_remote_section(cfg["remote"])
    assert remote.jump_port == 2222


@pytest.mark.parametrize(
    ("raw_port", "match"),
    [
        pytest.param("0", "jump_port must be >= 1", id="below-1"),
        pytest.param("65536", "jump_port must be <= 65535", id="at-65536"),
        pytest.param("ssh", "invalid literal for int", id="non-integer"),
    ],
)
def test_config_remote_jump_port_rejects_invalid(raw_port: str, match: str) -> None:
    """[remote] parser rejects out-of-range and non-integer jump_port"""
    cfg = ConfigParser()
    cfg.read_string(f"[remote]\nuser=root\njump_port={raw_port}\n")
    with pytest.raises(ValueError, match=match):
        _parse_remote_section(cfg["remote"])


def test_config_remote_with_jump_host() -> None:
    """Parses jump host fields"""
    cfg = ConfigParser()
    cfg.read_string(
        "[remote]\nuser=admin\njump_user=jumper\njump_host=bastion.example.com\n",
    )
    remote = _parse_remote_section(cfg["remote"])
    assert remote.username == "admin"
    assert remote.jump_username == "jumper"
    assert remote.jump_host == "bastion.example.com"


def test_config_remote_without_jump_host() -> None:
    """Jump host fields default to None"""
    cfg = ConfigParser()
    cfg.read_string("[remote]\nuser=root\n")
    remote = _parse_remote_section(cfg["remote"])
    assert remote.username == "root"
    assert remote.jump_username is None
    assert remote.jump_host is None


def test_config_cloud_hetzner_jump_port_defaults_to_22_when_absent() -> None:
    """[clouds] hetzner_jump_port defaults to 22 when key absent"""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\nhetzner_token=tk\n")
    clouds = parse_clouds(cfg, RemoteDefaults())
    hetzner = next(c for c in clouds if isinstance(c, ConfigCloudHetzner))
    assert hetzner.jump_port == 22


def test_config_cloud_hetzner_jump_port_read_from_section() -> None:
    """[clouds] hetzner_jump_port=2222 → ConfigCloudHetzner.jump_port == 2222"""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\nhetzner_token=tk\nhetzner_jump_port=2222\n")
    clouds = parse_clouds(cfg, RemoteDefaults())
    hetzner = next(c for c in clouds if isinstance(c, ConfigCloudHetzner))
    assert hetzner.jump_port == 2222


@pytest.mark.parametrize(
    ("ini", "match"),
    [
        pytest.param(
            "az_tenant_id=tid\naz_client_id=cid\naz_client_secret=cs\n"
            "az_subscription_id=sid\naz_user=admin\naz_jump_port=0\n",
            "az_jump_port must be >= 1",
            id="az-jump-port-below-1",
        ),
        pytest.param(
            "upcloud_login=user\nupcloud_password=pass\nupcloud_jump_port=70000\n",
            "upcloud_jump_port must be <= 65535",
            id="upcloud-jump-port-65536",
        ),
        pytest.param(
            "hetzner_token=tk\nhetzner_jump_port=ssh\n",
            "invalid literal for int",
            id="hetzner-jump-port-non-integer",
        ),
        pytest.param(
            "hetzner_user=root\n",
            "hetzner_token is required",
            id="hetzner-missing-token",
        ),
        pytest.param(
            "upcloud_password=pass\n",
            "upcloud_login is required",
            id="upcloud-missing-login",
        ),
        pytest.param(
            "upcloud_login=user\n",
            "upcloud_password is required",
            id="upcloud-missing-password",
        ),
        pytest.param(
            "vastai_user=root\n",
            "vastai_api_key is required",
            id="vastai-missing-api-key",
        ),
        pytest.param(
            "vultr_user=root\n",
            "vultr_api_key is required",
            id="vultr-missing-api-key",
        ),
        pytest.param(
            "az_user=admin\n",
            "az_tenant_id is required",
            id="az-missing-tenant-id",
        ),
        pytest.param(
            "az_tenant_id=tid\naz_client_id=cid\naz_user=admin\n",
            "az_client_secret is required",
            id="az-missing-client-secret",
        ),
    ],
)
def test_config_clouds_reject_invalid_input(ini: str, match: str) -> None:
    """parse_clouds rejects out-of-range jump ports and missing required cloud credentials"""
    cfg = ConfigParser()
    cfg.read_string(f"[clouds]\n{ini}")
    with pytest.raises(ValueError, match=match):
        parse_clouds(cfg, RemoteDefaults())


def test_config_cloud_hetzner_parsing() -> None:
    """Parses hetzner token and username via parse_cloud_section"""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\nhetzner_token=abc123\nhetzner_user=root\n")
    hetzner = cast("ConfigCloudHetzner", parse_cloud_section(cfg["clouds"], "hetzner"))
    assert hetzner.token == "abc123"
    assert hetzner.username == "root"
    assert hetzner.server_type == "cx52"  # default
    assert hetzner.image_name == "debian-13"  # default


def test_config_cloud_upcloud_parsing() -> None:
    """Parses upcloud login and password via parse_cloud_section"""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\nupcloud_login=user\nupcloud_password=pass\n")
    upcloud = cast("ConfigCloudUpcloud", parse_cloud_section(cfg["clouds"], "upcloud"))
    assert upcloud.login == "user"
    assert upcloud.password == "pass"
    assert upcloud.username == "root"  # default


def test_azure_image_reference_from_urn() -> None:
    """Parses URN into publisher, offer, sku, version"""
    ref = AzureImageReference.from_urn("Publisher:Offer:SKU:1.0")
    assert ref.publisher == "Publisher"
    assert ref.offer == "Offer"
    assert ref.sku == "SKU"
    assert ref.version == "1.0"


def test_azure_image_reference_invalid_urn() -> None:
    """Raises ValueError for too-short URN"""
    with pytest.raises(ValueError):
        AzureImageReference.from_urn("a:b:c")


def test_config_cloud_azure_rejects_root() -> None:
    """parse_clouds raises ValueError for an explicitly-set root user on Azure."""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\naz_tenant_id=tid\naz_user=root\n")
    with pytest.raises(ValueError, match="Root user is forbidden on Azure"):
        parse_clouds(cfg, RemoteDefaults())


def test_config_cloud_azure_inherits_remote_root_without_banning() -> None:
    """An Azure section without az_user inherits [remote] username without tripping the root ban.

    Regression: the ban targets only an explicitly-set az_user. With the default
    [remote] user=root, an Azure section lacking az_user previously failed to load.
    """
    cfg = ConfigParser()
    cfg.read_string(
        "[clouds]\n"
        "az_tenant_id=tid\naz_client_id=cid\naz_client_secret=cs\n"
        "az_subscription_id=sid\n"
    )
    clouds = parse_clouds(cfg, RemoteDefaults(username="root"))
    az = next(c for c in clouds if isinstance(c, ConfigCloudAzure))
    assert az.username == "root"


@pytest.mark.filterwarnings(
    "error::yascheduler.entrypoints._config_utils.ConfigWarning",
)
def test_config_cloud_hetzner_package_upgrade_false() -> None:
    """[clouds] hetzner_package_upgrade=false → ConfigCloudHetzner.package_upgrade is False (no ConfigWarning)"""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\nhetzner_token=t\nhetzner_package_upgrade=false\n")
    clouds = parse_clouds(cfg, RemoteDefaults())
    hetzner = next(c for c in clouds if isinstance(c, ConfigCloudHetzner))
    assert hetzner.package_upgrade is False


def test_config_cloud_package_upgrade_defaults_true() -> None:
    """Absent hetzner_package_upgrade → ConfigCloudHetzner.package_upgrade is True (default)"""
    cfg = ConfigParser()
    cfg.read_string("[clouds]\nhetzner_token=t\n")
    clouds = parse_clouds(cfg, RemoteDefaults())
    hetzner = next(c for c in clouds if isinstance(c, ConfigCloudHetzner))
    assert hetzner.package_upgrade is True


def test_package_upgrade_not_on_cloud_config_protocol() -> None:
    """package_upgrade is NOT on the CloudConfig Protocol (infra-only consumer, like token/vm_size)"""
    assert not hasattr(CloudConfig, "package_upgrade")
    assert "package_upgrade" not in CloudConfig.__annotations__


def test_local_cloud_package_upgrade_now_warns_unknown() -> None:
    """A leftover [local] cloud_package_upgrade=false emits a ConfigWarning (clean break, no deprecation shim)"""
    cfg = ConfigParser()
    cfg.read_string("[local]\ncloud_package_upgrade=false\n")
    with pytest.warns(ConfigWarning, match="unknown fields"):
        local = _parse_local_section(cfg["local"])
    assert not hasattr(local, "cloud_package_upgrade")


def test_engine_valid_parsing() -> None:
    """Parses a valid engine section"""
    cfg = ConfigParser()
    cfg.read_string(
        "[engine.test]\n"
        "spawn={task_path} {engine_path} {ncpus}\n"
        "check_cmd=echo ok\n"
        "input_files=input.txt\n"
        "output_files=output.txt\n",
    )
    engine = parse_engine_section(cfg["engine.test"], PurePath())
    assert engine.name == "test"
    assert engine.spawn == "{task_path} {engine_path} {ncpus}"
    assert engine.check_cmd == "echo ok"
    assert engine.input_files == ("input.txt",)
    assert engine.output_files == ("output.txt",)


def test_engine_invalid_spawn_template() -> None:
    """Raises ValueError for unknown template placeholder in spawn"""
    with pytest.raises(ValueError, match="unknown"):
        cfg = ConfigParser()
        cfg.read_string(
            "[engine.test]\n"
            "spawn={unknown} {engine_path}\n"
            "check_cmd=echo ok\n"
            "input_files=input.txt\n"
            "output_files=out.txt\n",
        )
        parse_engine_section(cfg["engine.test"], PurePath())


def test_engine_missing_check_methods() -> None:
    """Raises ValueError when no check method is set"""
    with pytest.raises(ValueError):
        cfg = ConfigParser()
        cfg.read_string(
            "[engine.test]\n"
            "spawn={task_path}\n"
            "input_files=input.txt\n"
            "output_files=out.txt\n",
        )
        parse_engine_section(cfg["engine.test"], PurePath())


def test_engine_empty_input_files() -> None:
    """parse_engine_section raises ValueError when input_files is empty"""
    cfg = ConfigParser()
    cfg.read_string(
        "[engine.test]\nspawn={task_path}\ncheck_cmd=echo ok\noutput_files=output.txt\n",
    )
    with pytest.raises(ValueError):
        parse_engine_section(cfg["engine.test"], PurePath())


_ENGINE_A = Engine(
    name="a",
    spawn="{task_path}",
    check_cmd="echo ok",
    check_pname=None,
    input_files=("f",),
    output_files=("o",),
)
_ENGINE_B = Engine(
    name="b",
    spawn="{task_path}",
    check_cmd="echo ok",
    check_pname=None,
    input_files=("f",),
    output_files=("o",),
)


@pytest.fixture
def engine_repo() -> EngineRepository:
    """Repository holding engines a and b; both are frozen, so sharing is safe."""
    return EngineRepository(data={"a": _ENGINE_A, "b": _ENGINE_B})


def test_engine_repository_filter(engine_repo: EngineRepository) -> None:
    """Filter returns new repo with matching engines only"""
    filtered = engine_repo.filter(lambda e: e.name == "a")
    assert "a" in filtered
    assert "b" not in filtered
    assert isinstance(filtered, EngineRepository)


def test_engine_repository_filter_platforms() -> None:
    """filter_platforms returns engines for matching platforms"""
    e1 = Engine(
        name="a",
        spawn="{task_path}",
        check_cmd="echo ok",
        check_pname=None,
        input_files=("f",),
        output_files=("o",),
        platforms=("linux",),
    )
    e2 = Engine(
        name="b",
        spawn="{task_path}",
        check_cmd="echo ok",
        check_pname=None,
        input_files=("f",),
        output_files=("o",),
        platforms=("windows",),
    )
    repo = EngineRepository(data={"a": e1, "b": e2})
    filtered = repo.filter_platforms(["linux"])
    assert "a" in filtered
    assert "b" not in filtered


def test_engine_repository_immutable() -> None:
    """__setitem__ and __delitem__ raise TypeError"""
    repo = EngineRepository(data={"a": _ENGINE_A})
    with pytest.raises(TypeError):
        repo["b"] = _ENGINE_A  # type: ignore[index]  # intentional mutation of frozen dataclass
    with pytest.raises(TypeError):
        del repo["a"]  # type: ignore[attr-defined]  # intentional mutation of frozen dataclass


def test_engine_repository_keys(engine_repo: EngineRepository) -> None:
    """keys() returns list[str] of engine names"""
    result = engine_repo.keys()
    assert isinstance(result, list)
    assert set(result) == {"a", "b"}


def test_engine_repository_items(engine_repo: EngineRepository) -> None:
    """items() returns list of (name, engine) tuples"""
    result = engine_repo.items()
    assert isinstance(result, list)
    assert dict(result) == {"a": _ENGINE_A, "b": _ENGINE_B}


def test_engine_repository_iter(engine_repo: EngineRepository) -> None:
    """__iter__ yields engine names (str)"""
    names = list(engine_repo)
    assert set(names) == {"a", "b"}


def test_engine_repository_len() -> None:
    """__len__ returns the number of engines"""
    repo = EngineRepository(data={"a": _ENGINE_A})
    assert len(repo) == 1
    assert len(EngineRepository()) == 0


def test_engine_repository_is_mapping() -> None:
    """EngineRepository is registered as a Mapping virtual subclass"""
    from collections.abc import Mapping

    repo = EngineRepository(data={"a": _ENGINE_A})
    assert isinstance(repo, Mapping)


def test_warn_unknown_fields() -> None:
    """Emits ConfigWarning for unknown config keys"""
    cfg = ConfigParser()
    cfg.read_string("[db]\nuser=root\nunknown_key=value\n")
    with pytest.warns(ConfigWarning, match="unknown fields"):
        warn_unknown_fields(["user", "password", "database", "host", "port"], cfg["db"])


@pytest.mark.filterwarnings(
    "error::yascheduler.entrypoints._config_utils.ConfigWarning",
)
def test_config_remote_no_warnings_known_keys() -> None:
    """No warnings for known INI keys user and jump_user"""
    cfg = ConfigParser()
    cfg.read_string(
        "[remote]\nuser=admin\njump_user=jumper\njump_host=bastion\njump_port=22\n",
    )
    remote = _parse_remote_section(cfg["remote"])
    assert remote.username == "admin"
    assert remote.jump_username == "jumper"


def test_config_remote_warns_unknown_keys() -> None:
    """Emits ConfigWarning for unknown keys in remote section"""
    cfg = ConfigParser()
    cfg.read_string("[remote]\nuser=root\nbogus=yes\n")
    with pytest.warns(ConfigWarning, match="unknown fields"):
        _parse_remote_section(cfg["remote"])


def test_config_top_level_full_ini(tmp_path: Path) -> None:
    """Assembles all sub-configs from full INI"""
    ini = "[db]\nuser=myuser\n[local]\n[remote]\nuser=root\n[clouds]\n"
    cfg_file = tmp_path / "full.ini"
    cfg_file.write_text(ini)
    config = parse_config(str(cfg_file))
    assert isinstance(config.db, PostgresDbConfig)
    assert isinstance(config.local, LocalSettings)
    assert isinstance(config.remote, RemoteDefaults)
    # engines must be an EngineRepository
    assert isinstance(config.engines, EngineRepository)


def test_config_top_level_empty_sections(tmp_path: Path) -> None:
    """Handles empty sections with defaults"""
    ini = "[db]\n[local]\n[remote]\n[clouds]\n"
    cfg_file = tmp_path / "empty.ini"
    cfg_file.write_text(ini)
    config = parse_config(str(cfg_file))
    assert config.db.user == "yascheduler"  # default
    assert config.local.webhook_reqs_limit == 5  # default
    assert config.remote.username == "root"  # default


def test_vastai_cloud_section_round_trips(tmp_path: Path) -> None:
    """parse_config recognises [cloud.vastai] and produces a ConfigCloudVastAI entry.

    parse_config delegates to ConfigParser.read, which expects a file
    path (not INI contents), so the INI is written to a temp file and the path is
    passed. This exercises the real production parsing path.
    """
    ini = "[db]\n[local]\n[remote]\nuser=root\n[clouds]\nvastai_api_key=secretkey\n"
    cfg_file = tmp_path / "vastai.ini"
    cfg_file.write_text(ini)
    config = parse_config(str(cfg_file))
    vastai_entries = [c for c in config.clouds if isinstance(c, ConfigCloudVastAI)]
    assert len(vastai_entries) == 1
    assert vastai_entries[0].prefix == "vastai"
    assert vastai_entries[0].api_key == "secretkey"


def test_config_local_is_frozen_dataclass_without_get_private_keys() -> None:
    """LocalSettings is a stdlib frozen dataclass with keys_dir and no get_private_keys method"""
    assert is_dataclass(LocalSettings)
    # frozen: assigning to a field must raise FrozenInstanceError
    instance = LocalSettings()
    with pytest.raises(FrozenInstanceError):
        instance.data_dir = PurePath("/x")  # type: ignore[misc,assignment]
    # get_private_keys is gone (extracted to M-SSH-KEYS as list_private_keys)
    assert not hasattr(instance, "get_private_keys")
    # keys_dir field retained
    assert hasattr(instance, "keys_dir")


CLOUD_DTO_KWARGS: dict = {
    ConfigCloudAzure: {
        "tenant_id": "test-tid",
        "client_id": "test-cid",
        "client_secret": "test-secret",
        "subscription_id": "test-sub",
    },
    ConfigCloudHetzner: {"token": "test-token"},
    ConfigCloudUpcloud: {"login": "test", "password": "test"},
    ConfigCloudVastAI: {"api_key": "test-key"},
    ConfigCloudVultr: {"api_key": "test-key"},
}


def test_config_cloud_dtos_jump_port_default() -> None:
    """Each ConfigCloud* DTO has jump_port == 22 by default"""
    for dto_cls in (
        ConfigCloudAzure,
        ConfigCloudHetzner,
        ConfigCloudUpcloud,
        ConfigCloudVastAI,
    ):
        instance = dto_cls(**CLOUD_DTO_KWARGS.get(dto_cls, {}))
        assert instance.jump_port == 22, (
            f"{dto_cls.__name__}.jump_port should be 22, got {instance.jump_port}"
        )


def test_config_cloud_dtos_are_frozen_dataclasses_without_parser_methods() -> None:
    """ConfigCloud* DTOs are stdlib frozen dataclasses with no parser methods (relocated to infra.cloud)"""
    for dto_cls in (
        ConfigCloudAzure,
        ConfigCloudHetzner,
        ConfigCloudUpcloud,
        ConfigCloudVastAI,
        AzureImageReference,
    ):
        assert is_dataclass(dto_cls), f"{dto_cls.__name__} is not a dataclass"
        instance = dto_cls(**CLOUD_DTO_KWARGS.get(dto_cls, {}))
        # frozen: assigning to a declared field must raise FrozenInstanceError
        first_field = next(iter(dto_cls.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(instance, first_field, getattr(instance, first_field))
        # parser methods removed (moved to entrypoints.config_parser free functions);
        # only ConfigCloud* DTOs ever had them, but assert on all for consistency.
        assert not hasattr(dto_cls, "from_config_parser_section")
        assert not hasattr(dto_cls, "get_valid_config_parser_fields")


def test_config_local_post_init_rejects_zero_limit() -> None:
    """__post_init__ raises ValueError for allocate_limit=0 (preserving attrs ge(1))"""
    with pytest.raises(ValueError):
        LocalSettings(allocate_limit=0)


def test_config_local_parser_passes_zero_through_to_post_init() -> None:
    """Vastai parser fix companion: a 0 in INI reaches __post_init__ (no falsy 'or' coercion)"""
    cfg = ConfigParser()
    cfg.read_string("[local]\nallocate_limit=0\n")
    with pytest.raises(ValueError):
        _parse_local_section(cfg["local"])
