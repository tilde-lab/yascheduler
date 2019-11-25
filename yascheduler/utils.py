"""
Console scripts for yascheduler
"""

import os
import sys
from pg8000.core import ProgrammingError
from configparser import ConfigParser
from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler


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


def init():
    # service initialization
    install_path = os.path.dirname(__file__)
    # check for systemd (exit status is 0 if there is a process)
    has_systemd = not os.system("pidof systemd")
    if has_systemd:
        _init_systemd(install_path)
    else:
        _init_sysv(install_path)
    _init_db(install_path)


def _init_systemd(install_path):
    print("Installing systemd service")
    # create unit file in /lib/systemd/system
    src_unit_file = os.path.join(install_path, 'data/yascheduler.service')
    unit_file = os.path.join('/lib/systemd/system/yascheduler.service')
    if not os.path.isfile(unit_file):
        daemon_file = os.path.join(install_path, 'daemon_systemd.py')
        systemd_script = open(src_unit_file).read().replace('%YASCHEDULER_DAEMON_FILE%', daemon_file)
        with open(unit_file, 'w') as f:
            f.write(systemd_script)


def _init_sysv(install_path):
    print("Installing SysV service")
    # create sysv script in /etc/init.d
    src_startup_file = os.path.join(install_path, 'data/yascheduler.sh')
    startup_file = os.path.join('/etc/init.d/yascheduler')
    if not os.path.isfile(startup_file):
        daemon_file = os.path.join(install_path, 'daemon_sysv.py')
        sysv_script = open(src_startup_file).read().replace('%YASCHEDULER_DAEMON_FILE%', daemon_file)
        with open(startup_file, 'w') as f:
            f.write(sysv_script)
        # make script executable
        os.chmod(startup_file, 0o755)


def _init_db(install_path):
    # database initialization
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    schema = open(os.path.join(install_path, 'data', 'schema.sql')).read()
    try:
        for line in schema.split(';'):
            yac.cursor.execute(line)
        yac.connection.commit()
    except ProgrammingError as e:
        if "already exists" in e.args[0]["M"]:
            print("Database already initialized!")
        else:
            print(e)
