#!/usr/bin/env python3

import sys
from configparser import ConfigParser
from yascheduler import Yascheduler

setup_input = open(sys.argv[1]).read()
struct_input = open(sys.argv[2]).read()
label = setup_input.splitlines()[0]

config = ConfigParser()
config.read('env.ini')
yac = Yascheduler(config)
task_id = yac.queue_submit_task(label, dict(structure=struct_input, input=setup_input))
print(task_id)