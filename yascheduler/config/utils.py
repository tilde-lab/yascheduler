#!/usr/bin/env python3
"""Config helper utilities"""

import warnings
from configparser import SectionProxy
from typing import Optional, Sequence

from attrs import converters, field, validators

opt_str_val = validators.optional(validators.instance_of(str))


class ConfigWarning(Warning):
    "Warning about config"


def _make_default_field(default, extra_validators: Optional[Sequence] = None):
    return field(
        default=default,
        converter=converters.default_if_none(default=default),
        validator=[validators.instance_of(type(default)), *(extra_validators or [])],
    )


def warn_unknown_fields(known_fields: Sequence[str], sec: SectionProxy) -> None:
    unknown_fields = list(set(sec.keys()) - set(known_fields))
    if unknown_fields:
        warnings.warn(
            f"Config section {sec.name} unknown fields: {', '.join(unknown_fields)}",
            ConfigWarning,
        )
