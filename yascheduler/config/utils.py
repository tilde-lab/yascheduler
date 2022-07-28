#!/usr/bin/env python3
"""Config helper utilities"""

from typing import Optional, Sequence

from attrs import converters, field, validators

opt_str_val = validators.optional(validators.instance_of(str))


def _make_default_field(default, extra_validators: Optional[Sequence] = None):
    return field(
        default=default,
        converter=converters.default_if_none(default=default),
        validator=[validators.instance_of(type(default)), *(extra_validators or [])],
    )
