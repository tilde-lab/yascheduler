"""Time utils"""

import asyncio
from datetime import datetime
from time import sleep


def sleep_until(end: datetime) -> None:
    "Sleep until :end:"
    now = datetime.now()
    if now >= end:
        return
    sleep((end - now).total_seconds())


async def asleep_until(end: datetime) -> None:
    "Sleep until :end:"
    now = datetime.now()
    if now >= end:
        return
    await asyncio.sleep((end - now).total_seconds())
