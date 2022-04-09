#!/usr/bin/env python3

import inspect
import queue
import dataclasses
from dataclasses import dataclass
from typing import Any, Dict, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from yascheduler.background_worker import BackgroundWorker


def from_dict_to_dataclass(cls, data: Dict[str, Any]):
    new_dict = {
        key: (
            data.get(key)
            if val.default == val.empty
            else data.get(key, val.default)
        )
        for key, val in inspect.signature(cls).parameters.items()
    }
    return cls(**new_dict)


@dataclass
class WebhookTaskMetadata:
    webhook_url: Optional[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebhookTaskMetadata":
        return from_dict_to_dataclass(cls, data)


@dataclass
class WebhookTask:
    status: int
    metadata: WebhookTaskMetadata

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebhookTask":
        return from_dict_to_dataclass(cls, data)

    def __post_init__(self) -> None:
        if isinstance(self.metadata, dict):
            self.metadata = WebhookTaskMetadata.from_dict(self.metadata)


@dataclass
class WebhookPayload:
    status: int


class WebhookWorker(BackgroundWorker):
    _task_queue: "queue.Queue[WebhookTask]"
    http: requests.Session

    def __init__(
        self,
        task_queue: "queue.Queue[WebhookTask]",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._task_queue = task_queue
        retry_strategy = Retry(total=5, backoff_factor=1)
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.http = requests.Session()
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)

    def do_work(self) -> None:
        try:
            t = self._task_queue.get(False)
        except queue.Empty:
            return

        if not isinstance(t.metadata.webhook_url, str):
            self._task_queue.task_done()
            return

        self._log.info(f"Executing webhook to {t.metadata.webhook_url}")
        payload = WebhookPayload(status=t.status)
        try:
            response = self.http.post(
                url=t.metadata.webhook_url,
                json=dataclasses.asdict(payload),
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            self._log.info(
                f"Webhook to {t.metadata.webhook_url} failed: {str(e)}"
            )

        self._task_queue.task_done()
