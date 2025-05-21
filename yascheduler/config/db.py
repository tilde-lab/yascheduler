#!/usr/bin/env python3
"""Database configuration"""

from collections.abc import Sequence
from configparser import SectionProxy

from attrs import define, fields

from .utils import make_default_field, warn_unknown_fields


@define(frozen=True)
class ConfigDb:
    """Database configuration"""

    user: str = make_default_field("yascheduler")
    password: str = make_default_field("password")
    database: str = make_default_field("database")
    host: str = make_default_field("localhost")
    port: int = make_default_field(5432)

    @classmethod
    def get_valid_config_parser_fields(cls) -> Sequence[str]:
        "Returns a list of valid config keys"
        return [f.name for f in fields(cls)]

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigDb":
        "Create config from config parser's section"
        warn_unknown_fields(cls.get_valid_config_parser_fields(), sec)
        return cls(
            sec.get("user"),  # type: ignore
            sec.get("password"),  # type: ignore
            sec.get("database"),  # type: ignore
            sec.get("host"),  # type: ignore
            sec.getint("port"),  # type: ignore
        )
