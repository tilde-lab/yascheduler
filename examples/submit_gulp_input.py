#!/usr/bin/env python3
"""Submit GULP task"""

from yascheduler import Yascheduler

LABEL = "test Gulp calc"

GULP_INPUT = """
opti conp prop
#
title
srtio3_140
A, A, J, Richard, Catlow parametrization
end
#
cell
5.5178  5.5178  7.81046  90.0000  90.0000  90.0000
frac

Sr core  0.0000 0.5000 0.2500   0.474
Sr shel  0.0000 0.5000 0.2500   1.526
Ti core  0.0000 0.0000 0.0000   39.863
Ti shel  0.0000 0.0000 0.0000  -35.863
O  core  0.0000 0.0000 0.2500   0.389
O  shel  0.0000 0.0000 0.2500  -2.389
O  core  0.2651 0.7651 0.0000   0.389
O  shel  0.2651 0.7651 0.0000  -2.389

space
140
#
buck
Ti shel O shel  877.200  0.38096  9.0  15.0
O  shel O shel  22764.3  0.14900  43.0  15.0
Sr shel O shel  776.840  0.35867  0  15.0
spring
Ti 65974.0 0
spring
O  18.41 0
spring
Sr 11.406 0
#
output arc srtio140_RC.car
"""


yac = Yascheduler()
result = yac.queue_submit_task(LABEL, {"INPUT": GULP_INPUT}, "gulp")
print(LABEL)
print(result)
