#!/usr/bin/env python3

import os
import sys
from configparser import ConfigParser

from mpds_ml_labs.struct_utils import detect_format, refine
from mpds_ml_labs.cif_utils import cif_to_ase

from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler

import spglib

from common import Fort34, get_basis_sets, get_input


structure = open(sys.argv[1]).read()
assert detect_format(structure) == 'cif'
ase_obj, error = cif_to_ase(structure)
assert not error, error

try: symprec = float(sys.argv[2])
except: symprec = 3E-02 # NB needs tuning
print('symprec = %s' % symprec)

label = sys.argv[1].split(os.sep)[-1].split('.')[0] + \
    " " + spglib.get_spacegroup(ase_obj, symprec=symprec)

ase_obj, error = refine(ase_obj, accuracy=symprec, conventional_cell=True)
assert not error, error

yaconfig = ConfigParser()
yaconfig.read(CONFIG_FILE)
yac = Yascheduler(yaconfig)

bs_repo_tzvp = get_basis_sets('./tzvp_RE')
bs_repo_other = get_basis_sets('./hand_made_bs')
bs_repo = {**bs_repo_tzvp , **bs_repo_other}

elements = list(set(ase_obj.get_chemical_symbols()))
f34_input = Fort34([bs_repo[el] for el in elements])
struct_input = str(f34_input.from_ase(ase_obj))
setup_input = get_input(elements, bs_repo, label)

result = yac.queue_submit_task(label, dict(structure=struct_input, input=setup_input))
print(label)
print(result)
