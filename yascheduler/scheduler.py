#!/usr/bin/env python

import asyncio
import logging
from asyncio.locks import Event, Semaphore
from collections import Counter
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path, PurePath, PurePosixPath
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import aiohttp
import asyncssh
import backoff
from asyncssh.sftp import SFTPClient, SFTPError
from attrs import asdict, define, evolve, field
from typing_extensions import Self

from .clouds import CloudAPIManager, PCloudAPIManager
from .config import Config, Engine
from .db import DB, NodeModel, TaskModel, TaskStatus
from .queue import TUMsgId, TUMsgPayload, UMessage, UniqueQueue
from .remote_machine import (
    AllSSHRetryExc,
    PRemoteMachine,
    RemoteMachine,
    RemoteMachineRepository,
    SFTPRetryExc,
)
from .time import asleep_until
from .variables import CONFIG_FILE

logging.basicConfig(level=logging.INFO)


def get_logger(log_file, level: int = logging.INFO):
    logger = logging.getLogger("yascheduler")
    logger.setLevel(level)

    backoff_logger = logging.getLogger("backoff")
    backoff_logger.setLevel(logging.ERROR if level >= logging.INFO else logging.DEBUG)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        backoff_logger.addHandler(fh)

    return logger


@define(frozen=True)
class WebhookPayload:
    task_id: int = field()
    status: int = field()
    custom_params: Mapping[str, Any] = field(factory=dict)


