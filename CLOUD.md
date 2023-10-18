# Cloud Providers

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
