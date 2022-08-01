#!/usr/bin/env python3
"""Engine configuration"""

from configparser import SectionProxy
from pathlib import PurePath
from typing import Optional, Sequence, Tuple, Union

from attrs import Attribute, define, field, validators

from .utils import _make_default_field


def _check_spawn(instance: "Engine", _, value: str):
    try:
        value.format(task_path="", engine_path="", ncpus="")
    except KeyError as err:
        msg = (
            "Engine {name} has unknown template placeholder "
            "`{placeholder}` in *spawn* command"
        )
        raise ValueError(
            msg.format(name=instance.name, placeholder=err.args[0])
        ) from err


def _check_check_(instance: "Engine", attribute: Attribute, value: Optional[str]):
    no_check_cmd_curr = attribute.name == "check_cmd" and not value
    no_check_pname_curr = attribute.name == "check_pname" and not value
    if (no_check_cmd_curr and not instance.check_pname) or (
        no_check_pname_curr and not instance.check_cmd
    ):
        raise ValueError(
            f"Engine {instance.name} has no *check_cmd* or *check_pname* set"
        )


def _check_at_least_one_elem(
    instance: "Engine", attribute: Attribute, value: Optional[Sequence]
):
    if not value or len(value) < 1:
        raise ValueError(f"Engine {instance.name} has no *{attribute.name}* config set")


@define(frozen=True)
class LocalFilesDeploy:
    "Deploy local files configuration"
    files: Tuple[PurePath] = field(factory=tuple)


@define(frozen=True)
class LocalArchiveDeploy:
    "Deploy local archive configuration"
    file: PurePath


@define(frozen=True)
class RemoteArchiveDeploy:
    "Deploy remote archive configuration"
    url: str


Deploy = Union[
    LocalFilesDeploy,
    LocalArchiveDeploy,
    RemoteArchiveDeploy,
]


@define(frozen=True)
class Engine:
    """Engine configuration"""

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
        "Create config from config parser's section"

        def gettuple(key: str) -> Tuple[str]:
            return tuple(
                x.strip() for x in filter(None, sec.get(key, fallback="").split())
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
            deploy = LocalArchiveDeploy(file=engine_dir / deploy_local_archive)
            deployable.append(deploy)
        deploy_remote_archive = sec.get("deploy_remote_archive", None)
        if deploy_remote_archive:
            deploy = RemoteArchiveDeploy(url=deploy_remote_archive)
            deployable.append(deploy)

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
            platforms=gettuple("platforms"),
            platform_packages=gettuple("platform_packages"),
        )
