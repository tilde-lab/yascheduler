#!/usr/bin/env python3
"""Cloud configurations"""

from configparser import SectionProxy
from functools import partial
from typing import Optional, Union

from attrs import define, field, validators
from typing_extensions import Self

from .utils import _make_default_field, opt_str_val


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
    resource_group: str = _make_default_field("yascheduler-rg")
    location: str = _make_default_field("westeurope")
    vnet: str = _make_default_field("yascheduler-vnet")
    subnet: str = _make_default_field("yascheduler-subnet")
    nsg: str = _make_default_field("yascheduler-nsg")
    vm_image: AzureImageReference = _make_default_field(AzureImageReference())
    vm_size: str = _make_default_field("Standard_B1s")
    max_nodes: int = _make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = _make_default_field(
        "yascheduler", extra_validators=[_check_az_user]
    )
    priority: int = _make_default_field(0)
    idle_tolerance: int = _make_default_field(300, extra_validators=[validators.ge(1)])
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudAzure":
        "Create config from config parser's section"

        fmt = partial(_fmt_key, cls.prefix)

        vm_image = sec.get(fmt("image"))
        image_ref = None
        if vm_image:
            image_ref = AzureImageReference.from_urn(vm_image)

        return cls(
            tenant_id=sec.get(fmt("tenant_id")),
            client_id=sec.get(fmt("client_id")),
            client_secret=sec.get(fmt("client_secret")),
            subscription_id=sec.get(fmt("subscription_id")),
            resource_group=sec.get(fmt("resource_group")),
            location=sec.get(fmt("location")),
            vnet=sec.get(fmt("vnet")),
            subnet=sec.get(fmt("subnet")),
            nsg=sec.get(fmt("nsg")),
            vm_image=image_ref or AzureImageReference(),
            vm_size=sec.get(fmt("size")),
            max_nodes=sec.getint(fmt("max_nodes")),
            username=sec.get(fmt("user")),
            priority=sec.getint(fmt("priority")),
            idle_tolerance=sec.getint(fmt("idle_tolerance")),
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


@define(frozen=True)
class ConfigCloudHetzner:
    """Hetzner cloud configuration"""

    prefix = "hetzner"
    token: str = field(validator=validators.instance_of(str))
    max_nodes: int = _make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = _make_default_field("root")
    priority: int = _make_default_field(0)
    server_type: str = _make_default_field("cx51")
    image_name: str = _make_default_field("debian-10")
    idle_tolerance: int = _make_default_field(120, extra_validators=[validators.ge(1)])
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudHetzner":
        "Create config from config parser's section"
        fmt = partial(_fmt_key, cls.prefix)
        return cls(
            token=sec.get(fmt("token")),
            max_nodes=sec.getint(fmt("max_nodes")),
            username=sec.get(fmt("user")),
            server_type=sec.get(fmt("server_type")),
            image_name=sec.get(fmt("image_name")),
            priority=sec.getint(fmt("priority")),
            idle_tolerance=sec.getint(fmt("idle_tolerance")),
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


@define(frozen=True)
class ConfigCloudUpcloud:
    """Upcloud cloud configuration"""

    prefix = "upcloud"
    login: str = field(validator=validators.instance_of(str))
    password: str = field(validator=validators.instance_of(str))
    max_nodes: int = _make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = _make_default_field("root")
    priority: int = _make_default_field(0)
    idle_tolerance: int = _make_default_field(120, extra_validators=[validators.ge(1)])
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudUpcloud":
        "Create config from config parser's section"
        fmt = partial(_fmt_key, cls.prefix)
        return cls(
            login=sec.get(fmt("login")),
            password=sec.get(fmt("password")),
            max_nodes=sec.getint(fmt("max_nodes")),
            username=sec.get(fmt("user")),
            priority=sec.getint(fmt("priority")),
            idle_tolerance=sec.getint(fmt("idle_tolerance")),
            jump_username=sec.get(fmt("jump_user"), None),
            jump_host=sec.get(fmt("jump_host"), None),
        )


ConfigCloud = Union[ConfigCloudAzure, ConfigCloudHetzner, ConfigCloudUpcloud]
