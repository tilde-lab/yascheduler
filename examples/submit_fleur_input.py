#!/usr/bin/env python3
"""Submit Fleur task"""

from pathlib import Path
from time import sleep

from yascheduler import Yascheduler

SI = """
Si bulk
&lattice latsys='cF', a0=1.8897269, a=5.43 /
2
14 0.125 0.125 0.125
14 -0.125 -0.125 -0.125
"""


yac = Yascheduler()
task_id = yac.queue_submit_task(
    "test inpgen",
    {
        "aiida.in": SI,
    },
    "inpgen",
)

while True:
    task = yac.queue_get_task(task_id)
    if task and task.get("status") == yac.STATUS_DONE:
        print(task)
        break
    sleep(1)

inpgen_folder = task.get("metadata", {}).get("local_folder")
assert inpgen_folder
inp_xml_path = Path(inpgen_folder) / "inp.xml"

task_id = yac.queue_submit_task(
    "test fleur",
    {
        "inp.xml": inp_xml_path.read_text(),
    },
    "fleur",
)

while True:
    task = yac.queue_get_task(task_id)
    if task and task.get("status") == yac.STATUS_DONE:
        print(task)
        break
    sleep(1)
