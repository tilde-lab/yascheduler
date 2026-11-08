# Configuration File Reference

Yascheduler reads its settings from an INI file (default location and
overrides are described in the [README](../README.md#usage)). This page
documents every section and key.

## Database Configuration `[db]`

Connection to a PostgreSQL database.

- `user`
  The username to connect to the PostgreSQL server with.

- `password`
  The user password to connect to the server with. This parameter is optional

- `host`
  The hostname of the PostgreSQL server to connect with.

- `port`
  The TCP/IP port of the PostgreSQL server instance.
  *Default*: `5432`

- `database`
  The name of the database instance to connect with.
  *Default*: Same as `user`

## Local Settings `[local]`

- `data_dir`
  Path to root directory of local data files.
  Can be relative to the current working directory.
  *Default*: `./data` (but it's always a good idea to set up explicitly!)
  *Example*: `/srv/yadata`

- `tasks_dir`
  Path to directory with tasks results.
  *Default*: `tasks` under `data_dir`
  *Example*: `%(data_dir)s/tasks`

- `keys_dir`
  Path to directory with SSH keys. Make sure it only contains the private keys.
  *Default*: `keys` under `data_dir`
  *Example*: `%(data_dir)s/keys`

- `engines_dir`
  Path to directory with engines repository.
  *Default*: `engines` under `data_dir`
  *Example*: `%(data_dir)s/engines`

- `webhook_url`
  Webhook endpoint notified on task lifecycle events. Empty disables webhooks.
  *Default*: empty

- `webhook_reqs_limit`
  Maximum number of in-flight webhook http requests.
  *Default*: 5

- `conn_machine_limit`
  Maximum number of concurrent SSH connection's `connect` requests.
  *Default*: 10

- `conn_machine_pending`
  Maximum number of pending SSH connection's `connect` requests.
  *Default*: 10

- `allocate_limit`
  Maximum number of concurrent task or node allocation requests.
  *Default*: 20

- `allocate_pending`
  Maximum number of pending task or node allocation requests.
  *Default*: 1

- `consume_limit`
  Maximum number of concurrent task's results downloads.
  *Default*: 20

- `consume_pending`
  Maximum number of pending task's results downloads.
  *Default*: 1

- `deallocate_limit`
  Maximum number of concurrent node deallocation requests.
  *Default*: 5

- `deallocate_pending`
  Maximum number of pending node deallocation requests.
  *Default*: 1

## Remote Settings `[remote]`

- `data_dir`
  Path to root directory of data files on remote node.
  Can be relative to the remote current working directory (usually `$HOME`).
  *Default*: `./data`
  *Example*: `/src/yadata`

- `tasks_dir`
  Path to directory with tasks results on remote node.
  *Default*: `tasks` under `data_dir`
  *Example*: `%(data_dir)s/tasks`

- `engines_dir`
  Path to directory with engines on remote node.
  *Default*: `engines` under `data_dir`
  *Example*: `%(data_dir)s/engines`

- `user`
  Default ssh username.
  *Default*: `root`

- `jump_user`
  Username of default SSH *jump host* (if used).

- `jump_host`
  Host of default SSH *jump host* (if used). These defaults are stamped onto
  the node row once when a node is added (`yasetnode`) and read from the row
  at connect time — they are not re-resolved from INI on each connection.
  Changing `jump_user` / `jump_host` therefore does not affect
  already-registered nodes; re-add the node or `UPDATE yascheduler_nodes`
  to change its jump leg.

- `jump_port`
  Port of default SSH *jump host*.
  *Default*: `22`

## Providers `[clouds]`

All cloud providers settings are set in the `[clouds]` group.
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
  *Default*: different for providers, starting from 120 seconds.

- `*_package_upgrade`
  Per provider cloud-init `package_upgrade` flag on freshly-provisioned VMs.
  Set to `false` to skip the slow `apt-get upgrade` on first boot.
  *Default*: `true`.

- `*_jump_user`
  Username of this cloud SSH jump host (if used).

- `*_jump_host`
  Host of this cloud SSH jump host (if used). Read once at allocation and
  persisted on the node row — not re-read from INI on each connection.

- `*_jump_port`
  Port of this cloud SSH jump host.
  *Default*: `22`

- `*_connect_grace`
  Seconds to wait for a freshly-provisioned node to become SSH-reachable
  before giving up. Per-provider defaults.

- `*_label`
  Label stamped on cloud nodes for identification.
  *Default*: `yascheduler`

### Hetzner

Settings prefix is `hetzner`.

- `hetzner_token`
  API token with Read & Write permissions for the project.

- `hetzner_server_type`
  Server type (size).
  *Default*: `cx52`

- `hetzner_location`
  Location name.
  *Default*: `fsn1`

- `hetzner_image_name`
  Image name for new nodes.
  *Default*: `debian-13`

### Azure

Azure Cloud should be pre-configured for `yascheduler`. See [Azure setup](docs/AZURE.md).

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
  *Default*: `yascheduler-rg`

- `az_user`
  SSH username. `root` is not supported.

- `az_location`
  Default location for resources.
  *Default*: `westeurope`

- `az_vnet`
  Virtual network name.
  *Default*: `yascheduler-vnet`

- `az_subnet`
  Subnet name.
  *Default*: `yascheduler-subnet`

- `az_nsg`
  Network security group name.
  *Default*: `yascheduler-nsg`

- `az_vm_image`
  OS image name.
  *Default*: `Debian`

- `az_vm_size`
  Machine size.
  *Default*: `Standard_B1s`

### UpCloud

Settings prefix is `upcloud`.

- `upcloud_login`
  Username.

- `upcloud_password`
  Password.

### VastAI

VastAI is a GPU marketplace for on-demand GPU instances.
See [VastAI setup](docs/VASTAI.md) for setup instructions.

Settings prefix is `vastai`.

- `vastai_api_key`
  VastAI API key. Get it from [Console](https://vast.ai/console/cli/)

- `vastai_image`
  Docker image to use for instances.
  *Default*: `pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel`

- `vastai_disk_gb`
  Disk space in GB.
  *Default*: `80`

- `vastai_min_vram_mb`
  Minimum VRAM in MB.
  *Default*: `81920` (80 GB)

- `vastai_num_gpus`
  Number of GPUs.
  *Default*: `1`

- `vastai_max_price_per_hr`
  Maximum price per hour in USD.
  *Default*: `1.50`

- `vastai_onstart_script`
  Script to run on instance startup.
  *Default*: empty

- `vastai_docker_options`
  Additional Docker options (e.g., port mappings).
  *Default*: empty
  *Example*: `-p 8384:8384`

### Vultr

Vultr provides **bare-metal** instances suitable for heavy `ab initio`
calculations. This integration uses the Vultr REST API v2 directly via `aiohttp`
(no extra Python dependency is required — `aiohttp` and `asyncssh` are already
core dependencies).

See [Vultr setup](docs/VULTR.md) for details on bare-metal provisioning,
RAID0 NVMe setup, and cloud-init configuration.

Settings prefix is `vultr`.

- `vultr_api_key`
  Vultr API key (required). Create one in the
  [Vultr customer portal](https://my.vultr.com/settings/#settingsapi).

- `vultr_location`
  Datacenter region (Vultr API `region`).
  *Default*: `ams`

- `vultr_server_type`
  Bare-metal plan id (Vultr API `plan`).
  *Default*: `vbm-24c-256gb-amd`

- `vultr_image_name`
  Vultr OS id (integer, sent as `os_id` in the API). For example, `2136` =
  Debian 12 (bookworm), `2284` = Ubuntu 24.04 LTS x64.
  *Default*: `2136`

- `vultr_need_raid`
  Whether cloud-init sets up RAID0 NVMe + `/dev/shm`. Set to `false` for plans
  where NVMe is already the main disk (e.g. `vbm-8c-132gb`).
  *Default*: `true`

- `vultr_max_nodes`
  Maximum number of concurrent Vultr nodes.
  *Default*: `10`

- `vultr_idle_tolerance`
  Seconds of idleness before auto-deletion.
  *Default*: `1800` (30 min)

## Engines `[engine.*]`

Supported engines should be defined in the section(s) `[engine.name]`.
The name is alphanumeric string to represent the real engine name.
Once set, it cannot be changed later.

- `platforms`
  List of supported platform, separated by space or newline.
  No default (empty list if unset).
  *Example*: `debian debian-like`

- `platform_packages`
  A list of required packages, separated by space or newline, which
  will be installed by the system package manager.
  *Default*: []
  *Example*: `openmpi-bin wget`

- `exclude_nodes`
  A list of node hostnames to exclude from task allocation of this
  engine, separated by space or newline. Entries must match the host
  address exactly as registered (`yasetnode`, e.g. an IP).
  *Default*: [] (no nodes excluded)
  *Example*: `10.0.0.7 192.168.1.9`

- `deploy_local_files`
  A list of filenames, separated by space or newline, which will be copied
  from local `%(engines_dir)s/%(engine_name)s` to remote
  `%(engines_dir)s/%(engine_name)s`.
  Mutually exclusive with `deploy_local_archive` and `deploy_remote_archive`.
  *Example*: `dummyengine`

- `deploy_local_archive`
  A name of the local archive (`.tar.gz`) which will be copied
  from local `%(engines_dir)s/%(engine_name)s` to the remote machine and
  then unarchived to the `%(engines_dir)s/%(engine_name)s`.
  Mutually exclusive with `deploy_local_files` and `deploy_remote_archive`.
  *Example*: `dummyengine.tar.gz`

- `deploy_remote_archive`
  The url to the engine arhive (`.tar.gz`) which will be downloaded
  to the remote machine and then unarchived to the
  `%(engines_dir)s/%(engine_name)s`.
  Mutually exclusive with `deploy_local_files` and `deploy_local_archive`.
  *Example*: `https://example.org/dummyengine.tar.gz`

- `spawn`
  This command is used by the scheduler to initiate calculations.

  ```sh
  cp {task_path}/INPUT OUTPUT && mpirun -np {ncpus} --allow-run-as-root \
    -wd {task_path} {engine_path}/Pcrystal >> OUTPUT 2>&1

  ```

*Example*: `{engine_path}/gulp < INPUT > OUTPUT`

- `check_pname`
  Process name used to check that the task is still running.
  Conflicts with `check_cmd`.
  *Example*: `dummyengine`

- `check_cmd`
  Command used to check that the task is still running.
  Conflicts with `check_pname`. See also `check_cmd_code`.
  *Example*: `ps ax -ocomm= | grep -q dummyengine`

- `check_cmd_code`
  Expected exit code of command from `check_cmd`.
  If code matches than task is running.
  *Default*: `0`

- `sleep_interval`
  Interval in seconds between the task checks.
  Set to a higher value if you are expecting long running jobs.
  *Default*: `10`

- `input_files`
  A list of task input file names, separated by a space or new line,
  that will be copied to the remote directory of the task before it is started.
  The first input is considered as the **main** input.
  *Example*: `INPUT sibling.file`

- `output_files`
  A list of task output file names, separated by a space or new line,
  that will be copied from the remote directory of the task after it is finished.
  *Example*: `INPUT OUTPUT`
