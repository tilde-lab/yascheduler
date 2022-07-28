"""Clouds helper utilities"""

import random
import string
from typing import TypeVar

from asyncssh.public_key import SSHKey

T = TypeVar("T")


def get_rnd_name(prefix: str) -> str:
    """Create random string with prefix"""
    return (
        prefix
        + "-"
        + "".join([random.choice(string.ascii_lowercase) for _ in range(8)])
    )


def get_key_name(key: SSHKey) -> str:
    """Get SSHKey's name"""
    key_filename = str(key.get_filename()) if key.get_filename() else None
    key_fingerprint = key.get_fingerprint()
    return key_filename or key.get_comment() or key_fingerprint
