#!/usr/bin/env python3

from attrs import define, field, validators, Attribute
from configparser import SectionProxy
from pathlib import PurePath
from typing import Optional, Sequence, Union, Tuple

from .utils import _make_default_field


def _check_spawn(instance: "Engine", _, value: str):
    try:
        value.format(task_path="", engine_path="", ncpus="")
    except KeyError as e:
        msg = (
            "Engine {name} has unknown template placeholder "
            "`{placeholder}` in *spawn* command"
        )
        raise ValueError(msg.format(name=instance.name, placeholder=e.args[0]))


def _check_check_(instance: "Engine", attribute: Attribute, value: Optional[str]):
    if (attribute.name == "check_cmd" and not value and not instance.check_pname) or (
        attribute.name == "check_pname" and not value and not instance.check_cmd
    ):
        raise ValueError(
            "Engine %s has no *check_cmd* or *check_pname* set" % instance.name
        )


def _check_at_least_one_elem(
    instance: "Engine", attribute: Attribute, value: Optional[Sequence]
):
    if not value or len(value) < 1:
        raise ValueError(
            "Engine %s has no *%s* config set" % (instance.name, attribute.name)
        )


@define(frozen=True)
class LocalFilesDeploy:
    files: Tuple[PurePath] = field(factory=tuple)


@define(frozen=True)
class LocalArchiveDeploy:
    file: PurePath


@define(frozen=True)
class RemoteArchiveDeploy:
    url: str


Deploy = Union[
    LocalFilesDeploy,
    LocalArchiveDeploy,
    RemoteArchiveDeploy,
]


@define(frozen=True)
class Engine:
    name: str = field(validator=[validators.instance_of(str)])
    spawn: str = field(validator=[validators.instance_of(str), _check_spawn])
    check_cmd: Optional[str] = field(
        validator=[validators.optional(validators.instance_of(str)), _check_check_]
    )
    check_pname: Optional[str] = field(
        validator=[validators.optional(validators.instance_of(str)), _check_check_]
    )
    deployable: Tuple[Deploy, ...] = field(factory=tuple)
    input_files: Tuple[str, ...] = field(
        factory=tuple,
        validator=[
            validators.deep_iterable(member_validator=validators.instance_of(str)),
            _check_at_least_one_elem,
        ],
    )
    output_files: Tuple[str, ...] = field(
        factory=tuple,
        validator=[
            validators.deep_iterable(member_validator=validators.instance_of(str)),
            _check_at_least_one_elem,
        ],
    )
    platforms: Tuple[str, ...] = field(
        factory=tuple,
        validator=[
            validators.deep_iterable(member_validator=validators.instance_of(str))
        ],
    )
    platform_packages: Tuple[str, ...] = field(
        factory=tuple,
        validator=[
            validators.deep_iterable(member_validator=validators.instance_of(str))
        ],
    )
    check_cmd_code: int = _make_default_field(0)
    sleep_interval: int = _make_default_field(10)

    @classmethod
    def from_config_parser_section(
        cls, sec: SectionProxy, engines_dir: PurePath
    ) -> "Engine":
        def gettuple(key: str) -> Tuple[str]:
            return tuple(
                [x.strip() for x in filter(None, sec.get(key, fallback="").split())]
            )

        name = sec.name[7:]
        engine_dir = engines_dir / name

        deployable: Sequence[Deploy] = []
        deploy_local_files = [
            engine_dir / x.strip() for x in gettuple("deploy_local_files")
        ]
        if deploy_local_files:
            deployable.append(LocalFilesDeploy(files=tuple(deploy_local_files)))
        deploy_local_archive = sec.get("deploy_local_archive", None)
        if deploy_local_archive:
            d = LocalArchiveDeploy(file=engine_dir / deploy_local_archive)
            deployable.append(d)
        deploy_remote_archive = sec.get("deploy_remote_archive", None)
        if deploy_remote_archive:
            d = RemoteArchiveDeploy(url=deploy_remote_archive)
            deployable.append(d)

        return cls(
            name=name,
            deployable=tuple(deployable),
            spawn=sec.get("spawn"),
            check_cmd=sec.get("check_cmd"),
            check_cmd_code=sec.getint("check_cmd_code"),
            check_pname=sec.get("check_pname"),
            input_files=gettuple("input_files"),
            output_files=gettuple("output_files"),
            sleep_interval=sec.getint("sleep_interval"),
            platforms=gettuple("platforms") or ["debian-11"],
            platform_packages=gettuple("platform_packages"),
        )
