"""Clouds helper utilities"""

import random
import string
from pathlib import PurePath
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
    fname_opt = key.get_filename()
    key_filename = fname_opt.decode("utf-8") if fname_opt else None
    if key_filename:
        key_filename = PurePath(key_filename).name
    key_fingerprint = key.get_fingerprint("md5").split(":", maxsplit=1)[1]
    return key_filename or key.get_comment() or key_fingerprint
