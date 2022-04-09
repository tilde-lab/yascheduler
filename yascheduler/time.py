#!/usr/bin/env python3

from datetime import datetime
from time import sleep


def sleep_until(end: datetime) -> None:
    "Sleep until :end:"
    now = datetime.now()
    if now >= end:
        return
    sleep((end - now).total_seconds())
