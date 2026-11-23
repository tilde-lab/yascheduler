"""Cloud provider config DTOs."""
# region MODULE_CONTRACT
# PURPOSE: Define per-provider configuration contracts so the provisioner can read VM parameters (image, size, credentials, limits) without depending on provider-specific SDK types.
# SCOPE: ConfigCloudAzure, ConfigCloudHetzner, ConfigCloudUpcloud, ConfigCloudVastAI, ConfigCloudVultr, AzureImageReference, ConfigCloud union.
# KEYWORDS: config, dto, azure, hetzner, upcloud, vastai, vultr, cloud config, image reference
# endregion MODULE_CONTRACT

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from yascheduler.domain import CloudConfig
from yascheduler.shared import MAX_PORT, validate_interval

if TYPE_CHECKING:
    from yascheduler.shared import Self

__all__ = [
    "AzureImageReference",
    "ConfigCloud",
    "ConfigCloudAzure",
    "ConfigCloudHetzner",
    "ConfigCloudUpcloud",
    "ConfigCloudVastAI",
    "ConfigCloudVultr",
]


# region CLASS_AzureImageReference
# PURPOSE: Hold the Azure image identity so INI config can refer to a VM image by one URN instead of four separate keys.
@dataclass(frozen=True)
class AzureImageReference:
    """Azure image reference (publisher:offer:sku:version URN)."""

    publisher: str = "Debian"
    offer: str = "debian-11-daily"
    sku: str = "11-backports-gen2"
    version: str = "latest"

    # region METHOD_from_urn
    # PURPOSE: Parse a publisher:offer:sku:version URN into structured fields so Azure image config is human-friendly at the INI level.
    @classmethod
    def from_urn(cls, urn: str) -> Self:
        """Create image reference from urn in format `publisher:offer:sku:version`."""
        min_parts = 4
        parts = urn.split(":", maxsplit=min_parts)
        if len(parts) < min_parts:
            msg = "`Image reference URN should be in format publisher:offer:sku:version"
            raise ValueError(msg)
        return cls(*parts)

    # endregion METHOD_from_urn


# endregion CLASS_AzureImageReference


# region CLASS_ConfigCloudAzure
# PURPOSE: Carry Azure-specific credentials, network topology, and VM sizing so the Azure provider can be configured from INI without leaking Azure SDK types into other providers.
@dataclass(frozen=True)
class ConfigCloudAzure(CloudConfig):
    """Azure cloud configuration."""

    # Class attr — not a dataclass field; identifies the provider prefix in INI keys.
    prefix = "az"

    tenant_id: str
    client_id: str
    client_secret: str
    subscription_id: str
    resource_group: str = "yascheduler-rg"
    location: str = "westeurope"
    vnet: str = "yascheduler-vnet"
    subnet: str = "yascheduler-subnet"
    nsg: str = "yascheduler-nsg"
    vm_image: AzureImageReference = field(default_factory=AzureImageReference)
    vm_size: str = "Standard_B1s"
    max_nodes: int = 10
    username: str = "yascheduler"
    priority: int = 0
    idle_tolerance: int = 300
    connect_grace: int = 120
    package_upgrade: bool = True
    jump_username: str | None = None
    jump_host: str | None = None
    jump_port: int = 22
    label: str = "yascheduler"

    def __post_init__(self) -> None:
        validate_interval(f"{self.prefix}_max_nodes", self.max_nodes, 0)
        validate_interval(f"{self.prefix}_idle_tolerance", self.idle_tolerance, 1)
        validate_interval(f"{self.prefix}_connect_grace", self.connect_grace, 1)
        validate_interval(f"{self.prefix}_jump_port", self.jump_port, 1, MAX_PORT)


# endregion CLASS_ConfigCloudAzure


# region CLASS_ConfigCloudHetzner
# PURPOSE: Carry Hetzner token, server type, and image so the Hetzner provider can be configured from INI without leaking provider SDK types into other providers.
@dataclass(frozen=True)
class ConfigCloudHetzner(CloudConfig):
    """Hetzner cloud configuration."""

    prefix = "hetzner"

    token: str
    max_nodes: int = 10
    username: str = "root"
    priority: int = 0
    server_type: str = "cx52"
    location: str = "fsn1"
    image_name: str = "debian-13"
    idle_tolerance: int = 120
    connect_grace: int = 60
    package_upgrade: bool = True
    jump_username: str | None = None
    jump_host: str | None = None
    jump_port: int = 22
    label: str = "yascheduler"

    def __post_init__(self) -> None:
        validate_interval(f"{self.prefix}_max_nodes", self.max_nodes, 0)
        validate_interval(f"{self.prefix}_idle_tolerance", self.idle_tolerance, 1)
        validate_interval(f"{self.prefix}_connect_grace", self.connect_grace, 1)
        validate_interval(f"{self.prefix}_jump_port", self.jump_port, 1, MAX_PORT)


