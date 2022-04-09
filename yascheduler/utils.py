"""
Console scripts for yascheduler
"""
import os
import argparse
from configparser import ConfigParser

from pg8000 import ProgrammingError
from fabric import Connection as SSH_Connection
from paramiko.rsakey import RSAKey
from invoke.exceptions import UnexpectedExit

from yascheduler import has_node, add_node, remove_node
from yascheduler.variables import CONFIG_FILE
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
                                     'local_folder': os.getcwd()}) # TODO

    print("Successfully submitted task: {}".format(task_id))
    yac.connection.close()


def check_status():
    parser = argparse.ArgumentParser(description="Submit task to yascheduler daemon")
    parser.add_argument('-j', '--jobs', required=False, default=None, nargs='*')
    parser.add_argument('-v', '--view', required=False, default=None, nargs='?', type=bool, const=True)
    parser.add_argument('-o', '--convergence', required=False, default=None, nargs='?', type=bool, const=True, help='needs -v option')
    parser.add_argument('-i', '--info', required=False, default=None, nargs='?', type=bool, const=True)
    #parser.add_argument('-k', '--kill', required=False, default=None, nargs='?', type=bool, const=True)

    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    statuses = {
        yac.STATUS_TO_DO: "QUEUED",
        yac.STATUS_RUNNING: "RUNNING",
        yac.STATUS_DONE: "FINISHED"
    }
    local_parsing_ready, local_calc_snippet = False, False

    if args.jobs:
        tasks = yac.queue_get_tasks(jobs=args.jobs)
    else:
        tasks = yac.queue_get_tasks(status=(yac.STATUS_RUNNING, yac.STATUS_TO_DO))

    if args.view: # or args.kill:
        if not tasks:
            print('NO MATCHING TASKS FOUND')
            return
        ssh_custom_key = {}
        for filename in os.listdir(config.get('local', 'data_dir')):
            if not filename.startswith('yakey') or not os.path.isfile(
                os.path.join(config.get('local', 'data_dir'), filename)):
                continue
            key_path = os.path.join(config.get('local', 'data_dir'), filename)
            pmk_key = RSAKey.from_private_key_file(key_path)
            print('LOADED KEY %s' % key_path)
            ssh_custom_key = {'pkey': pmk_key}
            break

    if args.convergence:
        try:
            from pycrystal import CRYSTOUT, CRYSTOUT_Error
            from numpy import nan
            local_parsing_ready = True
        except: pass

    if args.view:
        yac.cursor.execute(
            (
                "SELECT t.task_id, t.label, t.metadata, t.ip, n.cloud "
                "FROM yascheduler_tasks AS t "
                "JOIN yascheduler_nodes AS n ON n.ip=t.ip "
                "WHERE status=%s AND task_id IN (%s);"
            ),
            (
                yac.STATUS_RUNNING,
                ", ".join([str(task["task_id"]) for task in tasks]),
            ),
        )
        for row in yac.cursor.fetchall():
            ssh_user = config.get(
                "clouds", f"{row[4]}", fallback=config.get("remote", "user")
            )
            print(
                "." * 50
                + "ID%s %s at %s@%s:%s"
                % (row[0], row[1], ssh_user, row[3], row[2]["remote_folder"])
            )
            ssh_conn = SSH_Connection(
                host=row[3], user=ssh_user, connect_kwargs=ssh_custom_key
            )
            try:
                result = ssh_conn.run('tail -n15 %s/OUTPUT' % row[2]['remote_folder'], hide=True)
            except UnexpectedExit:
                print('OUTDATED TASK, SKIPPING')
            else:
                print(result.stdout)

            if local_parsing_ready:
                local_calc_snippet = os.path.join(config.get('local', 'data_dir'), 'local_calc_snippet.tmp')
                try:
                    ssh_conn.get(row[2]['remote_folder'] + '/OUTPUT', local_calc_snippet)
                except IOError as err:
                    continue
                try:
                    calc = CRYSTOUT(local_calc_snippet)
                except CRYSTOUT_Error as err:
                    print(err)
                    continue
                output_lines = ''
                if calc.info['convergence']:
                    output_lines += str(calc.info['convergence']) + "\n"
                if calc.info['optgeom']:
                    for n in range(len(calc.info['optgeom'])):
                        try:
                            ncycles = calc.info['ncycles'][n]
                        except IndexError:
                            ncycles = "^"
                        output_lines += "{:8f}".format(calc.info['optgeom'][n][0] or nan) + "  " + \
                                        "{:8f}".format(calc.info['optgeom'][n][1] or nan) + "  " + \
                                        "{:8f}".format(calc.info['optgeom'][n][2] or nan) + "  " + \
                                        "{:8f}".format(calc.info['optgeom'][n][3] or nan) + "  " + \
                                        "E={:12f}".format(calc.info['optgeom'][n][4] or nan) + " eV" + "  " + \
                                        "(%s)" % ncycles + "\n"
                print(output_lines)

    #elif args.kill:
    #    if not args.jobs:
    #        print('NO JOBS GIVEN')
    #        return
    #    yac.cursor.execute(
    #        'SELECT ip FROM yascheduler_tasks WHERE status=%s AND task_id IN (%s);' % (
    #        yac.STATUS_RUNNING, ', '.join([str(task['task_id']) for task in tasks])
    #    ))
    #    for row in yac.cursor.fetchall():
    #        ssh_conn = SSH_Connection(host=row[0], user=config.get('remote', 'user'),
    #            connect_kwargs=ssh_custom_key)
    #        try:
    #            result = ssh_conn.run('pkill %s' % yac.RUNNING_MARKER, hide=True)
    #        except: pass

    elif args.info:
        for task in tasks:
            print('task_id={}\tstatus={}\tlabel={}\tip={}'.format(
                task['task_id'], statuses[task['status']], task['label'], task['ip'] or '-'
            ))

    else:
        for task in tasks:
            print('{}   {}'.format(task['task_id'], statuses[task['status']]))

    yac.connection.close()

    if local_calc_snippet and os.path.exists(local_calc_snippet):
        os.unlink(local_calc_snippet)


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
        if not os.access(startup_file, os.W_OK):
            print("Error: cannot write to %s" % startup_file)
            return

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
