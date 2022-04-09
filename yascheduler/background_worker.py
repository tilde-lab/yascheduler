#!/usr/bin/env python3

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional
from yascheduler.variables import SLEEP_INTERVAL
from yascheduler.time import sleep_until


class BackgroundWorker(threading.Thread):
    _kill: threading.Event
    _log: logging.Logger
    _sleep_interval: float = SLEEP_INTERVAL

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        **kwargs,
    ):
        super().__init__(target=self.run, **kwargs)
        if logger:
            self._log = logger.getChild(self.name)
        else:
            self._log = logging.getLogger(self.name)
        self._kill = threading.Event()

    def stop(self):
        self._log.info("Stopping thread...")
        self._kill.set()

    def do_work(self):
        raise NotImplementedError()

    def run(self):
        self._log.info("Thread started")
        while not self._kill.is_set():
            end_time = datetime.now() + timedelta(seconds=self._sleep_interval)
            self.do_work()
            sleep_until(end_time)
        self._log.info("Thread stopped")
