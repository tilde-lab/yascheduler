#!/usr/bin/env python3

import os
import sys
import random
from collections import namedtuple
from configparser import ConfigParser

import numpy as np
from mpds_client import MPDSDataRetrieval, APIError
from ase.data import chemical_symbols

from aiida_crystal.io.d12_write import write_input
from aiida_crystal.io.f34 import Fort34

from yascheduler import Yascheduler


supported_arities = {1: 'unary', 2: 'binary', 3: 'ternary', 4: 'quaternary', 5: 'quinary'}
mpds_api = MPDSDataRetrieval()


def get_basis_sets(repo_dir):
    bs_repo = {}
    for filename in os.listdir(repo_dir):
        if not filename.endswith('.basis'):
            continue

        el = filename.split('.')[0]
        assert el in chemical_symbols
        with open(repo_dir + os.sep + filename, 'r') as f:
            bs_repo[el] = f.read().strip()

    return bs_repo


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

    try:
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

    except APIError as ex:
        if ex.code == 204:
            print("No results!")
            return []
        else: raise

    return structures


def get_input(elements, bs_repo, label):
    """
    Generates a test input for very quick
    (but meaningful) run
    """
    setup = {
        "title": label,
        "scf": {
            "k_points": (4, 4),
            "dft": {"xc": ("PBE", "PBE")},
            "numerical": {"TOLDEE": 8, "MAXCYCLE": 50, "TOLINTEG": (6, 6, 6, 6, 12)},
            "post_scf": ["PPAN"]
        },
        "geometry": {
            "optimise": {
                "type": "CELLONLY",
                "convergence": {"TOLDEE": 8, "MAXCYCLE": 50}
            }
        }
    }
    basis = namedtuple("basis", field_names="content")
    basis.content = "\n".join([bs_repo[el] for el in elements])
    return write_input(setup, [basis])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        elements = sys.argv[1:]
    else:
        elements = [random.choice(chemical_symbols[1:]) for _ in range(random.randint(1, 5))]
    elements = list(set(elements))
    print("Elements: %s" % ', '.join(elements))

    config = ConfigParser()
    config.read('env.ini')

    bs_repo = get_basis_sets(config.get('local', 'bs_repo_dir'))

    structures = get_structures(elements)
    structures_by_sgn = {}

    for s in structures:
        structures_by_sgn.setdefault(s.info['spacegroup'].no, []).append(s)

    assert structures_by_sgn
    if len(structures_by_sgn) == 1:
        user_sgn = list(structures_by_sgn.keys())[0]
    else:
        user_sgn = input('Which SG? %s: ' % ' or '.join(map(str, sorted(structures_by_sgn.keys()))))
        user_sgn = int(user_sgn)

    print("%s (SG%s)" % (
        structures_by_sgn[user_sgn][0].get_chemical_formula(empirical=True),
        structures_by_sgn[user_sgn][0].info['spacegroup'].no
    ))

    # get structures with the minimal number of atoms and find the one with median cell vectors
    minimal_struct = min([len(s) for s in structures_by_sgn[user_sgn]])
    cells = np.array([s.get_cell().reshape(9) for s in structures_by_sgn[user_sgn] if len(s) == minimal_struct])
    median_cell = np.median(cells, axis=0)
    median_idx = int(np.argmin(np.sum((cells - median_cell)**2, axis=1)**0.5))
    target_obj = structures_by_sgn[user_sgn][median_idx]

    setup_input = get_input(elements, bs_repo, target_obj.info['phase'])
    #setup_input = open('INPUT.tpl').read()

    f34_input = Fort34()
    struct_input = f34_input.from_ase(target_obj)

    yac = Yascheduler(config)
    yac.queue_submit_task(target_obj.info['phase'], dict(structure=str(struct_input), input=setup_input))
