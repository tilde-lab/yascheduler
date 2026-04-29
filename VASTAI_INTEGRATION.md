# VastAI Integration with Yascheduler

## Overview
This integration adds VastAI as a cloud provider for yascheduler, allowing automatic creation and deletion of GPU instances on the VastAI marketplace.

## Files Modified/Created

### 1. `yascheduler/clouds/vastai.py` (NEW)
Main VastAI cloud module implementing:
- `vastai_create_node()` - Async function to create a VastAI instance
- `vastai_delete_node()` - Async function to delete a VastAI instance
- Helper functions for API communication with VastAI
- Automatic instance search, creation, and SSH readiness verification

### 2. `yascheduler/config/cloud.py` (MODIFIED)
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

### 3. `yascheduler/clouds/adapters.py` (MODIFIED)
Added `get_vastai_adapter()` function to create CloudAdapter for VastAI.

### 4. `yascheduler/clouds/cloud_api_manager.py` (MODIFIED)
Added VastAI to `CLOUD_ADAPTER_GETTERS` dictionary.

### 5. `yascheduler/config/__init__.py` (MODIFIED)
Added `ConfigCloudVastAI` to exports.

## Configuration

Add VastAI settings to the `[clouds]` section in `/etc/yascheduler/yascheduler.conf`:

```ini
[clouds]
vastai_api_key = YOUR_VAST_API_KEY_HERE
vastai_image = pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel
vastai_disk_gb = 80
vastai_min_vram_mb = 81920
vastai_num_gpus = 1
vastai_max_price_per_hr = 1.50
vastai_max_nodes = 10
vastai_user = root
vastai_priority = 0
vastai_idle_tolerance = 300
vastai_onstart_script = 
vastai_docker_options = -p 8384:8384
vastai_jump_user = 
vastai_jump_host = 
```

**Important:** All cloud providers (azure, hetzner, upcloud, vastai) share the same `[clouds]` section. Each setting uses its prefix (`az_`, `hetzner_`, `upcloud_`, `vastai_`).

## Usage

The integration follows yascheduler's automatic node management:
- **Tasks pending** → Yascheduler automatically creates VastAI instances
- **No tasks** → Yascheduler automatically deletes idle instances
- No manual `yasetnode` calls needed

### Configuration Steps:

1. **Get VastAI API key** from https://vast.ai/console/cli/

2. **Edit `/etc/yascheduler/yascheduler.conf`** - Add to `[clouds]` section:
   ```ini
   [clouds]
   vastai_api_key = YOUR_VAST_API_KEY_HERE
   vastai_image = pytorch/pytorch:2.2.2-cuda12.1-cudnn8-devel
   vastai_disk_gb = 80
   vastai_min_vram_mb = 81920
   vastai_max_price_per_hr = 1.50
   vastai_max_nodes = 10
   vastai_user = root
   vastai_priority = 0
   vastai_idle_tolerance = 300
   ```

3. **Initialize**: `yainit`

4. **Start yascheduler service**

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
