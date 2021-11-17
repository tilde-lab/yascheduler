#!/usr/bin/env python3

import os
import sys
from configparser import ConfigParser

from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler


target = os.path.abspath(sys.argv[1])
work_folder = os.path.dirname(target)
setup_input = open(target).read()

try: sys.argv[2]
except IndexError:
    folder = None
    print('**To save calc in a local repo**')
else:
    folder = work_folder
    print('**To save calc in an input folder**')

if os.path.exists(os.path.join(work_folder, 'fort.34')):
    assert "EXTERNAL" in setup_input
    struct_input = open(os.path.join(work_folder, 'fort.34')).read()
else:
    assert "EXTERNAL" not in setup_input
    struct_input = "UNUSED"

label = setup_input.splitlines()[0]

config = ConfigParser()
config.read(CONFIG_FILE)
yac = Yascheduler(config)

result = yac.queue_submit_task(label, dict(structure=struct_input, input=setup_input, local_folder=folder))
print(label)
print(result)
