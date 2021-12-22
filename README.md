Yet another computing scheduler & cloud orchestration engine
==========

**Yascheduler** is a simple job scheduler designed for submitting scientific calculations and copying back the results from the computing clouds.

Currently it has been used for the _ab initio_ [CRYSTAL](http://www.crystal.unito.it) code, although any other scientific code can be supported via the declarative control template system (see `yascheduler.conf` settings file). An example dummy C++ code with its configuration template is included.


Installation
------------
Installation by `pip` is preferred (clone the repo first before it gets on PyPI):
```
    git clone https://github.com/tilde-lab/yascheduler.git
    pip install yascheduler/
```
The installation procedure creates the configuration file located at `/etc/yascheduler/yascheduler.conf`.
The file contains credentials for Postgres database access, used directories, cloud providers and scientific simulation codes (called _engines_).
Please check and amend this file with the correct credentials. The database and the system service should then be initialized with `yainit` script.


Usage
------------

```python
from configparser import ConfigParser

from yascheduler import CONFIG_FILE
from yascheduler.scheduler import Yascheduler

config = ConfigParser()
config.read(CONFIG_FILE)
yac = Yascheduler(config)

label = 'test assignment'
engine = 'pcrystal'
struct_input = str(...) # simulation control file: crystal structure
setup_input = str(...) # simulation control file: main setup, can include struct_input

result = yac.queue_submit_task(label, {'fort.34': struct_input, 'INPUT': setup_input}, engine)
print(result)
```


AiiDA integration
------------

See the detailed instructions for the [MPDS-AiiDA-CRYSTAL workflows](https://github.com/mpds-io/mpds-aiida) as well as the [ansible-mpds](https://github.com/mpds-io/ansible-mpds) repository. In essence:
```
    pip install --upgrade paramiko
    ssh aiidauser@localhost # important
    reentry scan
    verdi computer setup
    verdi computer test $COMPUTER
    verdi code setup
```
