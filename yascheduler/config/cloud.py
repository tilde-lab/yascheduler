#!/usr/bin/env python3
"""Cloud configurations"""

from collections.abc import Sequence
from configparser import SectionProxy
from functools import partial
from typing import Optional, Union

from attrs import define, field, fields, validators

from ..compat import Self
from .utils import make_default_field, opt_str_val, warn_unknown_fields


def _check_az_user(_: "ConfigCloudAzure", __, value: str):
    if value == "root":
        raise ValueError("Root user is forbidden on Azure")


def _fmt_key(prefix: str, name: str):
    return f"{prefix}_{name}"


@define(frozen=True)
class AzureImageReference:
    """Azure's image reference"""

    publisher: str = field(default="Debian")
    offer: str = field(default="debian-11-daily")
    sku: str = field(default="11-backports-gen2")
    version: str = field(default="latest")

    @classmethod
    def from_urn(cls, urn: str) -> Self:
        "Create image reference from urn in forma `publisher:offer:sku:version'"
        parts = urn.split(":", maxsplit=4)
        if len(parts) < 4:
            raise ValueError(
                "`Image reference URN should be in format publisher:offer:sku:version"
            )

        return cls(*parts)


@define(frozen=True)
class ConfigCloudAzure:
    """Azure cloud configuration"""

    prefix = "az"
    tenant_id: str = field(validator=validators.instance_of(str))
    client_id: str = field(validator=validators.instance_of(str))
    client_secret: str = field(validator=validators.instance_of(str))
    subscription_id: str = field(validator=validators.instance_of(str))
    resource_group: str = make_default_field("yascheduler-rg")
    location: str = make_default_field("westeurope")
    vnet: str = make_default_field("yascheduler-vnet")
    subnet: str = make_default_field("yascheduler-subnet")
    nsg: str = make_default_field("yascheduler-nsg")
    vm_image: AzureImageReference = make_default_field(AzureImageReference())
    vm_size: str = make_default_field("Standard_B1s")
    max_nodes: int = make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = make_default_field("yascheduler", extra_validators=[_check_az_user])
    priority: int = make_default_field(0)
    idle_tolerance: int = make_default_field(300, extra_validators=[validators.ge(1)])
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def get_valid_config_parser_fields(cls) -> Sequence[str]:
        "Returns a list of valid config keys"
        exclude_names = ["prefix", "username", "jump_username", "vm_image", "vm_size"]
        include_names = ["user", "jump_user", "image", "size"]
        return [
            f"{cls.prefix}_{x}"
            for x in [f.name for f in fields(cls) if f.name not in exclude_names]
            + include_names
        ]

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudAzure":
        "Create config from config parser's section"

        fmt = partial(_fmt_key, cls.prefix)

        warn_unknown_fields(
            [
                *cls.get_valid_config_parser_fields(),
                *ConfigCloudHetzner.get_valid_config_parser_fields(),
                *ConfigCloudUpcloud.get_valid_config_parser_fields(),
                *ConfigCloudVastAI.get_valid_config_parser_fields(),
            ],
            sec,
        )

        vm_image = sec.get(fmt("image"))
        image_ref = None
        if vm_image:
            image_ref = AzureImageReference.from_urn(vm_image)

        return cls(
            tenant_id=sec.get(fmt("tenant_id")),  # type: ignore
            client_id=sec.get(fmt("client_id")),  # type: ignore
            client_secret=sec.get(fmt("client_secret")),  # type: ignore
            subscription_id=sec.get(fmt("subscription_id")),  # type: ignore
            resource_group=sec.get(fmt("resource_group")),  # type: ignore
            location=sec.get(fmt("location")),  # type: ignore
            vnet=sec.get(fmt("vnet")),  # type: ignore
            subnet=sec.get(fmt("subnet")),  # type: ignore
            nsg=sec.get(fmt("nsg")),  # type: ignore
            vm_image=image_ref or AzureImageReference(),
            vm_size=sec.get(fmt("size")),  # type: ignore
            max_nodes=sec.getint(fmt("max_nodes")),  # type: ignore
            username=sec.get(fmt("user")),  # type: ignore
            priority=sec.getint(fmt("priority")),  # type: ignore
            idle_tolerance=sec.getint(fmt("idle_tolerance")),  # type: ignore
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


@define(frozen=True)
class ConfigCloudHetzner:
    """Hetzner cloud configuration"""

    prefix = "hetzner"
    token: str = field(validator=validators.instance_of(str))
    max_nodes: int = make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = make_default_field("root")
    priority: int = make_default_field(0)
    server_type: str = make_default_field("cx52")
    location: Optional[str] = field(default=None, validator=opt_str_val)
    image_name: str = make_default_field("debian-11")
    idle_tolerance: int = make_default_field(120, extra_validators=[validators.ge(1)])
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def get_valid_config_parser_fields(cls) -> Sequence[str]:
        "Returns a list of valid config keys"
        exclude_names = ["prefix", "username", "jump_username"]
        include_names = ["user", "jump_user"]
        return [
            f"{cls.prefix}_{x}"
            for x in [f.name for f in fields(cls) if f.name not in exclude_names]
            + include_names
        ]

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudHetzner":
        "Create config from config parser's section"
        fmt = partial(_fmt_key, cls.prefix)

        warn_unknown_fields(
            [
                *ConfigCloudAzure.get_valid_config_parser_fields(),
                *cls.get_valid_config_parser_fields(),
                *ConfigCloudUpcloud.get_valid_config_parser_fields(),
                *ConfigCloudVastAI.get_valid_config_parser_fields(),
            ],
            sec,
        )

        return cls(
            token=sec.get(fmt("token")),  # type: ignore
            max_nodes=sec.getint(fmt("max_nodes")),  # type: ignore
            username=sec.get(fmt("user")),  # type: ignore
            server_type=sec.get(fmt("server_type")),  # type: ignore
            location=sec.get(fmt("location")),
            image_name=sec.get(fmt("image_name")),  # type: ignore
            priority=sec.getint(fmt("priority")),  # type: ignore
            idle_tolerance=sec.getint(fmt("idle_tolerance")),  # type: ignore
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


@define(frozen=True)
class ConfigCloudUpcloud:
    """Upcloud cloud configuration"""

    prefix = "upcloud"
    login: str = field(validator=validators.instance_of(str))
    password: str = field(validator=validators.instance_of(str))
    max_nodes: int = make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = make_default_field("root")
    priority: int = make_default_field(0)
    idle_tolerance: int = make_default_field(120, extra_validators=[validators.ge(1)])
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def get_valid_config_parser_fields(cls) -> Sequence[str]:
        "Returns a list of valid config keys"
        exclude_names = ["prefix", "username", "jump_username"]
        include_names = ["user", "jump_user"]
        return [
            f"{cls.prefix}_{x}"
            for x in [f.name for f in fields(cls) if f.name not in exclude_names]
            + include_names
        ]

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudUpcloud":
        "Create config from config parser's section"
        fmt = partial(_fmt_key, cls.prefix)

        warn_unknown_fields(
            [
                *ConfigCloudAzure.get_valid_config_parser_fields(),
                *ConfigCloudHetzner.get_valid_config_parser_fields(),
                *cls.get_valid_config_parser_fields(),
                *ConfigCloudVastAI.get_valid_config_parser_fields(),
            ],
            sec,
        )

        return cls(
            login=sec.get(fmt("login")),  # type: ignore
            password=sec.get(fmt("password")),  # type: ignore
            max_nodes=sec.getint(fmt("max_nodes")),  # type: ignore
            username=sec.get(fmt("user")),  # type: ignore
            priority=sec.getint(fmt("priority")),  # type: ignore
            idle_tolerance=sec.getint(fmt("idle_tolerance")),  # type: ignore
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


@define(frozen=True)
class ConfigCloudVastAI:
    """VastAI cloud configuration"""

    prefix = "vastai"
    api_key: str = field(validator=validators.instance_of(str))
    image: str = make_default_field("pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel")
    disk_gb: int = make_default_field(80, extra_validators=[validators.ge(1)])
    min_vram_mb: int = make_default_field(
        80 * 1024, extra_validators=[validators.ge(1024)]
    )
    num_gpus: int = make_default_field(1, extra_validators=[validators.ge(1)])
    max_price_per_hr: float = make_default_field(
        1.50, extra_validators=[validators.ge(0)]
    )
    max_nodes: int = make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = make_default_field("root")
    priority: int = make_default_field(0)
    idle_tolerance: int = make_default_field(300, extra_validators=[validators.ge(1)])
    onstart_script: str = make_default_field("")
    docker_options: str = make_default_field("")
    env: dict = make_default_field({})
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def get_valid_config_parser_fields(cls) -> Sequence[str]:
        "Returns a list of valid config keys"
        exclude_names = ["prefix", "username", "jump_username", "env"]
        include_names = ["user", "jump_user"]
        return [
            f"{cls.prefix}_{x}"
            for x in [f.name for f in fields(cls) if f.name not in exclude_names]
            + include_names
        ]

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudVastAI":
        "Create config from config parser's section"
        fmt = partial(_fmt_key, cls.prefix)

        warn_unknown_fields(
            [
                *ConfigCloudAzure.get_valid_config_parser_fields(),
                *ConfigCloudHetzner.get_valid_config_parser_fields(),
                *ConfigCloudUpcloud.get_valid_config_parser_fields(),
                *cls.get_valid_config_parser_fields(),
            ],
            sec,
        )

        return cls(
            api_key=sec.get(fmt("api_key")),  # type: ignore
            image=sec.get(fmt("image")),  # type: ignore
            disk_gb=sec.getint(fmt("disk_gb")),  # type: ignore
            min_vram_mb=sec.getint(fmt("min_vram_mb")),  # type: ignore
            num_gpus=sec.getint(fmt("num_gpus")),  # type: ignore
            max_price_per_hr=sec.getfloat(fmt("max_price_per_hr")),  # type: ignore
            max_nodes=sec.getint(fmt("max_nodes")),  # type: ignore
            username=sec.get(fmt("user")),  # type: ignore
            priority=sec.getint(fmt("priority")),  # type: ignore
            idle_tolerance=sec.getint(fmt("idle_tolerance")),  # type: ignore
            onstart_script=sec.get(fmt("onstart_script"), ""),
            docker_options=sec.get(fmt("docker_options"), ""),
            env={},  # Parse from config if needed
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


ConfigCloud = Union[
    ConfigCloudAzure, ConfigCloudHetzner, ConfigCloudUpcloud, ConfigCloudVastAI
]
