# Yet another computing scheduler & cloud orchestration engine

[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler?ref=badge_shield)

**Yascheduler** is a simple job scheduler designed for submitting scientific
calculations and copying back the results from the computing clouds.

Currently it supports several scientific simulation codes in chemistry
and solid state physics.
Any other scientific simulation code can be supported via the declarative
control template system (see `yascheduler.conf` settings file).
There is an [example dummy C++ code][dummy-engine] with its configuration template.

## Installation

Use `pip` and PyPI: `pip install yascheduler`.

The last updates and bugfixes can be obtained cloning the repository:

```sh
git clone https://github.com/tilde-lab/yascheduler.git
pip install yascheduler/
```

The installation procedure creates the configuration file located at
`/etc/yascheduler/yascheduler.conf`.
The file contains credentials for Postgres database access, used directories,
cloud providers and scientific simulation codes (called _engines_).
Please check and amend this file with the correct credentials. The database
and the system service should then be initialized with `yainit` script.

## Usage

```python
from yascheduler import Yascheduler

yac = Yascheduler()
label = "test assignment"
engine = "pcrystal"
struct_input = str(...)  # simulation control file: crystal structure
setup_input = str(...)  # simulation control file: main setup, can include struct_input
result = yac.queue_submit_task(
    label, {"fort.34": struct_input, "INPUT": setup_input}, engine
)
print(result)
```

File paths can be set using the environment variables:

- `YASCHEDULER_CONF_PATH`

  Configuration file.

  _Default_: `/etc/yascheduler/yascheduler.conf`

- `YASCHEDULER_LOG_PATH`

  Log file path.

  _Default_: `/var/log/yascheduler.log`

- `YASCHEDULER_PID_PATH`

  PID file.

  _Default_: `/var/run/yascheduler.pid`

## Configuration File Reference

### Database Configuration `[db]`

Connection to a PostgreSQL database.

- `user`

  The username to connect to the PostgreSQL server with.

- `password`

  The user password to connect to the server with. This parameter is optional

- `host`

  The hostname of the PostgreSQL server to connect with.

- `port`

  The TCP/IP port of the PostgreSQL server instance.

  _Default_: `5432`

- `database`

  The name of the database instance to connect with.

  _Default_: Same as `user`

### Local Settings `[local]`

- `data_dir`

  Path to root directory of local data files.
  Can be relative to the current working directory.

  _Default_: `./data`

  _Example_: `/srv/yadata`

- `tasks_dir`

  Path to directory with tasks results.

  _Default_: `tasks` under `data_dir`

  _Example_: `%(data_dir)s/tasks`

- `keys_dir`

  Path to directory with SSH keys.

  _Default_: `keys` under `data_dir`

  _Example_: `%(data_dir)s/keys`

- `engines_dir`

  Path to directory with engines repository.

  _Default_: `engines` under `data_dir`

  _Example_: `%(data_dir)s/engines`

- `webhook_reqs_limit`

  Maximum number of in-flight webhook http requests.

  _Default_: 5

- `conn_machine_limit`

  Maximum number of concurrent SSH connection's `connect` requests.

  _Default_: 10

- `conn_machine_pending`

  Maximum number of pending SSH connection's `connect` requests.

  _Default_: 10

- `allocate_limit`

  Maximum number of concurrent task or node allocation requests.

  _Default_: 20

- `allocate_pending`

  Maximum number of pending task or node allocation requests.

  _Default_: 1

- `consume_limit`

  Maximum number of concurrent task's results downloads.

  _Default_: 20

- `consume_pending`

  Maximum number of pending task's results downloads.

  _Default_: 1

- `deallocate_limit`

  Maximum number of concurrent node deallocation requests.

  _Default_: 5

- `deallocate_pending`

  Maximum number of pending node deallocation requests.

  _Default_: 1

### Remote Settings `[remote]`

- `data_dir`

  Path to root directory of data files on remote node.
  Can be relative to the remote current working directory (usually `$HOME`).

  _Default_: `./data`

  _Example_: `/src/yadata`

- `tasks_dir`

  Path to directory with tasks results on remote node.

  _Default_: `tasks` under `data_dir`

  _Example_: `%(data_dir)s/tasks`

- `engines_dir`

  Path to directory with engines on remote node.

  _Default_: `engines` under `data_dir`

  _Example_: `%(data_dir)s/engines`

- `user`

  Default ssh username.

  _Default_: `root`

- `jump_user`

  Username of default SSH _jump host_ (if used).

- `jump_host`

  Host of default SSH _jump host_ (if used).

### Providers `[clouds]`

All cloud providers settings are set in the `[cloud]` group.
Each provider has its own settings prefix.

These settings are common to all the providers:

- `*_max_nodes`

  The maximum number of nodes for a given provider.
  The provider is not used if the value is less than 1.

- `*_user`

  Per provider override of `remote.user`.

- `*_priority`

  Per provider priority of node allocation.
  Sorted in descending order, so the cloud with the highest value is the first.

- `*_idle_tolerance`

  Per provider idle tolerance (in seconds) for deallocation of nodes.

  _Default_: different for providers, starting from 120 seconds.

