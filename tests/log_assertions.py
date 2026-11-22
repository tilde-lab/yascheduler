# region MODULE_CONTRACT
# PURPOSE: Shared helpers for log-driven test assertions against the stdlib `debug(msg, extra=...)` trace contract.
# SCOPE: extra_fields(rec) — reconstruct the structured trace fields dict from a LogRecord by diffing its attributes against the introspection-derived native LogRecord attribute set, minus the `message` key a Formatter writes as a side effect.
# KEYWORDS: log-driven assertions, extra_fields, LogRecord structured data
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yascheduler.shared.log import _NATIVE_KEYS

if TYPE_CHECKING:
    import logging

# _NATIVE_KEYS is static; `message` is not native but appears whenever a handler
# formats the record, so it must not leak into the structured-field view.
_NON_FIELD_KEYS = _NATIVE_KEYS | {"message"}


def extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The fields the emitter passed as `extra=`, without formatter artifacts."""
    return {k: getattr(record, k) for k in record.__dict__ if k not in _NON_FIELD_KEYS}
