
from itertools import product
import numpy as np
from aiida_crystal.io.f34 import Fort34
from yasubmitter import get_structures
from yascheduler import Yascheduler

ela = ['Li', 'Na', 'K', 'Rb', 'Cs', 'Be', 'Mg', 'Ca', 'Sr', 'Ba']
elb = ['F', 'Cl', 'Br', 'I', 'O', 'S', 'Se', 'Te']

f34_input = Fort34()
yac = Yascheduler()

for pair in product(ela, elb):
    print(pair)
    structures = get_structures(pair, more_query_args=dict(lattices='cubic'))
    structures_by_sgn = {}

    for s in structures:
        structures_by_sgn.setdefault(s.info['spacegroup'].no, []).append(s)

    for sgn_cls in structures_by_sgn:
        # get structures with the minimal number of atoms and find the one with median cell vectors
        minimal_struct = min([len(s) for s in structures_by_sgn[sgn_cls]])
        cells = np.array([s.get_cell().reshape(9) for s in structures_by_sgn[sgn_cls] if len(s) == minimal_struct])
        median_cell = np.median(cells, axis=0)
        median_idx = int(np.argmin(np.sum((cells - median_cell)**2, axis=1)**0.5))
        target_obj = structures_by_sgn[sgn_cls][median_idx]
        struct_inp = f34_input.from_ase(target_obj)
        yac.queue_submit_task(target_obj.info['phase'], dict(structure=str(struct_inp)))