- `*_jump_user`

  Username of this cloud SSH jump host (if used).

- `*_jump_host`

  Host of this cloud SSH jump host (if used).

#### Hetzner

Settings prefix is `hetzner`.

- `hetzner_token`

  API token with Read & Write permissions for the project.

- `hetzner_server_type`

  Server type (size).

  _Default_: `cx51`

- `hetzner_image_name`

  Image name for new nodes.

  _Default_: `debian-10`

#### Azure

Azure Cloud should be pre-configured for `yascheduler`. See [Cloud Providers](CLOUD.md).

Settings prefix is `az`.

- `az_tenant_id`

  Tenant ID of Azure Active Directory.

- `az_client_id`

  Application ID.

- `az_client_secret`

  Client Secret value from the **Application Registration**.

- `az_subscription_id`

  Subscription ID

- `az_resource_group`

  Resource Group name.

  _Default_: `yascheduler-rg`

- `az_user`

  SSH username. `root` is not supported.

- `az_location`

  Default location for resources.

  _Default_: `westeurope`

- `az_vnet`

  Virtual network name.

  _Default_: `yascheduler-vnet`

- `az_subnet`

  Subnet name.

  _Default_: `yascheduler-subnet`

- `az_nsg`

  Network security group name.

  _Default_: `yascheduler-nsg`

- `az_vm_image`

  OS image name.

  _Default_: `Debian`

- `az_vm_size`

  Machine size.

  _Default_: `Standard_B1s`

#### UpCloud

Settings prefix is `upcloud`.

- `upcloud_login`

  Username.

- `upcloud_password`

  Password.

#### Engines `[engine.*]`

Supported engines should be defined in the section(s) `[engine.name]`.
The name is alphanumeric string to represent the real engine name.
Once set, it cannot be changed later.

- `platforms`

  List of supported platform, separated by space or newline.

  _Default_: `debian-10`
  _Example_: `mY-cOoL-OS another-cool-os`

- `platform_packages`

  A list of required packages, separated by space or newline, which
  will be installed by the system package manager.

  _Default_: []
  _Example_: `openmpi-bin wget`

- `deploy_local_files`

  A list of filenames, separated by space or newline, which will be copied
  from local `%(engines_dir)s/%(engine_name)s` to remote
  `%(engines_dir)s/%(engine_name)s`.
  Conflicts with `deploy_local_archive` and `deploy_remote_archive`.

  _Example_: `dummyengine`

- `deploy_local_archive`

  A name of the local archive (`.tar.gz`) which will be copied
  from local `%(engines_dir)s/%(engine_name)s` to the remote machine and
  then unarchived to the `%(engines_dir)s/%(engine_name)s`.
  Conflicts with `deploy_local_archive` and `deploy_remote_archive`.

  _Example_: `dummyengine.tar.gz`

- `deploy_remote_archive`

  The url to the engine arhive (`.tar.gz`) which will be downloaded
  to the remote machine and then unarchived to the
  `%(engines_dir)s/%(engine_name)s`.
  Conflicts with `deploy_local_archive` and `deploy_remote_archive`.

  _Example_: `https://example.org/dummyengine.tar.gz`

  _Example_:

  ```sh
  cp {task_path}/INPUT OUTPUT && mpirun -np {ncpus} --allow-run-as-root \
    -wd {task_path} {engine_path}/Pcrystal >> OUTPUT 2>&1

  ```

  _Example_: `{engine_path}/gulp < INPUT > OUTPUT`

- `check_pname`

  Process name used to check that the task is still running.
  Conflicts with `check_cmd`.

  _Example_: `dummyengine`

- `check_cmd`

  Command used to check that the task is still running.
  Conflicts with `check_pname`. See also `check_cmd_code`.

  _Example_: `ps ax -ocomm= | grep -q dummyengine`

- `check_cmd_code`

  Expected exit code of command from `check_cmd`.
  If code matches than task is running.

  _Default_: `0`

- `sleep_interval`

  Interval in seconds between the task checks.
  Set to a higher value if you are expecting long running jobs.

  _Default_: `10`

- `input_files`

  A list of task input file names, separated by a space or new line,
  that will be copied to the remote directory of the task before it is started.

  _Example_: `INPUT sibling.file`

- `output_files`

  A list of task output file names, separated by a space or new line,
  that will be copied from the remote directory of the task after it is finished.

  _Example_: `INPUT OUTPUT`

## Aiida Integration

See the detailed instructions for the [MPDS-AiiDA-CRYSTAL workflows][mpds-aiida]
as well as the [ansible-mpds][ansible-aiida] repository. In essence:

```sh
ssh aiidauser@localhost # important
reentry scan
verdi computer setup
verdi computer test $COMPUTER
verdi code setup
```

[ansible-aiida]: https://github.com/mpds-io/ansible-mpds
[mpds-aiida]: https://github.com/mpds-io/mpds-aiida
[dummy-engine]: https://github.com/tilde-lab/dummy-engine

## License

[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler?ref=badge_large)
