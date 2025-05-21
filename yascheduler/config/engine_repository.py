#!/usr/bin/env python3
"""Repository for Engines"""

import json
from collections import UserDict
from collections.abc import Callable, Sequence
from configparser import ConfigParser
from itertools import chain
from pathlib import PurePath
from typing import Any

from attrs import asdict, define, field, validators

from ..compat import Self
from .engine import Engine


def _value_serializer(_: type, __: Any, value: Any) -> Any:
    "Serialize PurePath as string"
    if isinstance(value, PurePath):
        return str(value)
    return value


@define
class EngineRepository(UserDict[str, Engine]):
    """Repository of Engines"""

    engines_dir: PurePath = field()
    data: dict[str, Engine] = field(
        factory=dict,
        validator=[
            validators.deep_mapping(
                key_validator=validators.instance_of(str),
                value_validator=validators.instance_of(Engine),
            )
        ],
    )

    def __setitem__(self, _: str, __: Engine) -> None:
        raise NotImplementedError()

    def __delitem__(self, _: str) -> None:
        raise NotImplementedError()

    def __hash__(self) -> int:
        return hash(
            json.dumps(asdict(self, value_serializer=_value_serializer), sort_keys=True)
        )

    def filter(self, filter_func: Callable[[Engine], bool]) -> Self:
        "Filter Engines by callable and return new Repository"
        new_data = dict(filter(lambda x: filter_func(x[1]), self.data.items()))
        return self.__class__(
            data=new_data,
            engines_dir=self.engines_dir,
        )

    def filter_platforms(self, platforms: Sequence[str]) -> Self:
        "Filter Engines by platforms and return new Repository"
        return self.filter(lambda x: bool(set(x.platforms) & set(platforms)))

    def get_platform_packages(self) -> list[str]:
        "Collect all platform pacakges from engines"
        mapped = map(lambda x: x.platform_packages, self.values())
        return list(set(chain(*mapped)))

    @classmethod
    def from_config_parser(cls, cfg: ConfigParser, engines_dir: PurePath) -> Self:
        "Create config from path or config file contents"
        snames = filter(lambda x: x.startswith("engine."), cfg.sections())
        data: dict[str, Engine] = {}
        for sname in snames:
            engine = Engine.from_config_parser_section(cfg[sname], engines_dir)
            data[engine.name] = engine
        return cls(engines_dir=engines_dir, data=data)
