#!/usr/bin/env python3
"""Submit PCrystal task"""

import os
import sys

from yascheduler import Yascheduler

target = os.path.abspath(sys.argv[1])
work_folder = os.path.dirname(target)
with open(target, encoding="utf-8") as f:
    SETUP_INPUT = f.read()

try:
    sys.argv[2]
except IndexError:
    FOLDER = None
    print("**To save calc in a local repo**")
else:
    FOLDER = work_folder
    print("**To save calc in an input folder**")

if os.path.exists(os.path.join(work_folder, "fort.34")):
    assert "EXTERNAL" in SETUP_INPUT
    with open(os.path.join(work_folder, "fort.34"), encoding="utf-8") as f:
        STRUCT_INPUT = f.read()
else:
    assert "EXTERNAL" not in SETUP_INPUT
    STRUCT_INPUT = "UNUSED"

label = SETUP_INPUT.splitlines()[0]


yac = Yascheduler()
result = yac.queue_submit_task(
    label,
    {"fort.34": STRUCT_INPUT, "INPUT": SETUP_INPUT, "local_folder": FOLDER},
    "pcrystal",
)
print(label)
print(result)
