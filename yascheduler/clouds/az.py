#!/usr/bin/env python3
"""
Azure cloud provider implementation.

Azure setup:
- Create a dedicated Resource Group:
  `az group create --location "{location}" --resource-group "{rg_name}"`
- Create a dedicated application for yascheduler service
- Assign roles "Network Contributor" and "Virtual Machine Contributor"
  to the app in the created Resource Group:
  ```
  az role assignment create \
    --assignee "{client_id}" \
    --role "{role_name}" \
    --resource-group "{rg_name}
  ```

Configuration in `yascheduler.conf`:
```
[clouds]
; required:
az_tenant_id = ...
az_client_id = ...
az_client_secret = ...
az_subscription_id = ...
az_resource_group = ...
; optional
az_max_nodes = ...
az_user = ...
; optional inputs for infra deployment (example):
az_infra_param_location = westeurope
; optinal imputs for VM deployment (example):
az_vm_param_location = westeurope
```
"""

import json
import logging
import random
import string
from pathlib import Path
from threading import Lock
from typing import cast, Any, Dict, List, NamedTuple, Optional, Union

from configparser import ConfigParser
from azure.identity import ClientSecretCredential
from azure.mgmt.compute.v2021_07_01 import ComputeManagementClient
from azure.mgmt.compute.v2021_07_01.operations import VirtualMachinesOperations
from azure.mgmt.network.v2020_06_01 import NetworkManagementClient
from azure.mgmt.network.v2020_06_01.models import PublicIPAddress
from azure.mgmt.network.v2020_06_01.operations import (
    PublicIPAddressesOperations,
    NetworkInterfacesOperations,
)
from azure.mgmt.resource.resources.v2021_04_01 import ResourceManagementClient
from azure.mgmt.resource.resources.v2021_04_01.models import (
    Dependency,
    Deployment,
    DeploymentExtended,
    DeploymentMode,
    DeploymentProperties,
    ResourceGroup,
)
from azure.mgmt.resource.resources.v2021_04_01.operations import (
    DeploymentsOperations,
)
from azure.core.exceptions import HttpResponseError

from yascheduler.clouds import AbstractCloudAPI

# Azure SDK is too noisy
for logger_name in [
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity._internal.get_token_mixin",
]:
    logging.getLogger(logger_name).setLevel(logging.ERROR)

infra_deployment_lock = Lock()

RG_LOCATION_MISMATCH_TMPL = (
    "Resource Group '{}' location mismatch: got '{}' instead of '{}'"
)

DeleteRequestsOperations = Union[
    DeploymentsOperations,
    NetworkInterfacesOperations,
    PublicIPAddressesOperations,
    VirtualMachinesOperations,
]


class DeleteRequest(NamedTuple):
    order: int
    operations: DeleteRequestsOperations
    name: str


class AzureRBACError(Exception):
    operation: str = "read"
    resource_type: Optional[str] = None

    def __init__(self, name: str):
        fstr = "Can't {} {} '{}' - access denied. Please, setup RBAC."
        msg = fstr.format(
            self.operation, self.resource_type or "resource", name
        )
        super().__init__(msg)


class AzureRGReadRBACError(AzureRBACError):
    operation = "read"
    resource_type = "Resource Group"


class AzurePubIPReadRBACError(AzureRBACError):
    operation = "read"
    resource_type = "Public IP Address"


class AzureDeploymentCreateRBACError(AzureRBACError):
    operation = "create"
    resuource_type = "Deployment"


class AzureNotFoundError(Exception):
    resource_type: Optional[str] = None

    def __init__(self, name: str):
        fstr = "{} '{}' - not found."
        msg = fstr.format(self.resource_type or "Resource", name)
        super().__init__(msg)


class AzureRGNotFoundError(AzureNotFoundError):
    resource_type = "Resource Group"


class AzurePubIPNotFoundError(AzureNotFoundError):
    resource_type = "Public IP Address"


class AzureCreateError(Exception):
    resource_type: Optional[str] = None

    def __init__(self, name: str):
        fstr = "{} '{}' failed to create."
        msg = fstr.format(self.resource_type or "Resource", name)
        super().__init__(msg)


class AzureDeploymentCreateError(AzureCreateError):
    resource_type = "Deployment"


class AzureCreatedVMPublicIPNotFoundError(Exception):
    def __init__(self):
        super().__init__("VM created without IP in outputs")


