#!/usr/bin/env python3
"""Submit a task with typed input entries and read them back from a query."""

from __future__ import annotations

import base64
import sys
from time import monotonic, sleep

from yascheduler import Yascheduler
from yascheduler.domain import TaskInputDirectory, TaskInputFile

# The default "dummy" engine declares input_files = 1.input 2.input 3.input
# (yascheduler/data/yascheduler.conf); all three must be present. The "basis"
# tree is an extra snapshot entry the engine declarations do not select.
TEXT = "Hello from a text input.\n"
BINARY = bytes(range(256))
NESTED = b"\x00\x01nested binary payload\n"
TASK_TIMEOUT = 300

yac = Yascheduler()
task_id = yac.queue_submit_task(
    "typed input entries",
    {"project": "typed-inputs-demo"},
    "dummy",
    input_entries=[
        TaskInputFile("1.input", TEXT.encode("utf-8")),
        TaskInputFile("2.input", BINARY),
        TaskInputFile("3.input", b"third declared input\n"),
        TaskInputDirectory("basis"),
        TaskInputFile("basis/Si.orb", NESTED),
    ],
)

task = None
deadline = monotonic() + TASK_TIMEOUT
while monotonic() < deadline:
    task = yac.queue_get_task(task_id)
    if task and task["status"] == yac.STATUS_DONE:
        break
    sleep(1)

if task is None or task["status"] != yac.STATUS_DONE:
    sys.exit(f"task {task_id} did not finish within {TASK_TIMEOUT}s")
# A failed task also ends as DONE, with the reason in metadata["error"]. The
# input snapshot below is present either way, so the outcome is checked first.
if task["metadata"].get("error"):
    sys.exit(f"task {task_id} failed: {task['metadata']['error']}")

# metadata keeps the legacy filename/value view: text as str, binary as base64.
assert task["metadata"]["1.input"] == TEXT
assert task["metadata"]["2.input"] == base64.b64encode(BINARY).decode("ascii")
entries = task["input_entries"]
print("input_entries:", [entry["path"] for entry in entries])

entry = next(e for e in entries if e["path"] == "2.input")
content = base64.b64decode(entry["content_base64"])
assert content == BINARY
print("decoded 2.input:", len(content), "bytes, round-trip OK")
