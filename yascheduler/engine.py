#!/usr/bin/env python3

from dataclasses import dataclass, field
from typing import List
from configparser import SectionProxy


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


if __name__ == "__main__":
    import pprint
    from configparser import ConfigParser
    from yascheduler import CONFIG_FILE

    pp = pprint.PrettyPrinter()
    config = ConfigParser()
    config.read(CONFIG_FILE)
    for section_name in config.sections():
        if not section_name.startswith("engine."):
            continue
        engine = Engine.from_config(config[section_name])
        # pp.pprint(engine)
        pp.pprint(engine.__dict__)
