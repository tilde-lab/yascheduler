#!/usr/bin/env python3

import queue
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Dict, List, Optional
from .abstract_cloud_api import AbstractCloudAPI, load_cloudapi
from .. import SLEEP_INTERVAL
from ..background_worker import BackgroundWorker


class CloudWorker(BackgroundWorker):
    _cfg: ConfigParser
    _apis: Dict[str, AbstractCloudAPI]

    def __init__(
        self,
        config: ConfigParser,
        use_apis: List[str],
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._cfg = config

        self._apis = {}
        for api_name in use_apis:
            self._apis[api_name] = load_cloudapi(api_name)(config)
            self._apis[api_name]._log = self._log.getChild(api_name)


@dataclass
class AllocateTask:
    api_name: str
    tmp_ip: str


@dataclass
class AllocateResult:
    api_name: str
    tmp_ip: str
    ip: Optional[str] = None
    provisioned: bool = False
    ncpus: Optional[int] = None


class AllocatorWorker(CloudWorker):
    _task_queue: "queue.Queue[AllocateTask]"
    _result_queue: "queue.Queue[AllocateResult]"

    def __init__(
        self,
        task_queue: "queue.Queue[AllocateTask]",
        result_queue: "queue.Queue[AllocateResult]",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._task_queue = task_queue
        self._result_queue = result_queue

    def do_work(self):
        try:
            t = self._task_queue.get(False)
        except queue.Empty:
            return

        r = AllocateResult(api_name=t.api_name, tmp_ip=t.tmp_ip)

        api = self._apis.get(t.api_name)
        if not api:
            self._log.error(f"Unknown cloud API {t.api_name}")
            self._task_queue.task_done()
            return

        try:
            api.init_key()
            self._log.info("Creating a node...")
            r.ip = api.create_node()
            self._log.info(f"Created: {r.ip}")
            api.setup_node(r.ip)
            self._log.info(f"Provisioned: {r.ip}")
            r.provisioned = True
            self._sleep_interval = SLEEP_INTERVAL
        except Exception as e:
            self._log.error(f"Allocation of {t.tmp_ip} failed: {str(e)}")
            # backoff
            self._sleep_interval = min(self._sleep_interval * 1.3, 60)

        self._result_queue.put(r)
        self._task_queue.task_done()


@dataclass
class DeallocateTask:
    api_name: str
    ip: str


@dataclass
class DeallocateResult:
    ip: str


class DeallocatorWorker(CloudWorker):
    _task_queue: "queue.Queue[DeallocateTask]"
    _result_queue: "queue.Queue[DeallocateResult]"

    def __init__(
        self,
        task_queue: "queue.Queue[DeallocateTask]",
        result_queue: "queue.Queue[DeallocateResult]",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._task_queue = task_queue
        self._result_queue = result_queue

    def do_work(self):
        try:
            t = self._task_queue.get(False)
        except queue.Empty:
            return

        r = DeallocateResult(ip=t.ip)

        api = self._apis.get(t.api_name)
        if not api:
            self._log.error(f"Unknown cloud API {t.api_name}")
            self._task_queue.task_done()
            return

        try:
            api.init_key()
            self._log.info(f"Deleting the {r.ip} node...")
            api.delete_node(r.ip)
            self._log.info(f"Node {r.ip} is deleted")
            self._sleep_interval = SLEEP_INTERVAL
        except Exception as e:
            self._log.error(f"Deallocation of {r.ip} failed: {str(e)}")
            # backoff
            self._sleep_interval = min(self._sleep_interval * 1.3, 60)

        self._result_queue.put(r)
        self._task_queue.task_done()
