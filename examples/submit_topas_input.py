#!/usr/bin/env python3
"""Submit TOPAS task"""

from yascheduler import Yascheduler

LABEL = "SrTiO3 XRPD pattern"

PATTERN_REQUEST = """
iters 0
yobs_eqn !x = 0; min 10 max 175 del 0.1

Out_X_Ycalc( calc.xy )

lam
    ymin_on_ymax  0.0001
    la  1.0 lo  1.540596 lh  0.1

str
space_group 221
phase_name SrTiO3
Cubic(@ 4.0)
site O   x  2.00000  y  0.00000  z  0.00000  occ O   1  beq  1
site O   x  0.00000  y  2.00000  z  0.00000  occ O   1  beq  1
site O   x  0.00000  y  0.00000  z  2.00000  occ O   1  beq  1
site Sr  x  2.00000  y  2.00000  z  2.00000  occ Sr  1  beq  1
site Ti  x  0.00000  y  0.00000  z  0.00000  occ Ti  1  beq  1
"""


yac = Yascheduler()
result = yac.queue_submit_task(LABEL, {"calc.inp": PATTERN_REQUEST, "structure.inc": ""}, "topas")
print(LABEL)
print(result)
