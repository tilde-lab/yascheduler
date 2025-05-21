#!/usr/bin/env python3
"""Config helper utilities"""

import warnings
from collections.abc import Sequence
from configparser import SectionProxy
from typing import Any, Callable, Optional, TypeVar

from attrs import Attribute, converters, field, validators

opt_str_val = validators.optional(validators.instance_of(str))


class ConfigWarning(Warning):
    "Warning about config"


_T = TypeVar("_T")


def make_default_field(
    default: _T,
    extra_validators: Optional[
        Sequence[Callable[[Any, "Attribute[_T]", _T], Any]]
    ] = None,
) -> _T:
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
            3,
        )
