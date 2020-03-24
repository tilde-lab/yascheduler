"""
Console scripts for yascheduler
"""
import os
import argparse
from configparser import ConfigParser

from pg8000.core import ProgrammingError
from fabric import Connection as SSH_Connection
from yascheduler import CONFIG_FILE, has_node, add_node, remove_node
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
    yac.connection.close()


def check_status():
    parser = argparse.ArgumentParser(description="Submit task to yascheduler daemon")
    parser.add_argument('-j', '--jobs', required=False, default=None, nargs='*')
    parser.add_argument('-v', '--view', required=False, default=None, nargs='?', type=bool, const=True)
    parser.add_argument('-i', '--info', required=False, default=None, nargs='?', type=bool, const=True)

    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    statuses = {
        yac.STATUS_TO_DO: "QUEUED",
        yac.STATUS_RUNNING: "RUNNING",
        yac.STATUS_DONE: "FINISHED"
    }
    if args.jobs:
        tasks = yac.queue_get_tasks(jobs=args.jobs)
    else:
        tasks = yac.queue_get_tasks(status=(yac.STATUS_RUNNING, yac.STATUS_TO_DO))

    if args.view and tasks:
        yac.cursor.execute(
            'SELECT task_id, label, metadata, ip FROM yascheduler_tasks WHERE status=%s AND task_id IN (%s);' % (
            yac.STATUS_RUNNING, ', '.join([str(task['task_id']) for task in tasks])
        ))
        for row in yac.cursor.fetchall():
            print("*" * 20 + "ID%s %s at %s@%s:%s" % (
                row[0], row[1], config.get('remote', 'user'), row[3], row[2]['remote_folder']
            ))
            ssh_conn = SSH_Connection(host=row[3], user=config.get('remote', 'user'))
            result = ssh_conn.run('tail -n15 %s/OUTPUT' % row[2]['remote_folder'], hide=True)
            print(result.stdout)

    elif args.info:
        for task in tasks:
            print('task_id={}\tstatus={}\tlabel={}\tip={}'.format(
                task['task_id'], statuses[task['status']], task['label'], task['ip'] or '-'
            ))

    else:
        for task in tasks:
            print('{}   {}'.format(task['task_id'], statuses[task['status']]))

    yac.connection.close()


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
            if not line:
                continue
            yac.cursor.execute(line)
        yac.connection.commit()
    except ProgrammingError as e:
        if "already exists" in str(e.args[0]):
            print("Database already initialized!")
        raise


def show_nodes():
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)

    yac.cursor.execute('SELECT ip, label, task_id FROM yascheduler_tasks WHERE status=%s;', [yac.STATUS_RUNNING])
    tasks_running = {row[0]: [row[1], row[2]] for row in yac.cursor.fetchall()}

    yac.cursor.execute('SELECT ip, ncpus, enabled, cloud from yascheduler_nodes;')
    for item in yac.cursor.fetchall():
        print("ip=%s ncpus=%s enabled=%s occupied_by=%s (task_id=%s) %s" % tuple(
            [item[0], item[1] or 'MAX', item[2]] + tasks_running.get(item[0], ['-', '-']) + [item[3] or '']
        ))


def manage_node():
    parser = argparse.ArgumentParser(description="Add nodes to yascheduler daemon")
    parser.add_argument('host',
        help='IP[~ncpus]')
    parser.add_argument('--remove-soft', required=False, default=None, nargs='?', type=bool, const=True,
        help='Remove IP delayed')
    parser.add_argument('--remove-hard', required=False, default=None, nargs='?', type=bool, const=True,
        help='Remove IP immediate')

    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)

    ncpus = None
    if '~' in args.host:
        args.host, ncpus = args.host.split('~')
        ncpus = int(ncpus)

    yac = Yascheduler(config)

    already_there = has_node(config, args.host)
    if already_there and not args.remove_hard and not args.remove_soft:
        print('Host already in DB: {}'.format(args.host))
        return False
    if not already_there and (args.remove_hard or args.remove_soft):
        print('Host NOT in DB: {}'.format(args.host))
        return False

    if args.remove_hard:
        yac.cursor.execute('SELECT task_id from yascheduler_tasks WHERE ip=%s AND status=%s;', [args.host, yac.STATUS_RUNNING])
        result = yac.cursor.fetchall() or []
        for item in result: # only one item is expected, but here we also account inconsistency case
            yac.cursor.execute('UPDATE yascheduler_tasks SET status=%s WHERE task_id=%s;', [yac.STATUS_DONE, item[0]])
            yac.connection.commit()
            print('An associated task %s at %s is now marked done!' % (item[0], args.host))

        remove_node(config, args.host)
        print('Removed host from yascheduler: {}'.format(args.host))
        return True

    elif args.remove_soft:
        yac.cursor.execute('SELECT task_id from yascheduler_tasks WHERE ip=%s AND status=%s;', [args.host, yac.STATUS_RUNNING])
        if yac.cursor.fetchall():
            print('A task associated, prevent from assigning the new tasks')
            yac.cursor.execute('UPDATE yascheduler_nodes SET enabled=FALSE WHERE ip=%s;', [args.host])
            yac.connection.commit()
            print('Prevented from assigning the new tasks: {}'.format(args.host))
            return True

        else:
            print('No tasks associated, remove node immediately')
            remove_node(config, args.host)
            print('Removed host from yascheduler: {}'.format(args.host))
            return True

    if not yac.ssh_check_node(args.host) or not add_node(config, args.host, ncpus):
        print('Failed to add host to yascheduler: {}'.format(args.host))
        return False
    print('Added host to yascheduler: {}'.format(args.host))
    return True
