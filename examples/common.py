
import os
from collections import namedtuple

from ase.data import chemical_symbols

from aiida_crystal.io.d12_write import write_input
from aiida_crystal.io.f34 import Fort34
from aiida_crystal.io.basis import BasisFile


supported_arities = {1: 'unary', 2: 'binary', 3: 'ternary', 4: 'quaternary', 5: 'quinary'}
verbatim_basis = namedtuple("basis", field_names="content, all_electron")

def get_basis_sets(repo_dir):
    """
    Keeps all available BS in a dict
    for convenience
    """
    bs_repo = {}
    for filename in os.listdir(repo_dir):
        if not filename.endswith('.basis'):
            continue

        el = filename.split('.')[0]
        assert el in chemical_symbols
        with open(repo_dir + os.sep + filename, 'r') as f:
            bs_str = f.read().strip()

        # FIXME?
        bs_parsed = BasisFile().parse(bs_str)
        bs_repo[el] = verbatim_basis(content=bs_str, all_electron=('ecp' not in bs_parsed))

    return bs_repo

def get_input(elements, bs_repo, label):
    """
    Generates a test input for a very quick
    (but meaningful) run
    """
    setup = {
        "title": label,
        "scf": {
            "k_points": (8, 16),
            "dft": {"xc": "PBE0", "grid": "XLGRID", "numerical": {"TOLLDENS": 8, "TOLLGRID": 16}},
            "numerical": {"TOLDEE": 9, "MAXCYCLE": 75},
            "post_scf": ["PPAN"]
        },
        "geometry": {
            "optimise": {
                "type": "FULLOPTG",
                "convergence": {"TOLDEE": 9, "MAXCYCLE": 75}
            }
        }
    }
    return write_input(setup, [bs_repo[el] for el in elements])
