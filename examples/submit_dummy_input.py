#!/usr/bin/env python3

from configparser import ConfigParser

from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler


label = "test dummy calc"

config = ConfigParser()
config.read(CONFIG_FILE)
yac = Yascheduler(config)

result = yac.queue_submit_task(
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
