#!/usr/bin/env python3

import asyncio

from yascheduler.scheduler import Yascheduler


label = "test dummy calc"


async def main():
    yac = await Yascheduler.create()
    result = yac.create_new_task(
        label,
        {
            "1.input": "ABC" * 100,
            "2.input": "DEF" * 100,
            "3.input": "Q" * 1000,
            "webhook_url": "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
        },
        "dummy",
    )
    print(label)
    print(result)


asyncio.run(main())
