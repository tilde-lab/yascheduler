#!/usr/bin/env python3
"""Main config module"""

from configparser import ConfigParser
from pathlib import PurePath
from typing import Sequence, Union

from attrs import define, field, validators

from .cloud import ConfigCloud, ConfigCloudAzure, ConfigCloudHetzner, ConfigCloudUpcloud
from .db import ConfigDb
from .engine_repository import EngineRepository
from .local import ConfigLocal
from .remote import ConfigRemote


@define(frozen=True)
class Config:
    """Main config module"""

    db: ConfigDb = field(validator=[validators.instance_of(ConfigDb)])
    local: ConfigLocal = field(validator=[validators.instance_of(ConfigLocal)])
    remote: ConfigRemote = field(validator=[validators.instance_of(ConfigRemote)])
    clouds: Sequence[ConfigCloud]
    engines: EngineRepository = field(
        validator=[validators.instance_of(EngineRepository)]
    )

    @classmethod
    def from_config_parser(cls, files: Union[str, bytes, PurePath]) -> "Config":
        "Create Config from path or config file contents"
        config = ConfigParser()
        config.read(files)

        for sec_name in ["db", "local", "remote", "clouds"]:
            if not config.has_section(sec_name):
                config.add_section(sec_name)

        local = ConfigLocal.from_config_parser_section(config["local"])
        remote = ConfigRemote.from_config_parser_section(config["remote"])

        # config prefixes
        cloud_prefixes = set(map(lambda x: x.split("_")[0], config.options("clouds")))
        # inherit username
        for prefix in cloud_prefixes:
            key = f"{prefix}_user"
            if key not in config.options("clouds"):
                config["clouds"][key] = remote.username
        # available cloud config models
        cloud_variants = (
            ConfigCloudAzure,
            ConfigCloudHetzner,
            ConfigCloudUpcloud,
        )
        # intersection
        cloud_variants_match = filter(
            lambda x: x.prefix in cloud_prefixes, cloud_variants
        )
        # instantiate
        clouds = map(
            lambda x: x.from_config_parser_section(config["clouds"]),
            cloud_variants_match,
        )

        return cls(
            db=ConfigDb.from_config_parser_section(config["db"]),
            local=local,
            remote=remote,
            clouds=list(clouds),
            engines=EngineRepository.from_config_parser(config, local.engines_dir),
        )
