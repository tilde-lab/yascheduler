#!/usr/bin/env python

import sys
import random

from configparser import ConfigParser
from yascheduler import connect_db, provision_node, add_node, has_node, CONFIG_FILE
from yascheduler.clouds import CloudAPIManager


config = ConfigParser()
config.read(CONFIG_FILE)
clouds = CloudAPIManager(config)
connection, cursor = connect_db(config)

active_providers = list(clouds.apis.keys())
used_providers = []

cursor.execute('SELECT cloud, COUNT(cloud) FROM yascheduler_nodes WHERE cloud IS NOT NULL GROUP BY cloud;')
for row in cursor.fetchall():
    cloudapi = clouds.apis.get(row[0])
    if not cloudapi:
        continue
    if row[1] >= cloudapi.max_nodes:
        active_providers.remove(cloudapi.name)
        continue
    used_providers.append((row[0], row[1]))

assert active_providers, 'No suitable cloud providers'

if len(used_providers) < len(active_providers):
    name = random.choice(list(
        set(active_providers) - set([x[0] for x in used_providers])
    ))
else:
    name = sorted(used_providers, key=lambda x: x[1])[0][0]

cloudapi = clouds.apis[name]

provision_node(config, cloudapi.name)
cloudapi.init_key()
ip = cloudapi.create_node() # NB time-consuming
cloudapi.setup_node(ip) # NB time-consuming
assert not has_node(config, ip)
add_node(config, ip, cloud=cloudapi.name, provisioned=True)