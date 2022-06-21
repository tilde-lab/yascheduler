#!/usr/bin/env python3

from configparser import SectionProxy
from pathlib import PurePath
from typing import Mapping, Union

from attrs import define, field, validators

from .utils import _make_default_field


def _check_az_user(_: "ConfigCloudAzure", __, value: str):
    if value == "root":
        raise ValueError(f"Root user is forbidden on Azure")


@define(frozen=True)
class ConfigCloudAzure:
    prefix = "az"
    tenant_id: str = field(validator=validators.instance_of(str))
    client_id: str = field(validator=validators.instance_of(str))
    client_secret: str = field(validator=validators.instance_of(str))
    subscription_id: str = field(validator=validators.instance_of(str))
    infra_tmpl_path: PurePath = field(validator=validators.instance_of(PurePath))
    vm_tmpl_path: PurePath = field(validator=validators.instance_of(PurePath))
    infra_params: Mapping[str, Union[str, int, float]] = field(factory=dict)
    vm_params: Mapping[str, Union[str, int, float]] = field(factory=dict)
    resource_group: str = _make_default_field("YaScheduler-VM-rg")
    location: str = _make_default_field("westeurope")
    max_nodes: int = _make_default_field(10, extra_validators=[validators.ge(0)])
    username: str = _make_default_field(
        "yascheduler", extra_validators=[_check_az_user]
    )
    priority: int = _make_default_field(0)
    idle_tolerance: int = _make_default_field(120, extra_validators=[validators.ge(1)])

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudAzure":
        fmt = lambda x: f"{cls.prefix}_{x}"
        clouds_path = PurePath(__file__).parent / "clouds"

        def filter_by_prefix(prefix: str):
            filtered = filter(lambda x: x[0].startswith(fmt(prefix)), sec.items())
            return dict(map(lambda x: (x[0][len(fmt(prefix)) :], x[1]), filtered))

        return cls(
            tenant_id=sec.get(fmt("tenant_id")),
            client_id=sec.get(fmt("client_id")),
            client_secret=sec.get(fmt("client_secret")),
            subscription_id=sec.get(fmt("subscription_id")),
            resource_group=sec.get(fmt("resource_group")),
            location=sec.get(fmt("location")),
            infra_tmpl_path=PurePath(
                sec.get(
                    fmt("infra_tmpl_path"), str(clouds_path / "azure_infra_tmpl.json")
                )
            ),
            vm_tmpl_path=PurePath(
                sec.get(fmt("infra_tmpl_path"), str(clouds_path / "azure_vm_tmpl.json"))
            ),
            infra_params=filter_by_prefix("infra_params"),
            vm_params=filter_by_prefix("vm_params"),
            max_nodes=sec.getint(fmt("max_nodes")),
            username=sec.get(fmt("user")),
            priority=sec.getint(fmt("priority")),
            idle_tolerance=sec.getint(fmt("idle_tolerance")),
        )


@define(frozen=True)
class ConfigCloudHetzner:
    prefix = "hetzner"
    token: str = field(validator=validators.instance_of(str))
    max_nodes: int = _make_default_field(10, extra_validators=[validators.ge(1)])
    username: str = _make_default_field("root")
    priority: int = _make_default_field(0)
    server_type: str = _make_default_field("cx51")
    image_name: str = _make_default_field("debian-10")
    idle_tolerance: int = _make_default_field(60, extra_validators=[validators.ge(1)])

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudHetzner":
        fmt = lambda x: f"{cls.prefix}_{x}"
        return cls(
            token=sec.get(fmt("token")),
            max_nodes=sec.getint(fmt("max_nodes")),
            username=sec.get(fmt("user")),
            server_type=sec.get(fmt("server_type")),
            image_name=sec.get(fmt("image_name")),
            priority=sec.getint(fmt("priority")),
            idle_tolerance=sec.getint(fmt("idle_tolerance")),
        )


@define(frozen=True)
class ConfigCloudUpcloud:
    prefix = "upcloud"
    login: str = field(validator=validators.instance_of(str))
    password: str = field(validator=validators.instance_of(str))
    max_nodes: int = _make_default_field(10, extra_validators=[validators.ge(1)])
    username: str = _make_default_field("root")
    priority: int = _make_default_field(0)
    idle_tolerance: int = _make_default_field(60, extra_validators=[validators.ge(1)])

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigCloudUpcloud":
        fmt = lambda x: f"{cls.prefix}_{x}"
        return cls(
            login=sec.get(fmt("login")),
            password=sec.get(fmt("password")),
            max_nodes=sec.getint(fmt("max_nodes")),
            username=sec.get(fmt("user")),
            priority=sec.getint(fmt("priority")),
            idle_tolerance=sec.getint(fmt("idle_tolerance")),
        )


ConfigCloud = Union[ConfigCloudAzure, ConfigCloudHetzner, ConfigCloudUpcloud]
