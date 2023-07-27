## v1.2.0 (2023-07-27)

### Feat

- **config**: warn on unknown config fields

## v1.1.0 (2023-07-27)

### Feat

- **remote_machine**: support more OS checks

## v1.0.13 (2023-07-27)

## v1.0.12 (2023-07-27)

### Fix

- **remote_machine_repo**: strict busy check
- **scheduler**: strict comparison with None
- **remote_machine**: skip initialization of unsupported engines

### Refactor

- **scheduler**: simplify engine get on task allocate

## v1.0.11 (2023-07-27)

### Fix

- **scheduler**: python 3.11 incompatibility

## v1.0.10 (2023-04-24)

## v1.0.8 (2023-03-11)

### Fix

- occupancy checks
- pre-commit hook

## v1.0.7 (2022-11-22)

### Feat

- support ancient windows versions
- per-cloud initial ssh connection timeout
- set logging level as argument
- setup linters
- synchronous public client

### Fix

- typo in func name
- workaround python bug in setup.py
- use username from config when node added manually
- remote.user inheritance to the clouds
- user ncpus from db if set
- recover running tasks after daemon restart
- useless check prevents machine occupancy check
- remote config omitted on cloud node allocation
- different event loop on node allocation
- python3.7 incompatibility
- absolute path on windows IS supported
- collect errors on sftp downloads
- less wide SFTP retriable errors
- fail loud when node setup is failed
- cloud init pkgs when engine without platform
- ssh connection options
- chmod 600 ssh priv key
- do not load system ssh_config
- pubkey and fingerprint format
- remote engine's platforms default
- greedy allocation
- synchronize ssh key generation
- upload task when remote path is absolute
- recovery on failed node setup
- don't filter engines on node setup
- max_nodes one more time
- webhook status + error logging
- regression - no queue_get_task
- typo in yascheduler client
- db init
- disable cloud on max_nodes <1
- typo in key generation
- TypeError on 3.10
- setup
- EOF
- chmod
- remove not approved task status (ERROR)

## v0.10.1 (2022-07-14)

### Feat

- reimplement azure
- connect via jump host
- more practical dummy sleep range
- throttle node allocations
- scheduler refactoring
- clouds refactoring
- implement universal RemoteMachine over asyncssh
- check for unknown spawn's placeholders
- plumbum dsl for ssh
- black github action
- update default config
- option to skip setup
- new node setup
- setup node stand alone nodes
- per engine dependencies + CloudConfig struct
- initial Engine class
- webhooks
- threads for (de)allocation of nodes
- **hetzner**: simple cloud-init support
- add providers docs
- file paths from env
- azure cloud provider
- wait for apt cache release
- use `sudo` for non-root users
- override ssh user for cloud provider

### Fix

- tilde-lab#48 with wildcards and fstreams
- true run-in-bg
- port utils
- regression about hetzner ssh keys
- not found mpi command
- pep8
- ssh_user property
- typing
- config loading
- cloud node provision
- sql injection
- typo
- sql typos again
- sql query typo
- typings and formatting
- typings
- azure api versions
- ssh backoff time
- wrong name (az->azure)
- formatting (88 -> 79 line length)

## v0.5.0 (2021-11-17)
