"""Azure cloud methods"""

import logging
from pathlib import PurePosixPath
from typing import Dict, Optional, Tuple, cast

from asyncssh.public_key import SSHKey
from attrs import asdict, evolve
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import (
    AzureError,
    IncompleteReadError,
    ServiceRequestTimeoutError,
    ServiceResponseError,
    ServiceResponseTimeoutError,
)
from azure.identity.aio import ClientSecretCredential
from azure.mgmt.compute.v2021_07_01.aio import ComputeManagementClient
from azure.mgmt.compute.v2021_07_01.models import (
    BootDiagnostics,
    DiagnosticsProfile,
    DiskCreateOptionTypes,
    DiskDeleteOptionTypes,
    HardwareProfile,
    ImageReference,
    LinuxConfiguration,
    NetworkProfile,
    OSDisk,
    OSProfile,
    SshConfiguration,
    SshPublicKey,
    StorageProfile,
    VirtualMachine,
)
from azure.mgmt.network.v2020_06_01.aio import NetworkManagementClient
from azure.mgmt.network.v2020_06_01.models import (
    IPAllocationMethod,
    NetworkInterface,
    NetworkInterfaceIPConfiguration,
    TagsObject,
)

from ..config.cloud import AzureImageReference, ConfigCloudAzure
from .protocols import PCloudConfig
from .utils import get_rnd_name

# Azure SDK is too noisy
for logger_name in [
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity.aio._internal.get_token_mixin",
    "msrest.serialization",
]:
    logging.getLogger(logger_name).setLevel(logging.ERROR)


ID_TAG_NAME = "yascheduler_ip"

RETRY_AZURE_ERRORS = (
    ServiceResponseError,
    ServiceRequestTimeoutError,
    ServiceResponseTimeoutError,
    IncompleteReadError,
)
ALL_AZURE_ERRORS = (AzureError,)


async def create_nic(
    log: logging.Logger,
    cfg: ConfigCloudAzure,
    client: NetworkManagementClient,
    vm_name: str,
) -> Tuple[NetworkInterface, str]:
    "Create network interface"
    nic_name = f"{vm_name}-nic"
    ip_config_name = f"{nic_name}-ip-config"
    subnet = await client.subnets.get(
        resource_group_name=cfg.resource_group,
        virtual_network_name=cfg.vnet,
        subnet_name=cfg.subnet,
    )
    log.debug(f"Subnet {subnet.name} found")
    nsg = await client.network_security_groups.get(cfg.resource_group, cfg.nsg)
    log.debug(f"Network security group {nsg.name} found")
    nic_ip_config_params = NetworkInterfaceIPConfiguration(
        name=ip_config_name,
        subnet=subnet,
        private_ip_allocation_method=IPAllocationMethod.DYNAMIC,
    )
    nic_params = NetworkInterface(
        name=nic_name,
        location=cfg.location,
        ip_configurations=[nic_ip_config_params],
        network_security_group=nsg,
    )
    poller = await client.network_interfaces.begin_create_or_update(
        resource_group_name=cfg.resource_group,
        network_interface_name=nic_name,
        parameters=nic_params,
    )
    await poller.wait()
    nic = await poller.result()
    log.debug(f"Network interface {nic.name} created")
    ip_addr = None
    if nic.ip_configurations:
        for ip_conf in nic.ip_configurations:
            ip_addr = ip_conf.private_ip_address
    if not ip_addr:
        raise RuntimeError("Azure VM created but no IP is assigned")
    await client.network_interfaces.update_tags(
        cfg.resource_group,
        cast(str, nic.name),
        parameters=TagsObject(tags={ID_TAG_NAME: ip_addr}),
    )
    return nic, ip_addr


