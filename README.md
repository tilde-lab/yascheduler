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

### Providers

All cloud provider settings are set in the `yascheduler.conf` file in the `[cloud]` group. Each provider has its own settings prefix.

These settings are common to all providers:

|               |                                                   |
|---------------|---------------------------------------------------|
| `*_max_nodes` | The maximum number of nodes for a given provider. |
| `*_user`      | Per provider override of `remote.user`            |

#### Hetzner

Create an API token with Read & Write permissions for the project.

Settings prefix is `hetzner`.

|                 |           |
|-----------------|-----------|
| `hetzner_token` | API token |

#### Azure

Azure Cloud should configured for `yascheduler`.

Create a dedicated Enterprise Application for service. Create an Application Registration. Add Client Secret to the Application Registration.

Create a dedicated Resource Group. Assign Roles "Network Contributor" and "Virtual Machine Contributor" in the Resource Group.

Settings prefix is `az`.

|                                  | Req. | Default                 |                                                    |
|----------------------------------|------|-------------------------|----------------------------------------------------|
| `az_tenant_id`                   | X    |                         | Tenant ID of Azure Active Directory                |
| `az_client_id`                   | X    |                         | Application ID                                     |
| `az_client_secret`               | X    |                         | Client Secret value from Application Registration. |
| `az_subscription_id`             | X    |                         | Subscription ID                                    |
| `az_resource_group`              |      | `YaScheduler-VM-rg`     | Resource Group name                                |
| `az_user`                        |      | `yascheduler`           | `root` is not supported                            |
| `az_location`                    |      | `westeurope`            | Default location for resources                     |
| `az_infra_tmpl_path`             |      | `azure_infra_tmpl.json` | Path to deployment template of common parts        |
| `az_infra_param_subnetMask`      |      | 24 (max 250 VMs)        | Subnet mask of VMs network                         |
| `az_infra_param_*`               |      |                         | Any input of deployment template of common parts   |
| `az_vm_tmpl_path`                |      | `azure_vm_tmpl.json`    | Path to deployment template of VM                  |
| `az_vm_param_virtualMachineSize` |      | `Standard_B1s`          | Machine type                                       |
| `az_vm_param_osDiskSize`         |      | `StandardSSD_LRS`       | Root disk type                                     |
| `az_vm_param_imagePublisher`     |      | `debian`                | OS image publisher                                 |
| `az_vm_param_imageOffer`         |      | `debian-10`             | OS image offer                                     |
| `az_vm_param_imageSku`           |      | `10-backports-gen2`     | OS image SKU                                       |
| `az_vm_param_imageVersion`       |      | `latest`                | OS image version                                   |
| `az_vm_param_*`                  |      |                         | Any input of deployment template of VM             |

#### UpCloud

Settings prefix is `upcloud`.

|                    |          |
|--------------------|----------|
| `upcloud_login`    | Username |
| `upcloud_password` | Password |

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
