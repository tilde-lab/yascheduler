"""
Console scripts for yascheduler
"""
import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any, Mapping, Sequence

from pg8000 import ProgrammingError

from .client import Yascheduler
from .config import Config
from .db import DB, TaskModel, TaskStatus
from .remote_machine import RemoteMachine
from .scheduler import Scheduler, get_logger
from .variables import CONFIG_FILE


def submit():
    parser = argparse.ArgumentParser(
        description="Submit task to yascheduler via AiiDA script"
    )
    parser.add_argument("script")

    args = parser.parse_args()
    script_file = Path(args.script)
    if not script_file.exists():
        raise ValueError("Script parameter is not a file name")

    log = logging.getLogger()
    log.setLevel(logging.ERROR)
    yac = Yascheduler(logger=log)

    script_params = {}
    with script_file.open("r") as f:
        for line in f.readlines():
            try:
                k, v = line.split("=")
                script_params[k.strip()] = v.strip()
            except ValueError:
                pass

    label = script_params.get("LABEL", "AiiDA job")
    metadata: Mapping[str, Any] = {"local_folder": os.getcwd()} # NB AiiDA chdirs to repo, but if not?
    if not script_params.get("ENGINE"):
        raise ValueError("Script has not defined an engine")

    engine = yac.config.engines.get(script_params["ENGINE"])
    if not engine:
        raise ValueError("Engine %s is not supported" % script_params["ENGINE"])

    for input_file in engine.input_files:
        try:
            metadata[input_file] = Path(
                metadata["local_folder"], input_file
            ).read_text()
        except Exception as err:
            raise ValueError(
                "Script was not supplied with the required input file"
            ) from err

    webhook_onsubmit = False
    if "PARENT" in script_params and yac.config.local.webhook_url:
        metadata["webhook_url"] = yac.config.local.webhook_url
        metadata["webhook_custom_params"] = {"parent": script_params["PARENT"]}
        webhook_onsubmit = True

    task_id = yac.queue_submit_task(
        label,
        metadata,
        engine.name,
        webhook_onsubmit=webhook_onsubmit,
    )

    # this should be received by AiiDA
    print(str(task_id))


async def _check_status():  # noqa: C901
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
    # parser.add_argument(
    #     "-k", "--kill", required=False, default=None, nargs="?", type=bool, const=True
    # )

    args = parser.parse_args()
    config = Config.from_config_parser(CONFIG_FILE)
    db = await DB.create(config.db)

    local_parsing_ready, local_calc_snippet = False, False

    tasks: Sequence[TaskModel] = []
    if args.jobs:
        tasks = await db.get_tasks_by_jobs(jobs=args.jobs)
    else:
        tasks = await db.get_tasks_by_status(
            statuses=(TaskStatus.RUNNING, TaskStatus.TO_DO)
        )

    if args.convergence:
        try:
            from numpy import nan
            from pycrystal import CRYSTOUT, CRYSTOUT_Error

            local_parsing_ready = True
        except Exception:
            pass

    if args.view:
        for task in await db.get_tasks_with_cloud_by_id_status(
            ids=list(map(lambda x: x.task_id, tasks)), status=TaskStatus.RUNNING
        ):
            ssh_user = None
            for c in config.clouds:
                ssh_user = c.username
            ssh_user = ssh_user or config.remote.username
            print(
                "." * 50
                + "ID%s %s at %s@%s:%s:%s"
                % (
                    task.task_id,
                    task.label,
                    ssh_user,
                    task.ip,
                    task.cloud or "",
                    task.metadata.get("remote_folder", ""),
                )
            )
            machine = await RemoteMachine.create(
                host=task.ip,
                username=ssh_user,
                client_keys=config.local.get_private_keys(),
            )
            r_output = machine.path(task.metadata.get("remote_folder")) / "OUTPUT"
            result = await machine.run(f"tail -n15 {machine.quote(str(r_output))}")
            if result.returncode:
                print("OUTDATED TASK, SKIPPING")
            else:
                print(result.stdout)

            if local_parsing_ready:
                local_calc_snippet = Path(
                    config.local.data_dir, "local_calc_snippet.tmp"
                )
                try:
                    r_output = (
                        machine.path(task.metadata.get("remote_folder")) / "OUTPUT"
                    )
                    async with machine.sftp() as sftp:
                        await sftp.get([str(r_output)], local_calc_snippet)
                except OSError:
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
                    task.task_id, task.status.name, task.label, task.ip or "-"
                )
            )

    else:
        for task in tasks:
            print(f"{task.task_id}   {task.status.name}")

    await db.close()

    if local_calc_snippet and os.path.exists(local_calc_snippet):
        os.unlink(local_calc_snippet)


def check_status():
    asyncio.run(_check_status())


async def _init():
    # service initialization
    install_path = Path(__file__).parent
    # check for systemd (exit status is 0 if there is a process)
    has_systemd = not os.system("pidof systemd")
    if has_systemd:
        _init_systemd(install_path)  # NB. will be absent in *service --status-all*
    else:
        _init_sysv(install_path)
    await _init_db(install_path)


