#!/usr/bin/env python3

import os
import argparse

from yascheduler import Yascheduler


parser = argparse.ArgumentParser()
parser.add_argument("-f", dest="file", action="store", type=str, required=True)
parser.add_argument("-e", dest="engine", action="store", type=str, required=True)
parser.add_argument("-l", dest="localrepo", action="store", type=bool, required=False, default=False)
args = parser.parse_args()

input_data = {}
yac = Yascheduler()
assert args.engine in yac.config.engines

target = os.path.abspath(args.file)
work_folder = os.path.dirname(target)
with open(target, encoding="utf-8") as f:
    MAIN_INPUT = f.read()

if args.localrepo:
    input_data["local_folder"] = None
    print("**To save calc in a local repo**")
else:
    input_data["local_folder"] = work_folder
    print("**To save calc in an input folder**")

input_data[yac.config.engines[args.engine].input_files[0]] = MAIN_INPUT
for inp in yac.config.engines[args.engine].input_files[1:]:
    input_data[inp] = ""

result = yac.queue_submit_task("test calc", input_data, args.engine)
print(result)
