#!/usr/bin/env python3
"""Local configuration"""

from configparser import SectionProxy
from pathlib import Path, PurePath
from typing import Optional, Sequence

from attrs import define, field, validators

from .utils import _make_default_field


@define(frozen=True)
class ConfigLocal:
    """Local configuration"""

    data_dir: Path = _make_default_field(Path("./data"))
    tasks_dir: Path = _make_default_field(Path("./data/tasks"))
    engines_dir: Path = _make_default_field(Path("./data/engines"))
    keys_dir: Path = _make_default_field(Path("./data/keys"))
    webhook_url: Optional[str] = field(default=None)
    webhook_reqs_limit: int = _make_default_field(
        5, extra_validators=[validators.ge(1)]
    )
    conn_machine_limit: int = _make_default_field(
        10, extra_validators=[validators.ge(1)]
    )
    conn_machine_pending: int = _make_default_field(
        10, extra_validators=[validators.ge(1)]
    )
    allocate_limit: int = _make_default_field(20, extra_validators=[validators.ge(1)])
    allocate_pending: int = _make_default_field(1, extra_validators=[validators.ge(1)])
    consume_limit: int = _make_default_field(20, extra_validators=[validators.ge(1)])
    consume_pending: int = _make_default_field(1, extra_validators=[validators.ge(1)])
    deallocate_limit: int = _make_default_field(5, extra_validators=[validators.ge(1)])
    deallocate_pending: int = _make_default_field(
        1, extra_validators=[validators.ge(1)]
    )

    def get_private_keys(self) -> Sequence[PurePath]:
        "List private key file paths"
        filepaths = filter(lambda x: x.is_file(), self.keys_dir.iterdir())
        return list(filepaths)

    @classmethod
    def from_config_parser_section(cls, sec: SectionProxy) -> "ConfigLocal":
        "Create config from config parser's section"
        data_dir = Path(sec.get("data_dir", "./data")).resolve()
        return ConfigLocal(
            data_dir,
            tasks_dir=Path(sec.get("tasks_dir", str(data_dir / "tasks"))).resolve(),
            engines_dir=Path(
                sec.get("engines_dir", str(data_dir / "engines"))
            ).resolve(),
            keys_dir=Path(sec.get("keys_dir", str(data_dir / "keys"))).resolve(),
            webhook_reqs_limit=sec.getint("webhook_reqs_limit"),
            webhook_url=sec.get("webhook_url"),
            conn_machine_limit=sec.getint("conn_machine_limit"),
            conn_machine_pending=sec.getint("conn_machine_pending"),
            allocate_limit=sec.getint("allocate_limit"),
            allocate_pending=sec.getint("allocate_pending"),
            consume_limit=sec.getint("consume_limit"),
            consume_pending=sec.getint("consume_pending"),
            deallocate_limit=sec.getint("deallocate_limit"),
            deallocate_pending=sec.getint("deallocate_pending"),
        )
