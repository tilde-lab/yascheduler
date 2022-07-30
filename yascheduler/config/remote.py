#!/usr/bin/env python3
"""Remote configuration"""

from configparser import SectionProxy
from pathlib import PurePath
from typing import Optional

from attrs import define, field

from .utils import _make_default_field, opt_str_val


@define(frozen=True)
class ConfigRemote:
    """Local configuration"""

    data_dir: PurePath = _make_default_field(PurePath("./data"))
    tasks_dir: PurePath = _make_default_field(PurePath("./data/tasks"))
    engines_dir: PurePath = _make_default_field(PurePath("./data/engines"))
    username: str = _make_default_field("root")
    jump_username: Optional[str] = field(default=None, validator=opt_str_val)
    jump_host: Optional[str] = field(default=None, validator=opt_str_val)

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigRemote":
        "Create config from config parser's section"
        data_dir = PurePath(sec.get("data_dir", "./data"))
        return cls(
            data_dir=data_dir,
            engines_dir=PurePath(sec.get("engines_dir", str(data_dir / "engines"))),
            tasks_dir=PurePath(sec.get("tasks_dir", str(data_dir / "tasks"))),
            username=sec.get("user"),
            jump_username=sec.get("jump_user", None),
            jump_host=sec.get("jump_host", None),
        )