def init():
    asyncio.run(_init())


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


async def _init_db(install_path: Path):
    # database initialization
    config = Config.from_config_parser(CONFIG_FILE)
    db = await DB.create(config.db, automigrate=False)
    schema = (install_path / "data" / "schema.sql").read_text()
    try:
        await db.run(schema)
        await db.commit()
        await db.close()
    except ProgrammingError as e:
        if "already exists" in str(e.args[0]):
            print("Database already initialized!")
        raise


async def _show_nodes():
    config = Config.from_config_parser(CONFIG_FILE)
    db = await DB.create(config.db)

    tasks = await db.get_tasks_by_status(statuses=[TaskStatus.RUNNING])
    nodes = await db.get_all_nodes()
    for node in nodes:
        node_tasks = list(filter(lambda x: x.ip == node.ip, tasks))
        node_task = ["-", "-"]
        for x in node_tasks:
            node_task = [x.label, x.task_id]
        data = tuple(
            [node.ip, node.ncpus or "MAX", node.enabled]
            + node_task
            + [node.cloud or ""]
        )
        print("ip=%s ncpus=%s enabled=%s occupied_by=%s (task_id=%s) %s" % data)


def show_nodes():
    asyncio.run(_show_nodes())


async def _manage_node():
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
    config = Config.from_config_parser(CONFIG_FILE)
    db = await DB.create(config.db)

    ncpus = None
    username = config.remote.username
    if "@" in args.host:
        username, args.host = args.host.split("@")
    if "~" in args.host:
        args.host, ncpus = args.host.split("~")
        ncpus = int(ncpus)

    already_there = await db.has_node(args.host)
    if already_there and not args.remove_hard and not args.remove_soft:
        print(f"Host already in DB: {args.host}")
        return False

    if not already_there and (args.remove_hard or args.remove_soft):
        print(f"Host NOT in DB: {args.host}")
        return False

    if args.remove_hard:
        task_ids = await db.get_task_ids_by_ip_and_status(args.host, TaskStatus.RUNNING)
        for task_id in task_ids:
            await db.update_task_status(task_id, TaskStatus.DONE)
            print(
                "An associated task %s at %s is now marked done!" % (task_id, args.host)
            )

        await db.remove_node(args.host)
        await db.commit()
        await db.close()
        print(f"Removed host from yascheduler: {args.host}")
        return True

    elif args.remove_soft:
        task_ids = await db.get_task_ids_by_ip_and_status(args.host, TaskStatus.RUNNING)
        if task_ids:
            print("A task associated, prevent from assigning the new tasks")
            await db.disable_node(args.host)
            print(f"Prevented from assigning the new tasks: {args.host}")
        else:
            print("No tasks associated, remove node immediately")
            await db.remove_node(args.host)
            print(f"Removed host from yascheduler: {args.host}")
        await db.commit()
        await db.close()
        return True

    machine = await RemoteMachine.create(
        host=args.host,
        username=username,
        client_keys=config.local.get_private_keys(),
        engines_dir=config.remote.engines_dir,
    )

    if not args.skip_setup:
        print("Setup host...")
        await machine.setup_node(config.engines)

    await db.add_node(ip_addr=args.host, username=username, ncpus=ncpus, enabled=True)
    await db.commit()
    await db.close()

    print(f"Added host to yascheduler: {args.host}")


def manage_node():
    asyncio.run(_manage_node())


def daemonize(log_file=None):
    parser = argparse.ArgumentParser(description="Start yascheduler daemon")
    parser.add_argument(
        "-l",
        "--log-level",
        default="INFO",
        help="set log level",
        choices=logging._levelToName.values(),
    )
    args = parser.parse_args()

    logger = get_logger(log_file, level=logging._nameToLevel[args.log_level])

    async def on_signal(
        y: Scheduler, shield: Sequence[asyncio.Task], signame: str, signum: int
    ):
        logger.info(f"Received signal {signame}")
        if signum in [signal.SIGTERM, signal.SIGINT]:
            await y.stop()
            shielded = [*shield, asyncio.current_task()]
            tasks = [t for t in asyncio.all_tasks() if t not in shielded]
            logger.info(f"Cancelling {len(tasks)} outstanding tasks")
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
            # Wait 250 ms for the underlying SSL connections to close
            await asyncio.sleep(0.25)
            logger.info("Done")

    async def run():
        yac = await Scheduler.create(log=logger)

        loop = asyncio.get_running_loop()
        current_task = asyncio.current_task()

        shielded = [current_task] if current_task else []
        for sig in [signal.SIGTERM, signal.SIGINT]:

            def handler():
                return asyncio.create_task(
                    on_signal(yac, shielded, sig.name, sig.value)
                )

            loop.add_signal_handler(sig, handler)

        await yac.start()

    asyncio.run(run())
