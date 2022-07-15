#!/usr/bin/env python3

import asyncio
import os
import sys

from yascheduler.scheduler import Yascheduler

target = os.path.abspath(sys.argv[1])
work_folder = os.path.dirname(target)
setup_input = open(target).read()

try:
    sys.argv[2]
except IndexError:
    folder = None
    print("**To save calc in a local repo**")
else:
    folder = work_folder
    print("**To save calc in an input folder**")

if os.path.exists(os.path.join(work_folder, "fort.34")):
    assert "EXTERNAL" in setup_input
    struct_input = open(os.path.join(work_folder, "fort.34")).read()
else:
    assert "EXTERNAL" not in setup_input
    struct_input = "UNUSED"

label = setup_input.splitlines()[0]


async def main():
    yac = await Yascheduler.create()
    result = yac.create_new_task(
        label,
        {"fort.34": struct_input, "INPUT": setup_input, "local_folder": folder},
        "pcrystal",
    )
    print(label)
    print(result)


asyncio.run(main())
