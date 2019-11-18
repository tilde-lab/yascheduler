
from itertools import product
from configparser import ConfigParser

import numpy as np
from aiida_crystal.io.f34 import Fort34
from yasubmitter import get_basis_sets, get_structures, get_input
from yascheduler import Yascheduler


# NB use HW ECP for Ba, Cs?
# NB no Rb, Sr, Te due to ECP
ela = ['Li', 'Na', 'K', 'Be', 'Mg', 'Ca']
elb = ['F', 'Cl', 'Br', 'I', 'O', 'S', 'Se']

config = ConfigParser()
config.read('env.ini')

bs_repo = get_basis_sets(config.get('local', 'bs_repo_dir'))
#setup_input = open('INPUT.tpl').read()

f34_input = Fort34()
yac = Yascheduler(config)

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

        struct_input = f34_input.from_ase(target_obj)
        setup_input = get_input(pair, bs_repo, target_obj.info['phase'])

        yac.queue_submit_task(target_obj.info['phase'], dict(structure=str(struct_input), input=setup_input))