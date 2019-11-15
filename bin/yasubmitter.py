#!/usr/bin/env python2

import sys
import random

import numpy as np

from mpds_client import MPDSDataRetrieval, APIError
from mpds_ml_labs.prediction import periodic_elements
from mpds_ml_labs.struct_utils import get_formula
from aiida_crystal.io.f34 import Fort34

from yascheduler import Yascheduler


supported_arities = {1: 'unary', 2: 'binary', 3: 'ternary', 4: 'quaternary', 5: 'quinary'}
mpds_api = MPDSDataRetrieval()


def get_structures(elements, more_query_args=None):
    """
    Given some arbitrary chemical elements,
    get their possible crystalline structures
    """
    assert sorted(list(set(elements))) == sorted(elements) and \
    len(elements) <= len(supported_arities)

    structures = []
    query = {
        "props": "atomic structure",
        "elements": '-'.join(elements),
        "classes": supported_arities[len(elements)] + ", non-disordered"
    }
    if more_query_args and type(more_query_args) == dict:
        query.update(more_query_args)

    for item in mpds_api.get_data(
        query,
        fields={'S': [
            'phase',
            'cell_abc',
            'sg_n',
            'basis_noneq',
            'els_noneq'
        ]}
    ):
        ase_obj = mpds_api.compile_crystal(item, flavor='ase')
        if not ase_obj:
            continue
        ase_obj.info['phase'] = item[0]
        structures.append(ase_obj)

    assert structures
    return structures


if __name__ == "__main__":
    if len(sys.argv) > 1:
        elements = sys.argv[1:]
    else:
        elements = [random.choice(periodic_elements[1:]) for _ in range(random.randint(1, 5))]
    elements = list(set(elements))
    print("Elements: %s" % ', '.join(elements))

    structures = get_structures(elements)
    structures_by_sgn = {}

    for s in structures:
        structures_by_sgn.setdefault(s.info['spacegroup'].no, []).append(s)

    if len(structures_by_sgn) == 1:
        user_sgn = structures_by_sgn.keys()[0]
    else:
        user_sgn = raw_input('Which SG? %s: ' % ' or '.join(map(str, sorted(structures_by_sgn.keys()))))
        user_sgn = int(user_sgn)

    print("%s (SG%s)" % (
        get_formula(structures_by_sgn[user_sgn][0]),
        structures_by_sgn[user_sgn][0].info['spacegroup'].no
    ))

    # get structures with the minimal number of atoms and find the one with median cell vectors
    minimal_struct = min([len(s) for s in structures_by_sgn[user_sgn]])
    cells = np.array([s.get_cell().reshape(9) for s in structures_by_sgn[user_sgn] if len(s) == minimal_struct])
    median_cell = np.median(cells, axis=0)
    median_idx = int(np.argmin(np.sum((cells - median_cell)**2, axis=1)**0.5))
    target_obj = structures_by_sgn[user_sgn][median_idx]

    f34_input = Fort34()
    struct_inp = f34_input.from_ase(target_obj)

    yac = Yascheduler()
    yac.queue_submit_task(target_obj.info['phase'], dict(structure=str(struct_inp)))