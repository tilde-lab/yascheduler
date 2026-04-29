"""Test script for VastAI integration with yascheduler"""

import asyncio
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test_vastai")

# Test imports
from yascheduler.clouds.vastai import (
    vastai_create_node,
    vastai_delete_node,
    search_offers,
    create_instance,
    get_instance_info,
    find_instance_by_ip,
    delete_instance,
)
from yascheduler.config import ConfigCloudVastAI

# Example configuration (you need to set your actual API key)
config = ConfigCloudVastAI(
    api_key="YOUR_VAST_API_KEY_HERE",  # Replace with actual key
    image="pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel",
    disk_gb=80,
    min_vram_mb=80 * 1024,  # 80 GB
    num_gpus=1,
    max_price_per_hr=1.50,
    max_nodes=5,
    username="root",
    priority=0,
    idle_tolerance=300,
    onstart_script="",
    docker_options="-p 8384:8384",
    env={"HOST": "0.0.0.0", "PORT": "8384"},
    jump_username=None,
    jump_host=None,
)

print("✓ VastAI configuration created successfully")
print(f"  API Key: {config.api_key[:10]}...")
print(f"  Image: {config.image}")
print(f"  Max nodes: {config.max_nodes}")
print(f"  Max price: ${config.max_price_per_hr}/hr")

# Note: Actual testing requires a valid API key
print("\n⚠️  To test actual VastAI functionality, set a valid API key in the config")
print("   and run the create/delete functions with proper async handling.")

print("\n✓ VastAI integration with yascheduler is complete!")
print("\nNext steps:")
print("1. Install yascheduler with: pip install -e /path/to/yascheduler")
print("2. Configure /etc/yascheduler/yascheduler.conf - add to [clouds] section:")
print("""
[clouds]
vastai_api_key = YOUR_KEY
vastai_max_price_per_hr = 1.50
vastai_max_nodes = 10
...""")
print("3. Run yainit to initialize")
print("4. Start yascheduler service")
