#!/usr/bin/env python

import logging
import time
import sys
from configparser import ConfigParser
from yascheduler import CONFIG_FILE
from yascheduler.clouds import CloudAPIManager
from yascheduler.scheduler import Yascheduler

logging.basicConfig(level=logging.INFO)

config = ConfigParser()
config.read(CONFIG_FILE)
config.set("local", "allocator_threads", "0")
config.set("local", "deallocator_threads", "1")

ip = sys.argv[2]

yac = Yascheduler(config)
clouds = CloudAPIManager(config)
clouds.yascheduler = yac
clouds.initialize()
clouds.deallocate([ip])
time.sleep(10)
clouds.stop()
