#!/usr/bin/env python

import sys
from configparser import ConfigParser
from yascheduler import has_node, remove_node, CONFIG_FILE
from yascheduler.clouds import load_cloudapi

name, ip = sys.argv[1], sys.argv[2]

config = ConfigParser()
config.read(CONFIG_FILE)

cloudapi = load_cloudapi(name)(config)

assert has_node(config, ip)
remove_node(config, ip)
cloudapi.delete_node(ip)