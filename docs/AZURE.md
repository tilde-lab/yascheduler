# Azure

You can pre-build an image with all the engines you need. This can be faster
than uploading an engine each time/creating an engine when configuring a node.
There is an example of building such an image at
[examples/own-vm-image/](../examples/own-vm-image/README.md).

## Setup

Azure Cloud should be pre-configured for `yascheduler`.

It is recommended to use
[Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli).
Configure it beforehand.

Run command and write down `subscriptionId` to the config file.

```sh
az account subscription list
```

Create a dedicated Resource Group. See
[documentation](https://docs.microsoft.com/en-us/cli/azure/manage-azure-groups-azure-cli).
For example, consider `yascheduler-rg` in `westeurope` location.
Save the resource group and location to the cloud config.

```bash
az group create -l westeurope -g yascheduler-rg
```

Create a dedicated *Enterprise Application* for service.
See [documentation](https://docs.microsoft.com/en-us/cli/azure/ad/app?view=azure-cli-latest#az-ad-app-create).
Save `appId` as `az_client_id` to the cloud config.

```bash
az ad app create --display-name yascheduler
```

Assign roles *Network Contributor* and *Virtual Machine Contributor*
in the *Resource Group*. Use the correct `appId` and `subscriptionId`:

```bash
az role assignment create \
    --assignee 00000000-0000-0000-0000-000000000000 \
    --scope /subscriptions/<subscriptionId>/resourceGroups/yascheduler-rg \
    --role "Network Contributor"
az role assignment create \
    --assignee 00000000-0000-0000-0000-000000000000 \
    --scope /subscriptions/<subscriptionId>/resourceGroups/yascheduler-rg \
    --role "Virtual Machine Contributor"
```

Create an *Application Registration*.
Add the *Client Secret* to this Application Registration. Use the correct `appId`:

```bash
az ad app credential reset --id 00000000-0000-0000-0000-000000000000 --append
```

Use `tenant` as the `az_tenant_id` and `password` as the `az_client_secret`
cloud settings.

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

According to our experience, while creating the nodes, the Azure allocates the
new public IP-addresses slowly and unwillingly, so we support **the internal
IP-addresses** only. This is no problem, if `yascheduler` is installed in the
internal network. If this is not the case, one has to setup a *jump host*,
allowing connections from the outside:

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

Save the `publicIpAddress` as `az_jump_host`, and `az_jump_user` will be
`yascheduler`. These values are read once at allocation and persisted on the
node row — changing `az_jump_host` / `az_jump_user` in INI does not affect
already-allocated cloud nodes (re-add or `UPDATE yascheduler_nodes` instead).
