#!/usr/bin/env python3

import asyncio
import string
import random
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, TypeVar

from asyncssh.public_key import SSHKey as SSHKey

T = TypeVar("T")


def get_rnd_name(prefix: str) -> str:
    return (
        prefix
        + "-"
        + "".join([random.choice(string.ascii_lowercase) for _ in range(8)])
    )


def get_key_name(key: SSHKey) -> str:
    key_filename = str(key.get_filename()) if key.get_filename() else None
    key_fingerprint = key.get_fingerprint()
    return key_filename or key.get_comment() or key_fingerprint
