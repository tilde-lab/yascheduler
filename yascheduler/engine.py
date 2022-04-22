#!/usr/bin/env python3

from collections import UserDict
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Callable, Dict, List, Union
from configparser import SectionProxy


@dataclass
class LocalFilesDeploy:
    files: List[Path]


@dataclass
class LocalArchiveDeploy:
    filename: Path


@dataclass
class RemoteArchiveDeploy:
    url: str


Deploy = Union[
    LocalFilesDeploy,
    LocalArchiveDeploy,
    RemoteArchiveDeploy,
]


@dataclass
class Engine:
    name: str

    # task i/o
    input_files: List[str]
    output_files: List[str]

    deployable: List[Deploy]
    spawn: str

    # TODO: this is stupid - change to pid tracking
    check: str
    run_marker: str

    platforms: List[str]
    platform_packages: List[str] = field(default_factory=lambda: [])

    # TODO: not used actually
    sleep_interval: int = 1

    def __post_init__(self):
        assert self.spawn.startswith("nohup"), "spawn command should start with `nohup`"

    @classmethod
    def from_config(cls, cfg: SectionProxy):
        def getlist(key: str) -> List[str]:
            return [x.strip() for x in filter(None, cfg.get(key, fallback="").split())]

        deployable: List[Deploy] = []
        deploy_local_files = [Path(x.strip()) for x in getlist("deploy_local_files")]
        if deploy_local_files:
            deployable.append(LocalFilesDeploy(files=deploy_local_files))
        deploy_local_archive = cfg.get("deploy_local_archive", None)
        if deploy_local_archive:
            d = LocalArchiveDeploy(filename=Path(deploy_local_archive))
            deployable.append(d)
        deploy_remote_archive = cfg.get("deploy_remote_archive", None)
        if deploy_remote_archive:
            d = RemoteArchiveDeploy(url=deploy_remote_archive)
            deployable.append(d)

        spawn = cfg.get("spawn")
        assert spawn, "Engine %s has no *spawn* config set" % cfg.name

        check = cfg.get("check")
        assert check, "Engine %s has no *check* config set" % cfg.name

        run_marker = cfg.get("run_marker")
        assert run_marker, "Engine %s has no *run_maker* config set" % cfg.name

        assert "input_files" in cfg.keys(), (
            "Engine %s has no *input_files* config set" % cfg.name
        )
        input_files = getlist("input_files")

        assert "output_files" in cfg.keys(), (
            "Engine %s has no *input_files* config set" % cfg.name
        )
        output_files = getlist("output_files")

        platforms = getlist("platforms") or ["debian-10"]
        platform_packages = getlist("platform_packages")

        return cls(
            name=cfg.name[7:],
            deployable=deployable,
            spawn=spawn,
            check=check,
            run_marker=run_marker,
            input_files=input_files,
            output_files=output_files,
            sleep_interval=cfg.getint("sleep_interval", cls.sleep_interval),
            platforms=platforms,
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

    def filter(self, filter_func: Callable[[Engine], bool]) -> "EngineRepository":
        repo = EngineRepository()
        for k, v in self.items():
            if filter_func(v):
                repo[k] = v
        return repo

    def filter_platforms(self, platforms: List[str]) -> "EngineRepository":
        return self.filter(lambda x: bool(set(x.platforms) & set(platforms)))

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
