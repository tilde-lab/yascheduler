#!/usr/bin/env python3

from configparser import SectionProxy
from pathlib import PurePath

from attrs import define

from .utils import _make_default_field


@define(frozen=True)
class ConfigRemote:
    data_dir: PurePath = _make_default_field(PurePath("./data"))
    tasks_dir: PurePath = _make_default_field(PurePath("./data/tasks"))
    engines_dir: PurePath = _make_default_field(PurePath("./data/engines"))
    username: str = _make_default_field("root")

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigRemote":
        data_dir = PurePath(sec.get("data_dir", "./data"))
        return cls(
            data_dir,
            engines_dir=PurePath(sec.get("engines_dir", str(data_dir / "engines"))),
            tasks_dir=PurePath(sec.get("tasks_dir", str(data_dir / "tasks"))),
            username=sec.get("user"),
        )