def create_vm_params(
    location: str,
    vm_name,
    vm_image: AzureImageReference,
    vm_size: str,
    nic: NetworkInterface,
    username: str,
    ssh_key: SSHKey,
    tags: Dict[str, str],
    cloud_config: Optional[PCloudConfig] = None,
) -> VirtualMachine:
    """Create VirtualMachine params"""
    img_ref = ImageReference.from_dict(asdict(vm_image))
    pub_key = SshPublicKey(
        path=str(PurePosixPath("/home", username, ".ssh/authorized_keys")),
        key_data=ssh_key.export_public_key("openssh").decode("utf-8"),
    )
    custom_data = None
    if cloud_config:
        my_boot_cmds = [
            # see https://github.com/MicrosoftDocs/azure-docs/issues/82500
            "systemctl mask waagent-apt.service",
        ]
        custom_data = evolve(
            cloud_config, bootcmd=[*my_boot_cmds, *cloud_config.bootcmd]
        ).render_base64()

    return VirtualMachine(
        location=location,
        tags=tags,
        hardware_profile=HardwareProfile(vm_size=vm_size),
        storage_profile=StorageProfile(
            image_reference=img_ref,
            os_disk=OSDisk(
                create_option=DiskCreateOptionTypes.FROM_IMAGE,
                delete_option=DiskDeleteOptionTypes.DELETE,
            ),
        ),
        network_profile=NetworkProfile(network_interfaces=[nic]),
        os_profile=OSProfile(
            computer_name=vm_name[:15],  # max length 15
            admin_username=username,
            custom_data=custom_data,
            linux_configuration=LinuxConfiguration(
                disable_password_authentication=True,
                ssh=SshConfiguration(public_keys=[pub_key]),
            ),
        ),
        diagnostics_profile=DiagnosticsProfile(
            boot_diagnostics=BootDiagnostics(enabled=True)
        ),
    )


async def create_node(
    nmc: NetworkManagementClient,
    cmc: ComputeManagementClient,
    log: logging.Logger,
    cfg: ConfigCloudAzure,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
):
    """Create virtual machine with nic"""
    vm_name = get_rnd_name("yascheduler-vm")
    nic, ip_addr = await create_nic(log=log, cfg=cfg, client=nmc, vm_name=vm_name)
    vm_params = create_vm_params(
        location=cfg.location,
        vm_name=vm_name,
        vm_image=cfg.vm_image,
        vm_size=cfg.vm_size,
        nic=nic,
        username=cfg.username,
        ssh_key=key,
        tags={ID_TAG_NAME: ip_addr},
        cloud_config=cloud_config,
    )

    poller = await cmc.virtual_machines.begin_create_or_update(
        resource_group_name=cfg.resource_group,
        vm_name=get_rnd_name("yascheduler-vm"),
        parameters=vm_params,
    )
    await poller.wait()
    vm_res = await poller.result()
    log.debug(f"VM {vm_res.name} created")
    return ip_addr


async def az_create_node(
    log: logging.Logger,
    cfg: ConfigCloudAzure,
    key: SSHKey,
    cloud_config: Optional[PCloudConfig] = None,
) -> str:
    """Create virtual machine with network interface"""
    async with ClientSecretCredential(
        cfg.tenant_id, cfg.client_id, cfg.client_secret
    ) as cred:
        cred = cast(AsyncTokenCredential, cred)  # fix library type errors
        async with NetworkManagementClient(cred, cfg.subscription_id) as nmc:
            async with ComputeManagementClient(cred, cfg.subscription_id) as cmc:
                return await create_node(nmc, cmc, log, cfg, key, cloud_config)


async def delete_node(
    nmc: NetworkManagementClient,
    cmc: ComputeManagementClient,
    log: logging.Logger,
    cfg: ConfigCloudAzure,
    host: str,
):
    """Delete virtual machine with network interface"""
    async for result in cmc.virtual_machines.list(cfg.resource_group):
        vm_res = cast(VirtualMachine, result)
        tag_ip = (vm_res.tags or {}).get(ID_TAG_NAME)
        if tag_ip == host:
            poller = await cmc.virtual_machines.begin_power_off(
                cfg.resource_group, cast(str, vm_res.name)
            )
            await poller.wait()

            poller = await cmc.virtual_machines.begin_delete(
                cfg.resource_group, cast(str, vm_res.name)
            )
            await poller.wait()
            log.debug(f"VM {vm_res.name} deleted")
            break

    nic = None
    async for result in nmc.network_interfaces.list(cfg.resource_group):
        nic = cast(NetworkInterface, result)
        tag_ip = (nic.tags or {}).get(ID_TAG_NAME)
        if tag_ip == host:
            poller = await nmc.network_interfaces.begin_delete(
                cfg.resource_group, cast(str, nic.name)
            )
            await poller.wait()
            log.debug(f"Network interface {nic.name} deleted")
            break


async def az_delete_node(
    log: logging.Logger,
    cfg: ConfigCloudAzure,
    host: str,
) -> None:
    """Delete virtual machine with network interface"""
    async with ClientSecretCredential(
        cfg.tenant_id, cfg.client_id, cfg.client_secret
    ) as cred:
        cred = cast(AsyncTokenCredential, cred)  # fix library type errors
        async with NetworkManagementClient(cred, cfg.subscription_id) as nmc:
            async with ComputeManagementClient(cred, cfg.subscription_id) as cmc:
                return await delete_node(nmc, cmc, log, cfg, host)
