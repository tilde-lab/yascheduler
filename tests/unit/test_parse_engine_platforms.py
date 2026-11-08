# region MODULE_CONTRACT
# PURPOSE: Unit tests for versioned-platform normalization in parse_engine_section.
# SCOPE: _normalize_platforms helper — versioned tags stripped + warning + dedup.
# KEYWORDS: platforms, normalize, versioned, debian, windows, dedup, warning
# endregion MODULE_CONTRACT

from __future__ import annotations

import logging
from configparser import ConfigParser
from pathlib import PurePath

import pytest

from yascheduler.entrypoints.config_parser import (
    _normalize_platforms,
    parse_engine_section,
)


def _engine_cfg(platforms_line: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg.read_string(
        "[engine.t]\n"
        "spawn=echo hi\n"
        "check_pname=hi\n"
        "input_files=in.txt\n"
        "output_files=out.txt\n"
        f"platforms={platforms_line}\n",
    )
    return cfg


def test_normalize_strips_debian_version() -> None:
    assert _normalize_platforms(("debian-12",), "t") == ("debian",)


def test_normalize_strips_windows_version() -> None:
    assert _normalize_platforms(("windows-11",), "t") == ("windows",)


def test_normalize_dedups_collisions() -> None:
    """debian-10 debian-11 collapse to a single debian (no duplicate)."""
    assert _normalize_platforms(("debian-10", "debian-11"), "t") == ("debian",)


def test_normalize_preserves_unversioned_and_unknown() -> None:
    assert _normalize_platforms(("linux", "debian", "mY-cOoL-OS"), "t") == (
        "linux",
        "debian",
        "mY-cOoL-OS",
    )


def test_normalize_preserves_order() -> None:
    assert _normalize_platforms(("windows-10", "debian-12"), "t") == (
        "windows",
        "debian",
    )


def test_normalize_warns_on_rewrite(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="yascheduler.entrypoints.config_parser",
    ):
        _normalize_platforms(("debian-12",), "myengine")

    matching = [r for r in caplog.records if "versioned platform tag" in r.getMessage()]
    assert matching, "expected a warning naming the versioned tag"
    msg = matching[0].getMessage()
    assert "myengine" in msg
    assert "'debian-12'" in msg
    assert "'debian'" in msg


def test_normalize_does_not_warn_for_unversioned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="yascheduler.entrypoints.config_parser",
    ):
        _normalize_platforms(("linux", "debian"), "t")

    assert not [r for r in caplog.records if "versioned platform tag" in r.getMessage()]


def test_parse_engine_section_normalizes_versioned_platforms() -> None:
    """parse_engine_section applies normalization end-to-end."""
    engine = parse_engine_section(
        _engine_cfg("debian-10 debian-11")["engine.t"], PurePath()
    )
    assert engine.platforms == ("debian",)


def test_parse_engine_section_normalizes_windows_versioned() -> None:
    engine = parse_engine_section(_engine_cfg("windows-11")["engine.t"], PurePath())
    assert engine.platforms == ("windows",)


def test_parse_engine_section_reads_exclude_nodes() -> None:
    """The exclude_nodes INI key lands in the engine's frozenset."""
    cfg = ConfigParser()
    cfg.read_string(
        "[engine.t]\n"
        "spawn=echo hi\n"
        "check_pname=hi\n"
        "input_files=in.txt\n"
        "output_files=out.txt\n"
        "platforms=debian\n"
        "exclude_nodes=10.0.0.7 192.168.1.9\n",
    )
    engine = parse_engine_section(cfg["engine.t"], PurePath())
    assert engine.exclude_nodes == frozenset({"10.0.0.7", "192.168.1.9"})


def test_parse_engine_section_exclude_nodes_defaults_empty() -> None:
    """Without the key the engine carries an empty exclude_nodes frozenset."""
    engine = parse_engine_section(_engine_cfg("debian")["engine.t"], PurePath())
    assert engine.exclude_nodes == frozenset()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
