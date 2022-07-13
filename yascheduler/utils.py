"""
Console scripts for yascheduler
"""
import os
import argparse
from configparser import ConfigParser
from pathlib import Path

from pg8000 import ProgrammingError
from plumbum import local
from plumbum.commands.processes import ProcessExecutionError

from yascheduler import has_node, add_node, remove_node
from yascheduler.ssh import MyParamikoMachine
from yascheduler.variables import CONFIG_FILE
from yascheduler.scheduler import Yascheduler


def submit():
    parser = argparse.ArgumentParser(description="Submit task to yascheduler via AiiDA script")
    parser.add_argument("script")

    args = parser.parse_args()
    if not os.path.isfile(args.script):
        raise ValueError("Script parameter is not a file name")

    script_params = {}
    with open(args.script) as f:
        for l in f.readlines():
            try:
                k, v = l.split("=")
                script_params[k.strip()] = v.strip()
            except ValueError:
                pass

    label = script_params.get('LABEL', 'AiiDA job')
    options = {
        "local_folder": os.path.dirname(args.script)
    }
    if not script_params.get('ENGINE'):
        raise ValueError("Script has not defined an engine")

    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)

    if script_params['ENGINE'] not in yac.engines:
        raise ValueError("Script refers to unknown engine")

    for input_file in yac.engines[script_params['ENGINE']].input_files:
        if not os.path.exists(os.path.join(options["local_folder"], input_file)):
            raise ValueError("Script was not supplied with the required input file")

        options[input_file] = open(os.path.join(options["local_folder"], input_file)).read()

    task_id = yac.queue_submit_task(label, options, script_params['ENGINE'])

    # this should be received by AiiDA
    print(str(task_id))
    yac.connection.close()


def check_status():
    parser = argparse.ArgumentParser(description="Submit task to yascheduler daemon")
    parser.add_argument("-j", "--jobs", required=False, default=None, nargs="*")
    parser.add_argument(
        "-v", "--view", required=False, default=None, nargs="?", type=bool, const=True
    )
    parser.add_argument(
        "-o",
        "--convergence",
        required=False,
        default=None,
        nargs="?",
        type=bool,
        const=True,
        help="needs -v option",
    )
    parser.add_argument(
        "-i", "--info", required=False, default=None, nargs="?", type=bool, const=True
    )
    # parser.add_argument('-k', '--kill', required=False, default=None, nargs='?', type=bool, const=True)

    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    statuses = {
        yac.STATUS_TO_DO: "QUEUED",
        yac.STATUS_RUNNING: "RUNNING",
        yac.STATUS_DONE: "FINISHED",
    }
    local_parsing_ready, local_calc_snippet = False, False

    if args.jobs:
        tasks = yac.queue_get_tasks(jobs=args.jobs)
    else:
        tasks = yac.queue_get_tasks(status=(yac.STATUS_RUNNING, yac.STATUS_TO_DO))

    if args.convergence:
        try:
            from pycrystal import CRYSTOUT, CRYSTOUT_Error
            from numpy import nan

            local_parsing_ready = True
        except:
            pass

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
            machine = MyParamikoMachine.create_machine(
                host=row[3],
                user=ssh_user,
                keys_dir=yac.local_keys_dir,
            )
            try:
                r_output = machine.path("{}/OUTPUT".format(row[2]["remote_folder"]))
                result = machine.cmd.tail("-n15", r_output)
            except ProcessExecutionError:
                print("OUTDATED TASK, SKIPPING")
            else:
                print(result)

            if local_parsing_ready:
                local_calc_snippet = Path(
                    config.get("local", "data_dir"), "local_calc_snippet.tmp"
                )
                try:
                    r_output = machine.path(row[2]["remote_folder"]).join("OUTPUT")
                    machine.download(r_output, local_calc_snippet)
                except IOError as err:
                    continue
                try:
                    calc = CRYSTOUT(local_calc_snippet)
                except CRYSTOUT_Error as err:
                    print(err)
                    continue
                output_lines = ""
                if calc.info["convergence"]:
                    output_lines += str(calc.info["convergence"]) + "\n"
                if calc.info["optgeom"]:
                    for n in range(len(calc.info["optgeom"])):
                        try:
                            ncycles = calc.info["ncycles"][n]
                        except IndexError:
                            ncycles = "^"
                        output_lines += (
                            "{:8f}".format(calc.info["optgeom"][n][0] or nan)
                            + "  "
                            + "{:8f}".format(calc.info["optgeom"][n][1] or nan)
                            + "  "
                            + "{:8f}".format(calc.info["optgeom"][n][2] or nan)
                            + "  "
                            + "{:8f}".format(calc.info["optgeom"][n][3] or nan)
                            + "  "
                            + "E={:12f}".format(calc.info["optgeom"][n][4] or nan)
                            + " eV"
                            + "  "
                            + "(%s)" % ncycles
                            + "\n"
                        )
                print(output_lines)

    # elif args.kill:
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
            print(
                "task_id={}\tstatus={}\tlabel={}\tip={}".format(
                    task["task_id"],
                    statuses[task["status"]],
                    task["label"],
                    task["ip"] or "-",
                )
            )

    else:
        for task in tasks:
            print("{}   {}".format(task["task_id"], statuses[task["status"]]))

    yac.connection.close()

    if local_calc_snippet and os.path.exists(local_calc_snippet):
        os.unlink(local_calc_snippet)