@define
class Scheduler:
    config: Config = field()
    db: DB = field()
    clouds: PCloudAPIManager = field()
    log: logging.Logger = field()
    bg_jobs: Set[asyncio.Task] = field(factory=set, init=False)
    conn_machine_q: UniqueQueue[str, NodeModel] = field(init=False)
    allocate_q: UniqueQueue[int, TaskModel] = field(init=False)
    consume_q: UniqueQueue[int, TaskModel] = field(init=False)
    deallocate_q: UniqueQueue[str, NodeModel] = field(init=False)
    cancellation_event: Event = field(factory=Event, init=False)
    remote_machines: RemoteMachineRepository = field()
    sleep_interval: int = field(default=1)
    http: aiohttp.ClientSession = field(factory=aiohttp.ClientSession, init=False)
    webhook_sem: Semaphore = field(init=False)

    def __attrs_post_init__(self):
        lcfg = self.config.local
        self.webhook_sem = Semaphore(lcfg.webhook_reqs_limit)
        self.conn_machine_q = UniqueQueue(
            "conn_machine", maxsize=lcfg.conn_machine_pending
        )
        self.allocate_q = UniqueQueue("allocate", maxsize=lcfg.allocate_pending)
        self.consume_q = UniqueQueue("consume", maxsize=lcfg.consume_pending)
        self.deallocate_q = UniqueQueue("deallocate", maxsize=lcfg.deallocate_pending)

    @classmethod
    async def create(
        cls,
        config: Optional[Config] = None,
        log: Optional[logging.Logger] = None,
    ) -> Self:
        "Async object initialization"
        if log:
            log = log.getChild(cls.__name__)
        else:
            log = logging.getLogger(cls.__name__)
        cfg = config or Config.from_config_parser(CONFIG_FILE)
        db = await DB.create(cfg.db)
        clouds = await CloudAPIManager.create(
            db=db,
            local_config=cfg.local,
            remote_config=cfg.remote,
            cloud_configs=cfg.clouds,
            engines=cfg.engines,
            log=log,
        )

        return cls(
            config=cfg,
            db=db,
            clouds=clouds,
            log=log,
            remote_machines=RemoteMachineRepository(log=log),
            sleep_interval=min(x.sleep_interval for x in cfg.engines.values()),
        )

    async def clouds_get_capacity(self) -> int:
        "Get capacity of all clouds"
        ccap = await self.clouds.get_capacity()
        n_busy_cloud_nodes = sum(x.current for x in ccap.values())
        max_nodes = sum(x.config.max_nodes for x in self.clouds.apis.values())
        diff = max_nodes - n_busy_cloud_nodes
        return max(0, diff)

    async def do_task_webhook(
        self, task_id: int, metadata: Mapping[str, Any], status: TaskStatus
    ):
        "Send webhook with task status"
        retry = backoff.on_exception(backoff.fibo, aiohttp.ClientError, max_time=60)
        url = metadata.get("webhook_url")
        if not url:
            return
        async with self.webhook_sem:
            self.log.info(f"Executing webhook to {url}")
            payload = WebhookPayload(
                task_id, status.value, metadata.get("webhook_custom_params", {})
            )
            try:
                async with retry(self.http.post)(url, data=asdict(payload)) as resp:
                    if resp.ok:
                        return
                    self.log.warn(
                        "Webhook for task_id=%s bad response: %s %s",
                        task_id,
                        resp.status,
                        resp.reason,
                    )
                    if self.log.isEnabledFor(logging.DEBUG):
                        self.log.debug(
                            "Webhook for task_id=%s response: %s",
                            task_id,
                            (await resp.text("utf-8")),
                        )
            except Exception as err:
                self.log.error("Webhook for task_id=%s failed: %s", task_id, err)

    async def create_new_task(
        self,
        label: str,
        metadata: Mapping[str, Any],
        engine_name: str,
        webhook_onsubmit: bool = False,
    ) -> TaskModel:
        "Create new task in DB"
        if engine_name not in self.config.engines:
            raise RuntimeError("Engine %s requested, but not supported" % engine_name)

        for input_file in self.config.engines[engine_name].input_files:
            if input_file not in metadata:
                raise RuntimeError("Input file %s was not provided" % input_file)

        meta_add = [("engine", engine_name)]
        new_meta = dict(list(metadata.items()) + meta_add)

        task = await self.db.add_task(
            label, ip_addr=None, status=TaskStatus.TO_DO, metadata=new_meta
        )
        dt_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        remote_folder = self.config.remote.tasks_dir / "{}_{}".format(
            dt_str, task.task_id
        )
        new_meta.update({"remote_folder": str(remote_folder)})
        await self.db.update_task_meta(task.task_id, new_meta)
        await self.db.commit()
        if webhook_onsubmit:
            await self.do_task_webhook(task.task_id, new_meta, TaskStatus.TO_DO)
        self.log.info(":::submitted: %s" % label)
        return task

    async def upload_task_data(
        self,
        sftp: SFTPClient,
        task: TaskModel,
        remote_dir: PurePath,
        input_files: Sequence[str],
    ) -> bool:
        "Upload task data to remote machine"
        try:
            await sftp.makedirs(PurePosixPath(remote_dir), exist_ok=True)
        except asyncssh.misc.Error as err:
            self.log.error(
                "Create %s - SFTPError: %s (%s) (task_id=%s)"
                % (str(remote_dir), err.reason, err.code, task.task_id)
            )
            raise err
        for input_file in input_files:
            r_input_file = remote_dir / input_file
            try:
                async with sftp.open(r_input_file.as_posix(), pflags_or_mode="w") as f:
                    await f.write(task.metadata[input_file])
            except asyncssh.misc.Error as err:
                self.log.error(
                    "Write %s - SFTPError: %s (%s)"
                    % (str(r_input_file), err.reason, err.code)
                )
                raise err
        return True

    async def start_task_on_machine(
        self,
        machine: PRemoteMachine,
        engine: Engine,
        task: TaskModel,
    ) -> bool:
        "Run task on remote machine"
        self.log.info(
            "Submitting task_id=%s %s to %s"
            % (task.task_id, task.label, machine.hostname)
        )
        assert task.metadata.get("remote_folder")
        machine.meta.busy = True
        remote_folder = machine.path(task.metadata["remote_folder"])

        async with machine.sftp() as sftp:
            try:
                root_dir = machine.path(await sftp.realpath("."))
                task_dir = (
                    remote_folder
                    if remote_folder.is_absolute()
                    else root_dir / remote_folder
                )
                if self.config.remote.engines_dir.is_absolute():
                    engine_path = self.config.remote.engines_dir / engine.name
                else:
                    engine_path = (
                        root_dir / self.config.remote.engines_dir / engine.name
                    )
                # upload files
                await self.upload_task_data(sftp, task, task_dir, engine.input_files)
            except Exception as err:
                self.log.error(f"Can't upload task_id={task.task_id} files: {err}")
                raise err

        # start task
        try:
            # detect cpus
            node = await self.db.get_node(task.ip)
            ncpus = node and node.ncpus or await machine.get_cpu_cores()
            # placeholders {task_path}, {engine_path} and {ncpus} are supported
            run_cmd = engine.spawn.format(
                engine_path=str(engine_path),
                task_path=machine.quote(str(task_dir)),
                ncpus=ncpus,
            )
            await machine.run_bg(run_cmd, cwd=str(task_dir))
        except Exception as err:
            self.log.error("SSH spawn cmd error: %s" % err)
            raise err

        return True

    async def allocate_task(self, task: TaskModel) -> bool:
        "Allocate task to a free remote machine or ask allocation of new cloud machine"
        self.log.debug(f"Allocating task {task.task_id}")
        engine_name: Optional[str] = task.metadata.get("engine", None)
        if not engine_name or engine_name not in self.config.engines:
            self.log.warning(
                "Unsupported engine '%s' for task_id=%s" % (engine_name, task.task_id)
            )
            await self.db.set_task_error(
                task.task_id, metadata=task.metadata, error="unsupported engine"
            )
            await self.do_task_webhook(task.task_id, task.metadata, TaskStatus.DONE)
            return False
        engine: Engine = self.config.engines[engine_name]

        busy_node_ips = [
            t.ip for t in await self.db.get_tasks_by_status((TaskStatus.RUNNING,))
        ]
        free_machines = {
            ip: m
            for ip, m in self.remote_machines.filter(
                busy=False, platforms=engine.platforms, reverse_sort=True
            ).items()
            if ip not in busy_node_ips
        }
        if free_machines:
            self.log.debug(
                "Free machines with platform match: %s"
                % ", ".join(free_machines.keys())
            )
        for ip, machine in free_machines.items():
            task_m = evolve(task, ip=ip)
            self.log.debug(f"Allocate task {task.task_id} to machine {ip}")
            if await self.start_task_on_machine(machine, engine, task_m):
                self.log.debug(f"Task {task.task_id} allocated to machine {ip}")
                await machine.start_occupancy_check(engine)
                await self.db.set_task_running(task.task_id, task_m.ip)
                await self.db.commit()
                await self.do_task_webhook(
                    task.task_id, task_m.metadata, TaskStatus.RUNNING
                )
                self.clouds.mark_task_done(task.task_id)
                return True

        # free machine not found - try to allocate new node
        self.log.debug(
            f"No free machine for task {task.task_id} - want to allocate new node"
        )
        await self.clouds.allocate(
            task.task_id, want_platforms=engine.platforms, throttle=True
        )
        return False

    async def consume_task(self, machine: PRemoteMachine, task: TaskModel):
        "Consume done tasks"
        meta = task.metadata
        local_folder: Union[str, None] = meta.get("local_folder")
        remote_folder: str = meta["remote_folder"]
        engine: Engine = self.config.engines[meta["engine"]]
        # NOTE: PureWindowsPath is not supported but posix is fine
        output_files = [
            str(PurePosixPath(remote_folder) / x) for x in engine.output_files
        ]
        if local_folder:
            store_folder = Path(local_folder)
        else:
            store_folder = self.config.local.tasks_dir / Path(remote_folder).name
        await asyncio.get_running_loop().run_in_executor(
            None, store_folder.mkdir, 0o777, True, True
        )

        meta_add = [
            ("remote_folder", remote_folder),
            ("local_folder", str(store_folder)),
        ]

        sftp_errors: List[Tuple[Optional[str], Exception]] = []
        sftp_get_retry = backoff.on_exception(backoff.fibo, SFTPRetryExc, max_time=60)

        async def job():
            async with machine.sftp() as sftp:
                for out_file in output_files:
                    try:
                        await sftp_get_retry(sftp.get)(
                            out_file, store_folder, preserve=True
                        )
                    except (OSError, SFTPError) as err:
                        sftp_errors.append((out_file, err))
                        self.log.warning(
                            "Cannot download file for task_id=%s from %s: %s",
                            task.task_id,
                            out_file,
                            err,
                        )
                await sftp.rmtree(machine.path(remote_folder)) # uncomment to keep raw files

        try:
            await sftp_get_retry(job)()
        except Exception as err:
            self.log.warning("Cannot scp from %s: %s" % (remote_folder, err))
            sftp_errors.append((remote_folder, err))

        if sftp_errors:
            meta_add.append(("error", {p: str(e) for p, e in sftp_errors}))

        new_meta = dict(list(task.metadata.items()) + meta_add)
        if "error" in new_meta:
            await self.db.set_task_error(task.task_id, new_meta)
            await self.do_task_webhook(task.task_id, new_meta, TaskStatus.DONE)
        else:
            await self.db.set_task_done(task.task_id, new_meta)
            await self.do_task_webhook(task.task_id, new_meta, TaskStatus.DONE)
        await self.db.commit()
        self.log.info(
            "task_id=%s %s done and saved in %s"
            % (task.task_id, task.label, store_folder)
        )
        self.clouds.mark_task_done(task.task_id)

    #
    # Background workers
    #

    async def print_stats(self):
        "Print usage statistics to the log"
        while not self.cancellation_event.is_set():
            end_time = datetime.now() + timedelta(seconds=10)
            ncounters = await self.db.count_nodes_by_status()
            tcounters = await self.db.count_tasks_by_status()
            tmpl = (
                "THREADS: {tasks} "
                "NODES: busy:{n_busy}/enabled:{n_enabled}/total:{n_total} "
                "TASKS: run:{t_run}/todo:{t_todo}/done:{t_done}"
            )
            msg = tmpl.format(
                tasks=len(asyncio.all_tasks()),
                n_busy=len(self.remote_machines.filter(busy=True).keys()),
                n_enabled=ncounters[True],
                n_total=sum(ncounters.values()),
                t_run=tcounters[TaskStatus.RUNNING],
                t_todo=tcounters[TaskStatus.TO_DO],
                t_done=tcounters[TaskStatus.DONE],
            )
            self.log.info(msg)

            queues = [
                self.conn_machine_q,
                self.allocate_q,
                self.deallocate_q,
                self.consume_q,
            ]
            qmsgs = [f"{q.name}: {q.psize()}/{q.qsize()}" for q in queues]
            self.log.info("QUEUES: %s" % " ".join(qmsgs))
            await asleep_until(end_time)

    async def connect_machine_producer(
        self,
    ) -> AsyncGenerator[UMessage[str, NodeModel], None]:
        """Produce messages with new machines for connecting"""
        enabled_nodes = await self.db.get_enabled_nodes()
        new_nodes = [
            n for n in enabled_nodes if n.ip not in self.remote_machines.keys()
        ]
        for node in new_nodes:
            yield UMessage(node.ip, node)

    async def connect_machine_consumer(self, msg: UMessage[str, NodeModel]):
        """Connect to machines"""
        node = msg.payload
        keys = await asyncio.get_running_loop().run_in_executor(
            None, self.config.local.get_private_keys
        )

        jump_host = self.config.remote.jump_host
        jump_username = self.config.remote.jump_username
        for cloud in self.config.clouds:
            if cloud.prefix == node.cloud:
                if cloud.jump_host and cloud.jump_username:
                    jump_host, jump_username = cloud.jump_host, cloud.jump_username

        try:
            self.remote_machines[node.ip] = await RemoteMachine.create(
                host=node.ip,
                username=node.username,
                client_keys=keys,
                logger=self.log,
                data_dir=self.config.remote.data_dir,
                engines_dir=self.config.remote.engines_dir,
                tasks_dir=self.config.remote.tasks_dir,
                connect_timeout=10,
                jump_username=jump_username,
                jump_host=jump_host,
            )
        except asyncssh.misc.Error as err:
            self.log.error(f"Can't connect to machine with error: {err}")
        except Exception as err:
            self.log.error(f"An error occuried on remote machine creation: {err}")

    async def allocator_producer(
        self,
    ) -> AsyncGenerator[UMessage[int, TaskModel], None]:
        """Produce messages with todo tasks to run"""
        ccap = await self.clouds_get_capacity()
        tlim = max(ccap, len(self.remote_machines.filter(busy=False)), 10)
        tasks = await self.db.get_tasks_by_status((TaskStatus.TO_DO,), tlim)
        if tasks:
            ids = [str(t.task_id) for t in tasks]
            self.log.debug("Want allocate tasks: %s" % ", ".join(ids))
        for task in tasks:
            yield UMessage(task.task_id, task)

    @backoff.on_exception(backoff.fibo, AllSSHRetryExc, max_time=60)
    async def allocator_consumer(self, msg: UMessage[int, TaskModel]):
        """Allocate task to node or allocate new node"""
        await self.allocate_task(msg.payload)

    async def task_consumer_producer(
        self,
    ) -> AsyncGenerator[UMessage[int, TaskModel], None]:
        """Produce messages with running tasks for consuming"""
        tasks = await self.db.get_tasks_by_status((TaskStatus.RUNNING,))
        for task in tasks:
            yield UMessage(task.task_id, task)

    async def task_consumer_consumer(
        self, msg: UMessage[int, TaskModel], machine_not_found: Counter
    ):
        """Consume running task if done, mark failed if machine is gone"""
        broken_tasks_passes = 20
        task_id, task = msg.id, msg.payload
        machine = self.remote_machines.get(task.ip)
        if not machine:
            self.log.warning(f"Task {task_id} - machine {task.ip} is gone")
            machine_not_found.update([task_id])
            if machine_not_found[task_id] > broken_tasks_passes:
                await self.db.set_task_error(
                    task_id, metadata=task.metadata, error="node is gone"
                )
                await self.do_task_webhook(task_id, task.metadata, TaskStatus.DONE)
            return
        # if machine state is unknown
        if machine.meta.busy is None:
            engine = self.config.engines.get(task.metadata["engine"])
            if engine:
                await machine.start_occupancy_check(engine)
        # consume
        if not machine.meta.busy:
            self.log.debug(f"machine {machine.hostname} is free for task {task_id}")
            await self.consume_task(machine, task)

    async def deallocator_producer(
        self,
    ) -> AsyncGenerator[UMessage[str, NodeModel], None]:
        """Produce messages with nodes for deallocation"""

        # (I) disable idle nodes without linked running tasks
        tasks = await self.db.get_tasks_by_status((TaskStatus.RUNNING,))
        busy_ips = [t.ip for t in tasks]
        all_enabled_nodes = {
            n.ip: n for n in await self.db.get_enabled_nodes() if n.ip not in busy_ips
        }
        for ccfg in self.config.clouds:
            tdlim = timedelta(seconds=ccfg.idle_tolerance)
            idlers = self.remote_machines.filter(
                busy=False, reverse_sort=False, free_since_gt=tdlim
            )
            nodes_to_disable = [
                ip
                for ip, node in all_enabled_nodes.items()
                if node.cloud == ccfg.prefix and ip in idlers.keys()
            ]
            for ip in nodes_to_disable:
                await self.db.disable_node(ip)
                await self.db.commit()

        # (II) deallocate all disabled nodes without linked running tasks
        free_disabled_nodes = [
            node
            for node in await self.db.get_disabled_nodes()
            if node.ip not in busy_ips and "." in node.ip
        ]
        to_disconnect = [
            n.ip for n in free_disabled_nodes if n.ip in self.remote_machines.keys()
        ]
        await self.remote_machines.disconnect_many(to_disconnect)
        for node in free_disabled_nodes:
            yield UMessage(node.ip, node)

    async def deallocator_consumer(self, msg: UMessage[str, NodeModel]):
        """Consume running task if done, mark failed if machine is gone"""
        node = msg.payload
        if not node.cloud:
            return
        await self.clouds.deallocate(node.ip)

    async def create_producer_consumers(
        self,
        queue: UniqueQueue[TUMsgId, TUMsgPayload],
        producer: Callable[[], AsyncGenerator[UMessage[TUMsgId, TUMsgPayload], None]],
        consumer: Callable[[UMessage[TUMsgId, TUMsgPayload]], Awaitable],
        workers_num: int = 1,
    ) -> None:
        async def worker():
            while not self.cancellation_event.is_set():
                msg = await queue.get()
                try:
                    await consumer(msg)
                finally:
                    queue.item_done(msg)

        workers: Set[asyncio.Task] = set()
        [workers.add(asyncio.create_task(worker())) for _ in range(0, workers_num)]

        try:
            while not self.cancellation_event.is_set():
                end_time = datetime.now() + timedelta(seconds=self.sleep_interval)
                try:
                    async for msg in producer():
                        await queue.put(msg)
                finally:
                    await asleep_until(end_time)

        except asyncio.CancelledError:
            if not queue.empty():
                self.log.info(f"Queue {queue.name} has {queue.qsize()} items - waiting")
                await queue.join()
            [task.cancel() for task in workers]
            await asyncio.gather(*workers, return_exceptions=True)

    #
    # Lifecycle
    #

    async def start(self):
        self.log.debug(
            "Available computing engines: %s" % ", ".join(self.config.engines.keys())
        )

        self.bg_jobs.add(asyncio.create_task(self.print_stats()))

        conn_machine_co = self.create_producer_consumers(
            queue=self.conn_machine_q,
            producer=self.connect_machine_producer,
            consumer=self.connect_machine_consumer,
            workers_num=self.config.local.conn_machine_limit,
        )
        self.bg_jobs.add(asyncio.create_task(conn_machine_co))

        # wait some connected machines before allocation
        async def wait_some_machines():
            while not len(self.remote_machines):
                await asyncio.sleep(1)

        await asyncio.wait(
            [wait_some_machines(), asyncio.sleep(30)], return_when="FIRST_COMPLETED"
        )

        allocate_co = self.create_producer_consumers(
            queue=self.allocate_q,
            producer=self.allocator_producer,
            consumer=self.allocator_consumer,
            workers_num=self.config.local.allocate_limit,
        )
        self.bg_jobs.add(asyncio.create_task(allocate_co))

        machine_not_found = Counter()
        consume_co = self.create_producer_consumers(
            queue=self.consume_q,
            producer=self.task_consumer_producer,
            consumer=partial(
                self.task_consumer_consumer, machine_not_found=machine_not_found
            ),
            workers_num=self.config.local.consume_limit,
        )
        self.bg_jobs.add(asyncio.create_task(consume_co))

        deallocate_co = self.create_producer_consumers(
            queue=self.deallocate_q,
            producer=self.deallocator_producer,
            consumer=self.deallocator_consumer,
            workers_num=self.config.local.deallocate_limit,
        )
        self.bg_jobs.add(asyncio.create_task(deallocate_co))

        await asyncio.gather(*self.bg_jobs, return_exceptions=True)
        await asyncio.sleep(1)  # workaround aiohttp's Unclosed client session

    async def stop(self):
        self.log.info("Stopping...")
        self.cancellation_event.set()

        for task in self.bg_jobs:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self.clouds.stop()
        await self.remote_machines.disconnect_all()
        await self.http.close()
