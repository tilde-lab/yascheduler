#!/usr/bin/env python3

import json
from configparser import ConfigParser
from collections import UserDict
from itertools import chain
from pathlib import PurePath
from typing import Any, Callable, Mapping, Sequence

from attrs import asdict, define, field, validators, Attribute
from typing_extensions import Self

from .engine import Engine


def _value_serializer(int: type, field: Attribute, value: Any) -> Any:
    if isinstance(value, PurePath):
        return str(value)
    return value


@define
class EngineRepository(UserDict, Mapping[str, Engine]):
    engines_dir: PurePath = field()
    data: Mapping[str, Engine] = field(
        factory=dict,
        validator=[
            validators.deep_mapping(
                key_validator=validators.instance_of(str),
                value_validator=validators.instance_of(Engine),
            )
        ],
    )

    def __setitem__(self, _: str, __: Engine):
        raise NotImplementedError()

    def __delitem__(self, _: str):
        raise NotImplementedError()

    def __hash__(self) -> int:
        return hash(
            json.dumps(asdict(self, value_serializer=_value_serializer), sort_keys=True)
        )

    def values(self):
        return self.data.values()

    def filter(self, filter_func: Callable[[Engine], bool]) -> Self:
        new_data = dict(filter(lambda x: filter_func(x[1]), self.data.items()))
        return self.__class__(self.engines_dir, new_data)

    def filter_platforms(self, platforms: Sequence[str]) -> Self:
        return self.filter(lambda x: bool(set(x.platforms) & set(platforms)))

    def get_platform_packages(self) -> Sequence[str]:
        mapped = map(lambda x: x.platform_packages, self.values())
        return list(set(chain(*mapped)))

    @classmethod
    def from_config_parser(cls, cfg: ConfigParser, engines_dir: PurePath) -> Self:
        snames = filter(lambda x: x.startswith("engine."), cfg.sections())
        data = {}
        for sname in snames:
            engine = Engine.from_config_parser_section(cfg[sname], engines_dir)
            data[engine.name] = engine
        return cls(engines_dir, data)
