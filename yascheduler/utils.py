"""
Console scripts for yascheduler
"""

import os
import argparse
from pg8000.core import ProgrammingError
from fabric import Connection as SSH_Connection
import socket
from configparser import ConfigParser
from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler


def submit():
    parser = argparse.ArgumentParser(description="Submit task to yascheduler daemon")
    parser.add_argument('script')
    args = parser.parse_args()
    if not os.path.isfile(args.script):
        raise ValueError("Script parameter is not a file name")
    inputs = {}
    with open(args.script) as f:
        for l in f.readlines():
            try:
                k, v = l.split('=')
                inputs[k.strip()] = v.strip()
            except ValueError:
                pass
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    task_id = yac.queue_submit_task(inputs['LABEL'],
                                    {'structure': open(inputs['STRUCT']).read(),
                                     'input': open(inputs['INPUT']).read(),
                                     'local_folder': os.getcwd()})

    print("Successfully submitted task: {}".format(task_id))


def check_status():
    parser = argparse.ArgumentParser(description="Submit task to yascheduler daemon")
    parser.add_argument('-j', '--jobs', required=False, default=None, nargs='*')
    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    status = {
        yac.STATUS_TO_DO: "QUEUED",
        yac.STATUS_RUNNING: "RUNNING",
        yac.STATUS_DONE: "FINISHED"
    }
    if args.jobs is not None:
        tasks = yac.queue_get_tasks(jobs=args.jobs)
    else:
        tasks = yac.queue_get_tasks(status=(yac.STATUS_RUNNING, yac.STATUS_TO_DO))
    for task in tasks:
        print('{}   {}'.format(task['task_id'], status[task['status']]))


def init():
    # service initialization
    install_path = os.path.dirname(__file__)
    # check for systemd (exit status is 0 if there is a process)
    has_systemd = not os.system("pidof systemd")
    if has_systemd:
        _init_systemd(install_path) # NB. will be absent in *service --status-all*
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
            raise


def add_node():
    # parse command line arguments
    parser = argparse.ArgumentParser(description="Add nodes to yascheduler daemon")
    parser.add_argument('host')
    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)
    try:
        with SSH_Connection(host=args.host, user=config.get('remote', 'user'), connect_timeout=5) as conn:
            conn.run('ls')
    except socket.timeout:
        print('Host %s@%s is unreachable' % (config.get('remote', 'user'), args.host))
        return False

    yac = Yascheduler(config)
    # check if node is already there
    yac.cursor.execute('SELECT * from yascheduler_nodes WHERE ip=%s;', [args.host])
    if yac.cursor.fetchall():
        print('Host already in DB: {}'.format(args.host))
        return False
    else:
        yac.cursor.execute('INSERT INTO yascheduler_nodes (ip) VALUES (%s);', [args.host])
        yac.connection.commit()
        print('Added host to yascheduler: {}'.format(args.host))
        return True

