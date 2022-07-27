#!/usr/bin/env python3

from configparser import SectionProxy

from attrs import define

from .utils import _make_default_field


@define(frozen=True)
class ConfigDb:
    user: str = _make_default_field("yascheduler")
    password: str = _make_default_field("password")
    database: str = _make_default_field("database")
    host: str = _make_default_field("localhost")
    port: int = _make_default_field(5432)

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigDb":
        return cls(
            sec.get("user"),
            sec.get("password"),
            sec.get("database"),
            sec.get("host"),
            sec.getint("port"),
        )
