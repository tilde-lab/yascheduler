#!/usr/bin/env python3

import logging
import queue
import random
from configparser import NoSectionError
from typing import List, Optional

from .abstract_cloud_api import load_cloudapi
from .workers import (
    AllocateResult,
    AllocateTask,
    AllocatorWorker,
    BackgroundWorker,
    DeallocateResult,
    DeallocateTask,
    DeallocatorWorker,
)
import yascheduler.scheduler

for logger_name in [
    "paramiko.transport",
]:
    logging.getLogger(logger_name).setLevel(logging.ERROR)


class CloudAPIManager(object):
    _log: logging.Logger
    _allocators: List[AllocatorWorker]
    _allocate_tasks: "queue.Queue[AllocateTask]"
    _allocate_results: "queue.Queue[AllocateResult]"
    _deallocators: List[DeallocatorWorker]
    _deallocate_tasks: "queue.Queue[DeallocateTask]"
    _deallocate_results: "queue.Queue[DeallocateResult]"
    yascheduler: "Optional['yascheduler.scheduler.Yascheduler']"

    def __init__(self, config, logger: Optional[logging.Logger] = None):
        if logger:
            self._log = logger.getChild(self.__class__.__name__)
        else:
            self._log = logging.getLogger(self.__class__.__name__)
        self.apis = {}
        self.yascheduler = None
        self.tasks = set()
        active_providers = set()

        try:
            for name, value in dict(config.items("clouds")).items():
                if not value:
                    continue
                active_providers.add(name.split("_")[0])
        except NoSectionError:
            pass

        for name in active_providers:
            if (
                config.getint("clouds", name + "_max_nodes", fallback=None)
                == 0
            ):
                continue
            self.apis[name] = load_cloudapi(name)(config)

        self._log.info(
            "Active cloud APIs: " + (", ".join(self.apis.keys()) or "-")
        )

        self._allocate_tasks = queue.Queue()
        self._allocate_results = queue.Queue()
        allocator_thread_num = int(
            config.get("local", "allocator_threads", fallback=10)
        )
        self._allocators = [
            AllocatorWorker(
                name=f"AllocatorThread[{x}]",
                logger=self._log,
                config=config,
                use_apis=self.apis.keys(),
                task_queue=self._allocate_tasks,
                result_queue=self._allocate_results,
            )
            for x in range(allocator_thread_num)
        ]

        self._deallocate_tasks = queue.Queue()
        self._deallocate_results = queue.Queue()
        deallocator_thread_num = int(
            config.get("local", "deallocator_threads", fallback=2)
        )
        self._deallocators = [
            DeallocatorWorker(
                name=f"DellocatorThread[{x}]",
                logger=self._log,
                config=config,
                use_apis=self.apis.keys(),
                task_queue=self._deallocate_tasks,
                result_queue=self._deallocate_results,
            )
            for x in range(deallocator_thread_num)
        ]

    def stop(self):
        self._log.info("Stopping threads...")
        workers: List[BackgroundWorker] = []
        workers.extend(self._allocators)
        workers.extend(self._deallocators)
        for t in workers:
            t.stop()
        for t in workers:
            t.join()
        self.do_async_work()

    def __bool__(self):
        return bool(len(self.apis))

    def initialize(self):
        assert self.yascheduler
        for cloudapi in self.apis:
            self.apis[cloudapi].yascheduler = self.yascheduler
            self.apis[cloudapi].init_key()

        for t in self._allocators:
            t.start()
        for t in self._deallocators:
            t.start()

    def allocate_node(self) -> str:
        assert self.yascheduler
        c = self.yascheduler.cursor
        active_providers = list(self.apis.keys())
        used_providers = []
        c.execute(
            """
            SELECT cloud, COUNT(cloud)
            FROM yascheduler_nodes
            WHERE cloud IS NOT NULL GROUP BY cloud;
            """
        )
        for row in c.fetchall():
            cloudapi = self.apis.get(row[0])
            if not cloudapi:
                continue
            if row[1] >= cloudapi.max_nodes:
                active_providers.remove(cloudapi.name)
                continue
            used_providers.append((row[0], row[1]))

        if not active_providers:
            self._log.warning("No suitable cloud provides")

        self._log.info("Enabled: %s" % str(active_providers))
        self._log.info("In use : %s" % str(used_providers))

        if len(used_providers) < len(active_providers):
            name = random.choice(
                list(
                    set(active_providers) - set([x[0] for x in used_providers])
                )
            )
        else:
            name = sorted(used_providers, key=lambda x: x[1])[0][0]

        cloudapi = self.apis[name]
        self._log.info("Chosen: %s" % cloudapi.name)

        c.execute(
            """INSERT INTO yascheduler_nodes (ip, enabled, cloud) VALUES (
            'prov' || SUBSTR(MD5(RANDOM()::TEXT), 0, 11),
            FALSE,
            %s
        ) RETURNING ip;""",
            [cloudapi.name],
        )

        t = AllocateTask(api_name=cloudapi.name, tmp_ip=c.fetchone()[0])
        self._allocate_tasks.put(t)
        self.yascheduler.connection.commit()
        return t.tmp_ip

    def allocate(self, on_task):
        if on_task in self.tasks:
            return
        self.tasks.add(on_task)
        self.allocate_node()

    def process_allocated(self):
        assert self.yascheduler
        c = self.yascheduler.cursor
        while not self._allocate_results.empty():
            try:
                r = self._allocate_results.get(False)
            except queue.Empty:
                break

            c.execute("DELETE FROM yascheduler_nodes WHERE ip=%s;", [r.tmp_ip])
            if r.ip and r.provisioned:
                c.execute(
                    """
                    INSERT INTO yascheduler_nodes (ip, ncpus, cloud)
                    VALUES (%s, %s, %s);
                    """,
                    [r.ip, r.ncpus, r.api_name],
                )
            if r.ip and not r.provisioned:
                self.deallocate([r.ip])

            self.yascheduler.connection.commit()
            self._allocate_results.task_done()

    def deallocate(self, ips):
        assert self.yascheduler
        c = self.yascheduler.cursor
        c.execute(
            "UPDATE yascheduler_nodes SET enabled=false WHERE ip IN ('%s');"
            % "', '".join(ips)
        )
        self.yascheduler.connection.commit()
        c.execute(
            """
            SELECT ip, cloud
            FROM yascheduler_nodes
            WHERE cloud IS NOT NULL AND ip IN ('%s');
            """
            % "', '".join(ips)
        )
        for row in c.fetchall():
            t = DeallocateTask(ip=row[0], api_name=row[1])
            self._deallocate_tasks.put(t)

    def process_deallocated(self):
        assert self.yascheduler
        c = self.yascheduler.cursor
        while not self._deallocate_results.empty():
            try:
                r = self._deallocate_results.get(False)
            except queue.Empty:
                break

            c.execute("DELETE FROM yascheduler_nodes WHERE ip=%s;", [r.ip])
            self.yascheduler.connection.commit()
            self._deallocate_results.task_done()

    def do_async_work(self):
        self.process_allocated()
        self.process_deallocated()

    def get_capacity(self, resources):
        n_busy_cloud_nodes = len(
            [item for item in resources if item[3]]
        )  # Yascheduler.queue_get_resources()
        max_nodes = sum(
            [self.apis[cloudapi].max_nodes for cloudapi in self.apis]
        )
        diff = max_nodes - n_busy_cloud_nodes
        return diff if diff > 0 else 0