class AzureAPI(AbstractCloudAPI):

    name = "az"
    client_id: str
    location: str
    rg_name: str
    resource_client: ResourceManagementClient
    network_client: NetworkManagementClient
    compute_client: ComputeManagementClient
    infra_tmpl_path: Path
    infra_deployment_name_tmpl: str = "{}-infra-deployment"
    vm_tmpl_path: Path
    vm_deployment_name_tmpl: str = "{}-vm-{}-deployment"

    def __init__(self, config: ConfigParser):
        super().__init__(
            max_nodes=config.getint("clouds", "az_max_nodes", fallback=None)
        )
        self.config = config
        self.client_id = config.get("clouds", "az_client_id")
        self.location = config.get(
            "clouds", "az_location", fallback="westeurope"
        )
        self.rg_name = config.get(
            "clouds", "az_resource_group_name", fallback="YaScheduler-VM-rg"
        )
        self.infra_tmpl_path = Path(
            config.get(
                "clouds",
                "az_infra_tmpl_path",
                fallback=Path(__file__).parent.absolute()
                / Path("azure_infra_tmpl.json"),
            )
        )
        self.vm_tmpl_path = Path(
            config.get(
                "clouds",
                "az_vm_tmpl_path",
                fallback=Path(__file__).parent.absolute()
                / Path("azure_vm_tmpl.json"),
            )
        )
        credential = ClientSecretCredential(
            tenant_id=config.get("clouds", "az_tenant_id"),
            client_id=self.client_id,
            client_secret=config.get("clouds", "az_client_secret"),
        )
        subscription_id = config.get("clouds", "az_subscription_id")
        self.resource_client = ResourceManagementClient(
            credential,
            subscription_id,
            api_version="2021-04-01",
        )
        self.network_client = NetworkManagementClient(
            credential, subscription_id
        )
        self.compute_client = ComputeManagementClient(
            credential, subscription_id
        )

    @property
    def ssh_user(self) -> str:
        "Default SSH user for azure"
        ssh_user = super().ssh_user
        if ssh_user == "root":
            self._log.warn("Root user not supported on Azure")
            ssh_user = "yascheduler"
        return ssh_user

    @property
    def cloud_config_data(self) -> Dict[str, Any]:
        "cloud-config for azure"
        my_boot_cmds = [
            # see https://github.com/MicrosoftDocs/azure-docs/issues/82500
            "systemctl mask waagent-apt.service",
        ]
        data = super().cloud_config_data
        data["bootcmd"] = my_boot_cmds + data.get("bootcmd", [])
        return data

    def _get_conf_by_prefix(self, section: str, prefix: str) -> Dict[str, str]:
        "Get part of config by section and prefix as dict"
        filtered = filter(
            lambda x: x[0].startswith(prefix), self.config.items(section)
        )
        prefix_removed = map(
            lambda x: (
                x[0][len(prefix):] if x[0].startswith(prefix) else x[0],
                x[1],
            ),
            filtered,
        )
        return dict(prefix_removed)

    def get_rg(self) -> ResourceGroup:
        "Check Resource Group"
        try:
            rg_result = self.resource_client.resource_groups.get(self.rg_name)
            if self.location != rg_result.location:
                msg = RG_LOCATION_MISMATCH_TMPL.format(
                    self.rg_name, rg_result.location, self.location
                )
                self._log.warning(msg)
            return rg_result
        except HttpResponseError as e:
            code = getattr(e, "error", None) and getattr(e.error, "code", None)
            if code == "AuthorizationFailed":
                raise AzureRGReadRBACError(self.rg_name) from e
            if code == "ResourceGroupNotFound":
                raise AzureRGNotFoundError(self.rg_name) from e
            raise e

    def get_pip(self, name: str) -> PublicIPAddress:
        "Get Public IP Address by name"
        try:
            return self.network_client.public_ip_addresses.get(
                self.rg_name, name
            )
        except HttpResponseError as e:
            code = getattr(e, "error", None) and getattr(e.error, "code", None)
            if code == "AuthorizationFailed":
                raise AzurePubIPReadRBACError(name) from e
            raise AzurePubIPNotFoundError(name) from e

    def create_deployment(
        self, name: str, tmpl: object, params: Dict[str, Any]
    ) -> DeploymentExtended:
        "Create deployment"
        properties = DeploymentProperties(
            mode=DeploymentMode.incremental,
            template=tmpl,
            parameters={k: {"value": v} for k, v in params.items()},
        )
        try:
            res = self.resource_client.deployments.begin_create_or_update(
                resource_group_name=self.rg_name,
                deployment_name=name,
                parameters=Deployment(properties=properties),
            ).result()
            self._log.info(f"Deployment {name} created/updated")
            return res
        except HttpResponseError as e:
            code = getattr(e, "error", None) and getattr(e.error, "code", None)
            if code == "AuthorizationFailed":
                raise AzureDeploymentCreateRBACError(name) from e
            self._log.error(e)
            raise AzureDeploymentCreateError(name) from e

    def create_infra_deployment(self) -> Dict[str, Any]:
        "Create deployment with common infrastructure parts"
        params = self._get_conf_by_prefix("clouds", "az_infra_param_")
        name = self.infra_deployment_name_tmpl.format(self.rg_name)
        with open(self.infra_tmpl_path, "r") as fd:
            tmpl = json.load(fd)
        res = self.create_deployment(name, tmpl, params)
        return res.properties and res.properties.outputs or {}

    def create_vm_deployment(self, infra_outputs) -> Dict[str, Any]:
        "Create deployment with VM parts"
        rnd_id = "".join(
            [random.choice(string.ascii_lowercase) for _ in range(8)]
        )
        name = self.vm_deployment_name_tmpl.format(self.rg_name, rnd_id)
        params = {
            "namePrefix": rnd_id,
            "adminPublicKey": self.public_key,
            "customData": "#cloud-config\n"
            + json.dumps(self.cloud_config_data),
        }
        # inherit params from infra deployment outputs
        inherit_infra_params = [
            "projectName",
            "location",
            "networkSecurityGroupName",
            "virtualNetworkName",
            "subnetName",
        ]
        for k, v in infra_outputs.items():
            if k in inherit_infra_params and type(v) == dict:
                params[k] = v.get("value")
        # load from config
        params.update(self._get_conf_by_prefix("clouds", "az_vm_param_"))

        with open(self.vm_tmpl_path, "r") as fd:
            tmpl = json.load(fd)
        res = self.create_deployment(name, tmpl, params)
        return res.properties and res.properties.outputs or {}

    def create_node(self):
        infra_deployment_lock.acquire()
        try:
            infra_outputs = self.create_infra_deployment()
        finally:
            infra_deployment_lock.release()
        vm_outputs = self.create_vm_deployment(infra_outputs)

        ip_name: Optional[str] = vm_outputs.get("publicIpAddressName", {}).get(
            "value"
        )
        if not ip_name:
            raise AzureCreatedVMPublicIPNotFoundError()
        ip_address = self.get_pip(ip_name).ip_address
        if not ip_address:
            raise AzureCreatedVMPublicIPNotFoundError()

        # wait node up and ready
        self._run_ssh_cmd_with_backoff(
            ip_address, cmd="cloud-init status --wait", max_time=600
        )

        return ip_address

    def _run_del_reqs(self, reqs: List[DeleteRequest]) -> None:
        for req in sorted(reqs, key=lambda x: x[0]):
            self._log.info(f"Removing {req.name}...")
            try:
                req.operations.begin_delete(self.rg_name, req.name).result()
                self._log.info(f"{req.name} removed")
            except Exception as e:
                self._log.info(f"Can't remove {req.name}: {str(e)}")

    def delete_node(self, ip):
        del_reqs: List[DeleteRequest] = []

        # find Public IP Address
        pip_obj = None
        for i in self.network_client.public_ip_addresses.list(self.rg_name):
            i = cast(PublicIPAddress, i)
            if i.ip_address != ip:
                continue
            pip_obj = i
            req = DeleteRequest(
                99, self.network_client.public_ip_addresses, cast(str, i.name)
            )
            del_reqs.append(req)

        if not pip_obj:
            self._log.error(f"Public IP {ip} not found")
            return
        pip_tags = cast(Dict[str, str], pip_obj.tags) or {}

        # find Deployment
        deployment_id = pip_tags.get("DeploymentId")
        if not deployment_id:
            self._log.error("Deployment ID not found in Public IP tags")
            return self._run_del_reqs(del_reqs)

        deployment_name = self.vm_deployment_name_tmpl.format(
            self.rg_name, deployment_id
        )
        try:
            deployment = self.resource_client.deployments.get(
                self.rg_name, deployment_name
            )
            req = DeleteRequest(
                99, self.resource_client.deployments, deployment_name
            )
            del_reqs.append(req)
        except Exception as e:
            self._log.error(
                f"Can't get deployment {deployment_name}: {str(e)}"
            )
            return self._run_del_reqs(del_reqs)

        deps = (
            cast(
                Union[List[Dependency], None],
                deployment.properties and deployment.properties.dependencies,
            )
            or []
        )
        for d in deps:
            if not d.resource_name:
                continue
            if d.resource_type == "Microsoft.Compute/virtualMachines":
                op = self.compute_client.virtual_machines
                del_reqs.append(DeleteRequest(0, op, d.resource_name))
            if d.resource_type == "Microsoft.Network/networkInterfaces":
                op = self.network_client.network_interfaces
                del_reqs.append(DeleteRequest(1, op, d.resource_name))
        return self._run_del_reqs(del_reqs)
