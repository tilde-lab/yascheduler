# Cloud Providers

You can pre-build an image with all the engines you need. This can be faster
than uploading an engine each time/creating an engine when configuring a node.
There is an example of building such an image at
[examples/own-vm-image/](examples/own-vm-image/README.md)`.

## Azure

### Setup

Azure Cloud should be pre-configured for `yascheduler`.

It is recommended to use [Azure CLI][az_cli_install]. Configure it beforehand.

Run command and write down `subscriptionId` to the config file.

```sh
az account subscription list
```

Create a dedicated Resource Group. See [documentation][az_manage_rg].
For example, consider `yascheduler-rg` in `westeurope` location.
Save the resource group and location to the cloud config.

```bash
az group create -l westeurope -g yascheduler-rg
```

Create a dedicated _Enterprise Application_ for service.
See [documentation][az_app_create].
Save `appId` as `az_client_id` to the cloud config.

```bash
az ad app create --display-name yascheduler
```

Assign roles _Network Contributor_ and _Virtual Machine Contributor_
in the _Resource Group_. Use the correct `appId`:

```bash
az role assignment create \
    --assignee 00000000-0000-0000-0000-000000000000 \
    --resource-group yascheduler-rg \
    --role "Network Contributor"
az role assignment create \
    --assignee 00000000-0000-0000-0000-000000000000 \
    --resource-group yascheduler-rg \
    --role "Virtual Machine Contributor"
```

Create an _Application Registration_.
Add the _Client Secret_ to this Application Registration. Use the correct `appId`:

```bash
az ad app credential reset --id 00000000-0000-0000-0000-000000000000 --append
```

Use `tenant` as the `az_tenant_id` and `password` as the `az_client_secret` cloud settings.

Create virtual networks:

```bash
az network nsg create \
    -g yascheduler-rg -l westeurope \
    -n yascheduler-nsg
az network nsg rule create \
    -g yascheduler-rg --nsg-name yascheduler-nsg \
    --name allow-ssh-rdp --priority 100 \
    --source-address-prefixes '*' \
    --destination-port-ranges 22 3389 \
    --protocol TCP --access Allow
az network vnet create \
    -g yascheduler-rg -l westeurope --nsg yascheduler-nsg \
    --name yascheduler-vnet --address-prefix 10.0.0.0/16 \
    --subnet-name yascheduler-subnet \
    --subnet-prefix 10.0.0.0/22
```

According to our experience, while creating the nodes, the Azure allocates the new public
IP-addresses slowly and unwillingly, so we support **the internal IP-addresses** only.
This is no problem, if `yascheduler` is installed in the internal network.
If this is not the case, one has to setup a _jump host_, allowing connections from the outside:

```bash
az vm create \
    -g yascheduler-rg -l westeurope \
    --name yascheduler-jump-host \
    --image Debian11 \
    --size Standard_B1s \
    --nsg yascheduler-nsg \
    --public-ip-address yascheduler-jump-host-ip \
    --public-ip-address-allocation static \
    --public-ip-sku Standard \
    --vnet-name yascheduler-vnet \
    --subnet yascheduler-subnet \
    --admin-username yascheduler \
    --ssh-key-values "$(ssh-keygen -y -f path/to/private/key)"
