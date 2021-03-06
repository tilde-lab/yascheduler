Yet another computational scheduler
==========

**Yascheduler** is a simple job scheduler designed specifically for submitting _ab initio_ calculations and copying back results from computing clouds.

Currently it supports the parallel [CRYSTAL](http://www.crystal.unito.it) code, although adopting to any other _ab initio_ code should be trivial. **Yascheduler** is an integral part of the [MPDS-AiiDA-CRYSTAL workflows](https://github.com/mpds-io/mpds-aiida).


Installation
------------
Installation by `pip` is preferred (clone the repo first before it gets on PyPI):
```
    git clone https://github.com/tilde-lab/yascheduler.git
    pip install yascheduler/
```
The installation procedure creates the configuration file located at `/etc/yascheduler/yascheduler.conf`.
The file contains credentials for Postgres database access as well as several directories. Please check
and amend the file with the correct credentials. The database should then be initialized with `yainit` script.



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
