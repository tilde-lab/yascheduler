# VastAI Integration with Yascheduler

## Overview
This integration adds VastAI as a cloud provider for yascheduler, allowing automatic creation and deletion of GPU instances on the VastAI marketplace.

## Files Modified/Created

### 1. `/yascheduler/yascheduler/clouds/vastai.py` (NEW)
Main VastAI cloud module implementing:
- `vastai_create_node()` - Async function to create a VastAI instance
- `vastai_delete_node()` - Async function to delete a VastAI instance
- Helper functions for API communication with VastAI
- Automatic instance search, creation, and SSH readiness verification

### 2. `/yascheduler/yascheduler/config/cloud.py` (MODIFIED)
Added `ConfigCloudVastAI` class with configuration options:
- `api_key` - VastAI API key
- `image` - Docker image to use
- `disk_gb` - Disk space in GB
- `min_vram_mb` - Minimum VRAM in MB (default 80GB)
- `num_gpus` - Number of GPUs (default 1)
- `max_price_per_hr` - Maximum price per hour (default $1.50)
- `onstart_script` - Script to run on instance startup
- `docker_options` - Additional Docker options
- `env` - Environment variables

### 3. `/yascheduler/yascheduler/clouds/adapters.py` (MODIFIED)
Added `get_vastai_adapter()` function to create CloudAdapter for VastAI.

### 4. `/yascheduler/yascheduler/clouds/cloud_api_manager.py` (MODIFIED)
Added VastAI to `CLOUD_ADAPTER_GETTERS` dictionary.

### 5. `/yascheduler/yascheduler/config/__init__.py` (MODIFIED)
Added `ConfigCloudVastAI` to exports.

## Configuration

Add a `[vastai]` section to `/etc/yascheduler/yascheduler.conf`:

```ini
[vastai]
api_key = YOUR_VAST_API_KEY_HERE
image = pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel
disk_gb = 80
min_vram_mb = 81920
num_gpus = 1
max_price_per_hr = 1.50
max_nodes = 10
user = root
priority = 0
idle_tolerance = 300
onstart_script = 
docker_options = -p 8384:8384
```

## Usage

The integration follows yascheduler's automatic node management:
- **Tasks pending** → Yascheduler automatically creates VastAI instances
- **No tasks** → Yascheduler automatically deletes idle instances
- No manual `yasetnode` calls needed

## Key Features
1. **Automatic instance search** - Finds cheapest available GPU offers matching criteria
2. **SSH readiness verification** - Waits for instances to be fully ready
3. **Proper cleanup** - Instances are deleted by IP address lookup
4. **Async support** - Follows yascheduler's async patterns
5. **Config integration** - Full integration with yascheduler's config system

## Testing

Run the test script:
```bash
cd /path/to/yascheduler
python test_vastai_integration.py
```

## Notes
- Requires `vastai-sdk` package: `pip install vastai-sdk`
- API key can be obtained from https://vast.ai/console/cli/
- Instances are created with `runtype: ssh_direct` for direct SSH access
- The `onstart_script` can be used to customize instance initialization
