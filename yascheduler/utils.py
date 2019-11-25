"""
Console scripts for yascheduler
"""

import os
import sys
from pg8000.core import ProgrammingError
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
    # database initialization
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    schema = open(os.path.join(os.path.dirname(__file__), 'data', 'schema.sql')).read()
    try:
        for line in schema.split(';'):
            yac.cursor.execute(line)
        yac.connection.commit()
    except ProgrammingError as e:
        if "already exists" in e.args[0]["M"]:
            print("Database already initialized!")
        else:
            print(e)

