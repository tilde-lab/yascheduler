## v3.0.0 (2026-10-08)

### BREAKING CHANGE

- database schema change

### Feat

- yascheduler 2.0.0
- **cloud**: vastai select_filters + CancellationError
- **cloud**: vultr non root user owns /data
- **cloud**: label is configurable for every cloud
- **cloud**: tolerate permission denied and other ssh error on setup_vm until connect_grace
- **cloud**: hetzner non-root user
- **cloud**: non-root user
- **cloud**: vultr setup for fleur
- **cloud**: vultr cloud adapter
- **cli**: View respects engine.output_files instead of hardcoded OUTPUT
- **vastai**: really working vastai adapter
- **cloud**: hetzner real external_id use
- **cloud**: create node DTO with external_id and all connection fields
- **config**: jump port config
- **node**: owns connection identity
- new node fields
- **persistence**: strong state check for task
- **persistence**: serial->identity
- task-by-id
- node-by-id implementation started
- **persistence**: db migrations
- debian-13 default
- **domain**: TaskContext.replace helper method
- **domain**: Task.with_context helper method
- **domain**: Task.with_event helper method
- domain events
- **cloud**: adapter init
- structured debug logs
- **ssh**: ssh adapter
- **application**: new layer
- **persistence**: postgres repositories, UoW
- **domain**: domain layer

### Fix

- **doimain**: restore dict-like behaviour of EngineRepository
- **cloud**: match vultr ssh keys by public key
- **infra**: check migration status and propose solutions
- **application**: static node not removed on --remove-soft
- **application**: do not delete node if abandone node failed to deallocate
- **application**: misconfiguration error: engine removed from config, but running task exist
- **cloud**: add guard against empty external_id
- **cloud**: HetznerError instead of RuntimeError
- import from typing_extensions
- **ssh**: timeout arg type
- py39 compat
- typo
- **cli**: log file roration fix#93
- **cli**: yasetnode warns when local dir == remote dir for localhost node fix#115
- **ssh**: windows list processes silent error swallow
- **ssh**: create keys_dir fix#100
- **ssh**: skip public keys, don't die on key import error
- py39 compat
- **ssh**: apt-get transient errors fix #133
- **allocate_task**: rollback on double allocation (that is unreal situation)
- **cloud**: vastai instance status polling retry on transient errors
- **cloud**: catch BaseExceptions in create_node
- **persistence**: migration 004 >1 rows in backfill
- **persistence**: migration 003 wrong statements order
- **orchestrator**: shutdown hang
- **cloud**: upcloud create without reconcile
- **cloud**: upcloud permanent hang
- **cloud**: azure clean up vm and nic if create failed
- **config**: default user for azure
- **ssh**: windows latent path bug
- **deallocate_nodes**: recheck busy
- remove litter
- **config**: warn on absent [db] password
- **ssh**: stop using private method
- **persistence**: update silently succeeded on zero rows
- **cloud**: op_limit semaphore never acquired
- **allocate_task**: status guarged save
- **persistence**: task todo->running status transition guard
- **cloud**: tolerate non-fatal cloud-init errors
- **cloud**: py39 compat
- **cloud**: setup sudo
- **cloud**: cloud-init observability
- **cloud**: vastai API error
- **cloud**: vastai tuning
- **cloud**: api urls
- **cloud**: more vultr fixes
- **cloud**: more vastai fixes
- **cloud**: more hetzner fixes
- **cloud**: more vastai fixes
- **cloud**: more vultr fixes
- **cloud**: more hetzner fixes
- CancellationErrors
- **cloud**: vultr cancellation exception
- **cloud**: hetzner orphaned node prevention
- **entrypoints**: hetzner location type
- **entrypoints**: exception message rewording
- **cloud**: more guards against orphaned nodes for vastai
- **cloud**: more guards against orphaned nodes for vultr
- **cloud**: vastai really-really no orphaned nodes
- **cloud**: hetzner really-really no orphaned nodes
- **cloud**: vultr really-really no orphaned nodes
- **application**: dead cleanup condition
- **ssh**: engine path quoting
- **dev**: don't overwrite remote.user
- **cloud**: hetzner hardcoded connect_grace
- **cloud**: vultr silent fail
- **cloud**: debian duke adapter version
- **orchestrator**: disabled node with running task on restart
- **ssh**: pgrep
- **cloud**: vultr orphaned nodes
- **dev**: proper key name
- update .gitignore
- **ssh**: log error on early non-0 exit of engine
- **config**: warn on not exist keys_dir
- **dev**: config rewrite
- **cloud**: not eager imports of adapters
- **config**: empty credentials are allowed
- **dev**: setup node
- **ssh**: pgrep
- **ssh**: spawn trace
- **shared**: py3.9 compat
- remove litter
- **shared**: py3.10 compat
- **shared**: py3.10 compat
- **node**: ncpus as config
- tracker node link leak
- **ssh**: <py3.12 compat
- **allocate**: wrong occupy message
- **orchestrator,cloud**: cloud alloc session lifecycle
- **orchestrator**: stats printing when no nodes
- **cli**: -l flag
- **orchestrator**: static node connect exclusion
- **persistence**: save silent zero rows
- **queue**: race condition
- **ssh**: write remote file swallow
- **ssh**: nonidempotent ssh retries
- **orchestrator**: daemon resource leak on start return
- **orchestrator**: silent death
- **ssh**: download rmtree data loss
- **orchestrator**: never connected node leak
- **orchestrator**: fix disconnect bg task leak
- **cloud**: double ssh gateway
- py39 compat
- **tests**: py39 compat
- **tests**: py39 compat
- **domain**: py39 compat
- **tests**: mock leak
- **cloud**: py39 compat
- **adapters**: py39 compat
- ruff format
- add prefix vastai for cloud enigne config in yascheduler cloud
- py39 compat
- **orchestrator**: only first task awaited
- type errors
- **orchestrator**: not closed aiohttp
- **domain**: wrong exception raised
- **sql**: schema.sql dedup
- **tests**: run only unit tests by default
- **tests**: correct test marking
- **config**: wrong ini file keys in remote config
- add importlib.metadata fallback
- **markdownlint**: ignore machine-generated documentation

