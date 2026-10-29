# Yet another computing scheduler & cloud orchestration engine

[![DOI](https://zenodo.org/badge/222936146.svg)](https://doi.org/10.5281/zenodo.7693555)
[![PyPI](https://img.shields.io/pypi/v/yascheduler.svg?style=flat)](https://pypi.org/project/yascheduler)
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler?ref=badge_shield)

**Yascheduler** is a simple job scheduler designed for submitting scientific
calculations and copying back the results from the computing clouds.

Currently it supports several scientific simulation codes in chemistry and solid
state physics. Any other scientific simulation code can be supported via the
declarative control template system (see `yascheduler.conf` settings file).
There is an [example dummy C++ code](https://github.com/tilde-lab/dummy-engine)
with its configuration template.

## Installation

Use `pip` and PyPI: `pip install yascheduler`.

By default, no cloud connectors are installed.
To install the appropriate connector, use one of the commands:

- for Microsoft Azure: `pip install yascheduler[azure]`
- for UpCloud: `pip install yascheduler[upcloud]`

The last updates and bugfixes can be obtained cloning the repository:

```sh
git clone https://github.com/tilde-lab/yascheduler.git
pip install yascheduler/
```

The installation procedure creates the configuration file located at
`/etc/yascheduler/yascheduler.conf`.
The file contains credentials for Postgres database access, used directories,
cloud providers and scientific simulation codes (called *engines*).
Please check and amend this file with the correct credentials. The database
and the system service should then be initialized with `yainit` script.

## Usage

```python
from yascheduler import Yascheduler

yac = Yascheduler()
label = "test assignment"
engine = "pcrystal"
struct_input = str(...)  # simulation control file: crystal structure
setup_input = str(...)  # simulation control file: main setup, can include struct_input
result = yac.queue_submit_task(
    label, {"fort.34": struct_input, "INPUT": setup_input}, engine
)
print(result)
```

Or run directly in console with `yascheduler` (use a key `-l DEBUG` to change
the log level).

*Supervisor* config reads e.g.:

```ini
[program:scheduler]
command=/usr/local/bin/yascheduler
user=root
autostart=true
autorestart=true
stderr_logfile=/data/yascheduler.log
stdout_logfile=/data/yascheduler.log
```

File paths can be set using the environment variables:

- `YASCHEDULER_CONF_PATH`
  Configuration file.
  *Default*: `/etc/yascheduler/yascheduler.conf`

- `YASCHEDULER_LOG_PATH`
  Log file path.
  *Default*: `/var/log/yascheduler.log`

- `YASCHEDULER_PID_PATH`
  PID file.
  *Default*: `/var/run/yascheduler.pid`

### Typed input entries

Pass typed entries with the keyword-only `input_entries=` argument (both
`queue_submit_task` and `queue_submit_task_async` accept it) to submit exact
bytes and directory structure:

```python
from pathlib import Path

from yascheduler import Yascheduler
from yascheduler.domain import TaskInputDirectory, TaskInputFile

yac = Yascheduler()
task_id = yac.queue_submit_task(
    "test assignment",
    {"local_folder": "/tmp/yascheduler-out", "project": "demo"},
    "pcrystal",
    input_entries=[
        TaskInputFile("INPUT", Path("INPUT").read_bytes()),
        TaskInputFile("fort.34", Path("fort.34").read_bytes()),  # binary-safe
        TaskInputDirectory("basis"),
    ],
)
print(task_id)
```

Entries must satisfy the engine's declared `input_files`; unrelated metadata
keys are preserved. The legacy positional form still works: engine-declared
file keys in metadata become the typed snapshot, ordinary strings encode as
UTF-8, and only the historical `fort.9` key accepts base64. Supplying
`input_entries=` together with legacy engine-declared input keys is rejected as
ambiguous. An explicit empty `input_entries=[]` is a valid empty snapshot, not
a fallback to metadata.

### Query results

`queue_get_tasks`, `queue_get_task`, and their async variants return one mapping
per task. Queries are additive: `metadata` still carries one filename/value key
per input file under the legacy encoding (UTF-8 text as a plain string,
anything else as padded base64), so existing metadata consumers keep working.
Before the typed snapshot existed, that metadata was the only input view:

```python
from yascheduler.domain import TaskStatus

# Before
{
    "task_id": 42,
    "label": "test assignment",
    "status": TaskStatus.TO_DO,
    "metadata": {
        "engine": "pcrystal",
        "local_folder": "/tmp/yascheduler-out",
        "project": "demo",
        "INPUT": "...",
        "fort.34": "...",
    },
    "node": None,
}
```

Queries now add a top-level `input_entries` list, sorted by task-relative path,
which is the canonical projection of the snapshot: it carries exact bytes and
also directory entries, which have no legacy metadata representation. The
metadata keys are unchanged — derived from that same immutable snapshot rather
than from a second stored copy:

```python
import base64

from yascheduler.domain import TaskStatus

task = yac.queue_get_task(42)
# {
#     "task_id": 42,
#     "label": "test assignment",
#     "status": TaskStatus.TO_DO,
#     "metadata": {
#         "engine": "pcrystal",
#         "local_folder": "/tmp/yascheduler-out",
#         "project": "demo",
#         "INPUT": "...",
#         "fort.34": "...",
#     },
#     "input_entries": [
#         {"path": "INPUT", "kind": "file", "content_base64": "..."},
#         {"path": "basis", "kind": "directory"},
#         {"path": "fort.34", "kind": "file", "content_base64": "..."},
#     ],
#     "node": None,
# }

entry = next(e for e in task["input_entries"] if e["kind"] == "file")
content = base64.b64decode(entry["content_base64"])  # exact file bytes
```

In `metadata`, text inputs appear as their decoded string and binary inputs as
padded base64; typed metadata fields are never overwritten by input paths.
`content_base64` in `input_entries` is always standard base64 with padding and
no line breaks; directory entries carry no content field. `node` is `None`
until allocation; allocated tasks return a mapping with `hostname`, `port`,
`username`, `cloud`, `status`, and timestamps.

## Configuration

Yascheduler reads its settings from an INI file. The file path is set by the
`YASCHEDULER_CONF_PATH` environment variable (see [Usage](#usage) above).

The file covers the PostgreSQL database, local daemon settings, remote SSH
defaults, cloud providers, and calculation engines. Every section, key, and
default is documented in **[docs/CONFIG.md](docs/CONFIG.md)**.

For cloud-provider-specific setup (Azure, VastAI, Vultr), see also
[docs/AZURE.md](docs/AZURE.md), [docs/VASTAI.md](docs/VASTAI.md),
[docs/VULTR.md](docs/VULTR.md).

## Aiida Integration

See the detailed instructions for the [MPDS-AiiDA-CRYSTAL
workflows](https://github.com/mpds-io/mpds-aiida) as well as the
[ansible-mpds](https://github.com/mpds-io/ansible-mpds) repository. In essence:

```sh
ssh aiidauser@localhost # important
reentry scan
verdi computer setup
verdi computer test $COMPUTER
verdi code setup
```

## License

[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2Ftilde-lab%2Fyascheduler?ref=badge_large)