def init():
    # service initialization
    install_path = Path(__file__).parent
    # check for systemd (exit status is 0 if there is a process)
    try:
        local.cmd.pidof("systemd")
        _init_systemd(install_path)  # NB. will be absent in *service --status-all*
    except ProcessExecutionError:
        _init_sysv(install_path)
    _init_db(install_path)


def _init_systemd(install_path: Path):
    print("Installing systemd service")
    # create unit file in /lib/systemd/system
    src_unit_file = install_path / "data/yascheduler.service"
    unit_file = Path("/lib/systemd/system/yascheduler.service")
    if not unit_file.is_file():
        if not os.access(unit_file, os.W_OK):
            print("Error: cannot write to %s" % unit_file)
            return
        daemon_file = install_path / "daemon_systemd.py"
        systemd_script = src_unit_file.read_text("utf-8").replace(
            "%YASCHEDULER_DAEMON_FILE%", str(daemon_file)
        )
        unit_file.write_text(systemd_script, "utf-8")


def _init_sysv(install_path: Path):

    print("Installing SysV service")

    # create sysv script in /etc/init.d
    src_startup_file = install_path / "data/yascheduler.sh"
    startup_file = Path("/etc/init.d/yascheduler")
    if not startup_file.is_file():
        if not os.access(startup_file, os.W_OK):
            print("Error: cannot write to %s" % startup_file)
            return

        daemon_file = install_path / "daemon_sysv.py"
        sysv_script = src_startup_file.read_text("utf-8").replace(
            "%YASCHEDULER_DAEMON_FILE%", str(daemon_file)
        )
        startup_file.write_text(sysv_script, "utf-8")
        # make script executable
        os.chmod(startup_file, 0o755)


def _init_db(install_path: Path):
    # database initialization
    config = ConfigParser()
    config.read(CONFIG_FILE)
    yac = Yascheduler(config)
    schema = (install_path / "data" / "schema.sql").read_text()
    try:
        for line in schema.split(";"):
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

    yac.cursor.execute(
        "SELECT ip, label, task_id FROM yascheduler_tasks WHERE status=%s;",
        [yac.STATUS_RUNNING],
    )
    tasks_running = {row[0]: [row[1], row[2]] for row in yac.cursor.fetchall()}

    yac.cursor.execute("SELECT ip, ncpus, enabled, cloud from yascheduler_nodes;")
    for item in yac.cursor.fetchall():
        print(
            "ip=%s ncpus=%s enabled=%s occupied_by=%s (task_id=%s) %s"
            % tuple(
                [item[0], item[1] or "MAX", item[2]]
                + tasks_running.get(item[0], ["-", "-"])
                + [item[3] or ""]
            )
        )


def manage_node():
    parser = argparse.ArgumentParser(description="Add nodes to yascheduler daemon")
    parser.add_argument("host", help="IP[~ncpus]")
    parser.add_argument(
        "--skip-setup",
        required=False,
        default=False,
        nargs="?",
        type=bool,
        const=True,
        help="Skip node setup",
    )
    parser.add_argument(
        "--remove-soft",
        required=False,
        default=None,
        nargs="?",
        type=bool,
        const=True,
        help="Remove IP delayed",
    )
    parser.add_argument(
        "--remove-hard",
        required=False,
        default=None,
        nargs="?",
        type=bool,
        const=True,
        help="Remove IP immediate",
    )

    args = parser.parse_args()
    config = ConfigParser()
    config.read(CONFIG_FILE)

    ncpus = None
    if "~" in args.host:
        args.host, ncpus = args.host.split("~")
        ncpus = int(ncpus)

    yac = Yascheduler(config)

    already_there = has_node(config, args.host)
    if already_there and not args.remove_hard and not args.remove_soft:
        print("Host already in DB: {}".format(args.host))
        return False

    if not already_there and (args.remove_hard or args.remove_soft):
        print("Host NOT in DB: {}".format(args.host))
        return False

    if args.remove_hard:
        yac.cursor.execute(
            "SELECT task_id from yascheduler_tasks WHERE ip=%s AND status=%s;",
            [args.host, yac.STATUS_RUNNING],
        )
        result = yac.cursor.fetchall() or []
        for (
            item
        ) in (
            result
        ):  # only one item is expected, but here we also account inconsistency case
            yac.cursor.execute(
                "UPDATE yascheduler_tasks SET status=%s WHERE task_id=%s;",
                [yac.STATUS_DONE, item[0]],
            )
            yac.connection.commit()
            print(
                "An associated task %s at %s is now marked done!" % (item[0], args.host)
            )

        remove_node(config, args.host)
        print("Removed host from yascheduler: {}".format(args.host))
        return True

    elif args.remove_soft:
        yac.cursor.execute(
            "SELECT task_id from yascheduler_tasks WHERE ip=%s AND status=%s;",
            [args.host, yac.STATUS_RUNNING],
        )
        if yac.cursor.fetchall():
            print("A task associated, prevent from assigning the new tasks")
            yac.cursor.execute(
                "UPDATE yascheduler_nodes SET enabled=FALSE WHERE ip=%s;", [args.host]
            )
            yac.connection.commit()
            print("Prevented from assigning the new tasks: {}".format(args.host))
            return True

        else:
            print("No tasks associated, remove node immediately")
            remove_node(config, args.host)
            print("Removed host from yascheduler: {}".format(args.host))
            return True

    # check connection
    yac.ssh_connect([args.host])

    if not args.skip_setup:
        print("Setup host...")
        yac.setup_node(args.host, "root")

    add_node(config, args.host, ncpus)

    print("Added host to yascheduler: {}".format(args.host))
    return True