### Refactor

- **ssh**: remove unused argument
- **persistence**: remove dead code
- **config**: remove dead code
- **cloud**: unused variable
- **cli**: unused argument
- reuse constant
- **ssh,cloud,config**: drop OS version checks, check only families
- **ssh**: convert some protocols to just type aliases
- **domain**: remove dead code
- **domain**: Task model typeset
- **config**: simplify + single source of truth for defaults
- **cloud**: vastai stand alone client
- **cloud**: hetzner stand alone client
- **cloud**: vultr stand alone client
- **cloud**: hetzner via aiohttp:
- **cloud**: vultr typed api
- **cloud**: vultr use new cloud init fields
- **log**: stabilize format and centralize logging setup
- __all__ everywhere
- remove litter
- **shared**: new retry module and drop backoff library dependency
- **cloud**: drop dead code
- semantic code markup
- strict linting
- trace logging
- trace logging
- **domain**: connected machine runtime only
- **task**: valid state transitions
- **ssh**: dissolve machine operations
- **task**: persistence
- **task**: schema and entity cleanup
- **cloud**: port node arg
- **cloud**: simplify cloud connect node args
- **ssh**: rekey node id
- task allocated node id
- more node_id instead of ip
- more node_id instead of ip
- more node_id instead of ip
- **ssh**: decompose gateway again
- **ssh**: remove dead code
- **ssh**: decompose gateway
- **ssh**: yagni protocol
- **cloud**: yagni port
- **entrypoints**: eliminate casts
- **config**: split
- **cloud**: migrate from attrs
- **shared**: prune shared kernel
- **ssh**: remote attrs dependency
- **entrypoints**: relocate di to entrypoints
- **application**: drop attrs from queue
- consolidate daemon entrypoints
- relocate daemon commands
- relocate yastatus command
- relocate yanodes command
- collapse provider selection YAGNI
- move yastatus command to entrypoints
- move yanodes command to entrypoints
- move init command to entrypoints
- move daemon entrypoints to layer
- move aiida plugin to entrypoints layer
- entrypoints layer
- top level modules to layers
- top level modules to layers
- adapters->infra layer
- shared layer
- remove legacy db.py
- **di**: drop dead code
- **ssh**: split long function
- client use UoW without lazy importsx
- client use UoW
- cloud manager's domain exceptions
- cloud manager to use UoW
- remove legacy scheduler
- ssh gateway
- imports chaos
- **persistence**: schema init
- **cli**: to adapters
- **adapters**: remove old remote-machine and clouds modules
- **cloud**: integrate VastAI into new adapter structure
- **application**: use UoW instead of DB
- **persistence**: named exception for UoW

## v1.8.0 (2026-07-15)

## v1.6.1 (2026-07-11)

## v1.6.0 (2026-04-13)

### Feat

- connection with custom port

### Fix

- py39 compat
- db.add_node misuse + types

## v1.5.1 (2026-01-14)

### Fix

- **README.md**: typo

## v1.5.0 (2025-05-23)

### Feat

- add yascheduler.conf to .gitignore

### Fix

- typo
- **clouds**: dynamic loading of only needed cloud adapters

### Refactor

- more simple python 3.9 upgrades
- typing things

### Perf

- more small lazy loading tricks

## v1.4.0 (2025-05-14)

### Feat

- **hetzner, config**: ability to set server location

## v1.3.2 (2025-04-04)

### Fix

- Add conditional import of ParamSpec for Python < 3.10
- nested asyncio loops problem

## v1.3.1 (2025-02-13)

### Fix

- **remote_machine**: change node occupancy algo

## v1.3.0 (2025-02-08)

### Feat

- stable tasks ordering + minor polishing

### Fix

- **package**: add backport of importlib
- **package**: one version in one place
- **remote_machine**: too short tuple
- **remote_machin**: non-POSIX linux shell

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
