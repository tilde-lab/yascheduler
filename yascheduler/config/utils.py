#!/usr/bin/env python3

from typing import Optional, Sequence

from attrs import converters, field, validators


def _make_default_field(default, extra_validators: Optional[Sequence] = None):
    return field(
        default=default,
        converter=converters.default_if_none(default=default),
        validator=[validators.instance_of(type(default)), *(extra_validators or [])],
    )