# endregion CLASS_ConfigCloudHetzner


# region CLASS_ConfigCloudUpcloud
# PURPOSE: Carry UpCloud credentials and image so the UpCloud provider can be configured from INI without leaking upcloud_api types into other providers.
@dataclass(frozen=True)
class ConfigCloudUpcloud(CloudConfig):
    """Upcloud cloud configuration."""

    prefix = "upcloud"

    login: str
    password: str
    max_nodes: int = 10
    username: str = "root"
    priority: int = 0
    idle_tolerance: int = 120
    connect_grace: int = 60
    package_upgrade: bool = True
    jump_username: str | None = None
    jump_host: str | None = None
    jump_port: int = 22
    label: str = "yascheduler"

    def __post_init__(self) -> None:
        validate_interval(f"{self.prefix}_max_nodes", self.max_nodes, 0)
        validate_interval(f"{self.prefix}_idle_tolerance", self.idle_tolerance, 1)
        validate_interval(f"{self.prefix}_connect_grace", self.connect_grace, 1)
        validate_interval(f"{self.prefix}_jump_port", self.jump_port, 1, MAX_PORT)


# endregion CLASS_ConfigCloudUpcloud


# region CLASS_ConfigCloudVastAI
# PURPOSE: Carry VastAI GPU-scheduling parameters so the VastAI provider can be configured from INI without leaking aiohttp types into other providers.
@dataclass(frozen=True)
class ConfigCloudVastAI(CloudConfig):
    """VastAI cloud configuration."""

    prefix = "vastai"

    api_key: str
    image: str = "pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel"
    disk_gb: int = 80
    min_vram_mb: int = 80 * 1024
    num_gpus: int = 1
    max_price_per_hr: float = 1.50
    max_nodes: int = 10
    priority: int = 0
    idle_tolerance: int = 300
    connect_grace: int = 300
    package_upgrade: bool = True
    onstart_script: str | None = None
    docker_options: str | None = None
    jump_username: str | None = None
    jump_host: str | None = None
    jump_port: int = 22
    label: str = "yascheduler"

    def __post_init__(self) -> None:
        validate_interval(f"{self.prefix}_disk_gb", self.disk_gb, 1)
        validate_interval(f"{self.prefix}_min_vram_mb", self.min_vram_mb, 1024)
        validate_interval(f"{self.prefix}_num_gpus", self.num_gpus, 1)
        validate_interval(f"{self.prefix}_max_price_per_hr", self.max_price_per_hr, 0)
        validate_interval(f"{self.prefix}_max_nodes", self.max_nodes, 0)
        validate_interval(f"{self.prefix}_idle_tolerance", self.idle_tolerance, 1)
        validate_interval(f"{self.prefix}_connect_grace", self.connect_grace, 1)
        validate_interval(f"{self.prefix}_jump_port", self.jump_port, 1, MAX_PORT)


# endregion CLASS_ConfigCloudVastAI


# region CLASS_ConfigCloudVultr
# PURPOSE: Carry Vultr bare-metal credentials, plan, region, and RAID flag so the Vultr provider can be configured from INI without leaking aiohttp types into other providers.
# RATIONALE:
# - Q: Why is need_raid a config field rather than derived from server_type?
#   A: Vultr bare-metal plans differ in NVMe layout (e.g. vbm-24c-256gb-amd ships NVMe unformatted vs vbm-8c-132gb where NVMe is the main disk); the operator knows which plan they configured.
@dataclass(frozen=True)
class ConfigCloudVultr(CloudConfig):
    """Vultr bare-metal cloud configuration."""

    prefix = "vultr"

    api_key: str
    location: str = "ams"
    server_type: str = "vbm-24c-256gb-amd"
    # Vultr OS id (integer, sent as `os_id` in the API). 2136 = Debian 12 (bookworm), 2284 = Ubuntu 24.04 LTS x64.
    image_name: int = 2136
    need_raid: bool = True
    max_nodes: int = 10
    username: str = "root"
    priority: int = 0
    idle_tolerance: int = 1800
    connect_grace: int = 300
    package_upgrade: bool = True
    jump_username: str | None = None
    jump_host: str | None = None
    jump_port: int = 22
    label: str = "yascheduler"

    def __post_init__(self) -> None:
        validate_interval(f"{self.prefix}_image_name", self.image_name, 1)
        validate_interval(f"{self.prefix}_max_nodes", self.max_nodes, 0)
        validate_interval(f"{self.prefix}_idle_tolerance", self.idle_tolerance, 1)
        validate_interval(f"{self.prefix}_connect_grace", self.connect_grace, 1)
        validate_interval(f"{self.prefix}_jump_port", self.jump_port, 1, MAX_PORT)


# endregion CLASS_ConfigCloudVultr


ConfigCloud = (
    ConfigCloudAzure
    | ConfigCloudHetzner
    | ConfigCloudUpcloud
    | ConfigCloudVastAI
    | ConfigCloudVultr
)
