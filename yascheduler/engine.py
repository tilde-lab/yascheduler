#!/usr/bin/env python3

from collections import UserDict
from dataclasses import dataclass, field
from itertools import chain
from typing import Callable, Dict, List, TypeVar
from configparser import SectionProxy

T = TypeVar("T")


@dataclass
class Engine:
    name: str

    deployable: str
    spawn: str

    input_files: List[str]
    output_files: List[str]

    # TODO: this is stupid - change to pid tracking
    check: str
    run_marker: str

    platform: str = "debian"
    platform_packages: List[str] = field(default_factory=lambda: [])

    # TODO: not used actually
    sleep_interval: int = 1

    def __post_init__(self):
        assert self.spawn.startswith(
            "nohup"
        ), "spawn command should start with `nohup`"

    @classmethod
    def from_config(cls, cfg: SectionProxy):
        deployable = cfg.get("deployable")
        assert deployable and len(deployable.strip()), (
            "Engine %s has no *deployable* config set, cloud usage is impossible"
            % cfg.name
        )

        spawn = cfg.get("spawn")
        assert spawn

        check = cfg.get("check")
        assert check

        run_marker = cfg.get("run_marker")
        assert run_marker

        assert "input_files" in cfg.keys()
        input_files = [
            x.strip() for x in filter(None, cfg.get("input_files").split())
        ]
        assert "output_files" in cfg.keys()
        output_files = [
            x.strip() for x in filter(None, cfg.get("output_files").split())
        ]

        platform_packages = [
            x.strip()
            for x in filter(None, cfg.get("platform_packages", "").split())
        ]
        return cls(
            name=cfg.name[7:],
            deployable=deployable,
            spawn=spawn,
            check=check,
            run_marker=run_marker,
            input_files=input_files,
            output_files=output_files,
            sleep_interval=cfg.getint("sleep_interval", cls.sleep_interval),
            platform=cfg.get("platform", cls.platform),
            platform_packages=platform_packages,
        )


class EngineRepository(UserDict, Dict[str, Engine]):
    def __setitem__(self, key: str, value: Engine):
        if not isinstance(key, str):
            raise TypeError(
                f"Invalid type for dictionary key: "
                f'expected "str", got "{type(key).__name__}"'
            )
        if not isinstance(value, Engine):
            raise TypeError(
                f"Invalid type for dictionary value: "
                f'expected "Engine", got "{type(value).__name__}"'
            )
        return super().__setitem__(key, value)

    def filter(
        self, filter_func: Callable[[Engine], bool]
    ) -> "EngineRepository":
        repo = EngineRepository()
        for k, v in self.items():
            if filter_func(v):
                repo[k] = v
        return repo

    def filter_platforms(self, platforms: List[str]) -> "EngineRepository":
        return self.filter(lambda x: x.platform in platforms)

    def get_platform_packages(self) -> List[str]:
        mapped = map(lambda x: x.platform_packages, self.values())
        return list(set(chain(*mapped)))


if __name__ == "__main__":
    import pprint
    from configparser import ConfigParser
    from yascheduler import CONFIG_FILE

    pp = pprint.PrettyPrinter()
    config = ConfigParser()
    config.read(CONFIG_FILE)

    engines = EngineRepository()
    for section_name in config.sections():
        if not section_name.startswith("engine."):
            continue
        engine = Engine.from_config(config[section_name])
        engines[engine.name] = engine
    pp.pprint(engines)
