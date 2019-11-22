"""
Console scripts for yascheduler
"""

import sys
from configparser import ConfigParser
from yascheduler import CONFIG_FILE
from yascheduler.yascheduler import Yascheduler


def submit():
    setup_input = open(sys.argv[1]).read()
    struct_input = open(sys.argv[2]).read()
    label = setup_input.splitlines()[0]

    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    task_id = yac.queue_submit_task(label, dict(structure=struct_input, input=setup_input))
    print("Successfully submitted task: {}".format(task_id))


def check_status():
    print("Not implemented!")


def init_db():
    print(__file__)