```

Save the `publicIpAddress` as `az_jump_host`, and `az_jump_user` will be `yascheduler`.

[az_cli_install]: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli
[az_manage_rg]: https://docs.microsoft.com/en-us/cli/azure/manage-azure-groups-azure-cli
[az_app_create]: https://docs.microsoft.com/en-us/cli/azure/ad/app?view=azure-cli-latest#az-ad-app-create

## Vultr

### Setup

Vultr provides **bare-metal** instances suitable for heavy `ab initio`
calculations (CRYSTAL Seebeck/TDF, FLEUR). This integration uses the Vultr
REST API v2 directly via `urllib` (no extra Python dependency is required).

Create an API key in the [Vultr customer portal][vultr_api_key] and set
`vultr_api_key` in the `[clouds]` section of the config.

The default plan is `vbm-24c-256gb-amd` (AMD EPYC 7443P, 24C/48T, 256 GB RAM,
2x 480 GB SSD + 2x 1.92 TB NVMe) in the `ams` location on Debian 12 (bookworm)
(`vultr_image_name=2136`, the Vultr OS id). Bare-metal provisioning is slow
(up to ~20 minutes), so `create_node_timeout` is set to 1200 s and `op_limit`
to 2.

> **Note:** `vultr_image_name=2136` is Debian 12 (bookworm) x64, better suited
> for headless bare-metal instances. Ubuntu 24.04 LTS x64 is `2284`. The
> cloud-init setup works on both, but Debian 12 is recommended for servers.

### RAID0 and disk setup (`vultr_need_raid`)

The `vultr_need_raid` flag controls whether cloud-init performs RAID0 NVMe
setup. It defaults to `True` (for `vbm-24c-256gb-amd`).

- **`vultr_need_raid = True`** (default, for `vbm-24c-256gb-amd`):
  RAID0 NVMe (`mdadm --create /dev/md0`), mount `/data`, resize `/dev/shm` to
  200G, install `mdadm`. Required because NVMe drives are unformatted on this
  plan.
- **`vultr_need_raid = False`** (for `vbm-8c-132gb` and similar):
  Skip RAID0. NVMe is already the main disk (mounted at `/`). Only `mkdir -p
  /data` is created on the root disk. `/dev/shm` is left at default (50% of
  RAM). No `mdadm` package installed.

In both cases `/data` is available for engines (`/data/engines`), tasks
(`/data/tasks`), and AiiDA work directory (`/data/aiida`).

### Bare-metal setup (automatic via cloud-init)

The following steps from the [Vultr setup README][vultr_setup] are applied
automatically via cloud-init `user_data` on instance creation:

- **`mkdir /data`** — always created (on RAID0 mount or root disk).
- **RAID0 NVMe** (only when `vultr_need_raid = True`) — `mdadm --create
  /dev/md0` from `nvme0n1` + `nvme1n1`, `mkfs.ext4`, mount at `/data`,
  persist via `/etc/fstab`, save to `mdadm.conf` and `update-initramfs -u`.
- **`/dev/shm` 200G** (only when `vultr_need_raid = True`) — required by
  CRYSTAL pproperties for inter-process communication during parallel
  Seebeck/TDF runs.
- **ulimit 65536** — required by FLEUR/CRYSTAL parallel runs that open many
  files simultaneously.
- **ScaLAPACK symlink** — `libscalapack-openmpi.so.2.2 -> .so.2.1` expected
  by FLEUR and CRYSTAL.
- **apt packages** — `openmpi-bin`, `libopenmpi-dev`, `libscalapack-openmpi-dev`,
  `libxml2-dev`, `libblas-dev`, `liblapack-dev`, `build-essential`, `gfortran`,
  `cmake`, `git` (`mdadm` only when `vultr_need_raid = True`), plus
  engine-specific packages from `[engine.*]` `platform_packages`.

Engines are deployed to `/data/engines` (per `[remote] engines_dir`) by the
scheduler after the instance becomes active.

### Cost control

The default plan costs **$0.993/hour**. Nodes are deleted automatically after
`vultr_idle_tolerance` seconds of idleness (default 1800 s = 30 min) by the
existing `deallocator_producer`. Setting `vultr_idle_tolerance` too low may
cause frequent provisioning cycles; too high wastes money. Tune per your
workload pattern.

### Config example

```ini
[clouds]
vultr_api_key = YOUR_VULTR_API_KEY
vultr_location = ams
vultr_server_type = vbm-24c-256gb-amd
vultr_image_name = 2136
vultr_need_raid = True
vultr_max_nodes = 2
vultr_idle_tolerance = 1800
```

### Standalone test script

A standalone script is provided to verify the Vultr API and instance lifecycle
without running the full scheduler:

```sh
export VULTR_API_KEY='your_api_key_here'
python examples/vultr_test.py test
```

This creates a bare-metal instance, waits for it to become active, checks that
the SSH port is open, then deletes it. Use `--keep` to leave the instance
running, and `python examples/vultr_test.py list` / `delete --id <id>` to manage
instances manually.

[vultr_api_key]: https://my.vultr.com/settings/#settingsapi
[vultr_setup]: https://github.com/mpds-io/ab_initio_calculations/blob/main/scripts/vultr/README.md
