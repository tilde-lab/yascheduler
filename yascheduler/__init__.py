import sys

from .client import Yascheduler
from .variables import CONFIG_FILE, LOG_FILE, PID_FILE

if sys.version_info < (3, 8):
    import importlib_metadata
else:
    import importlib.metadata as importlib_metadata

__version__ = importlib_metadata.version("yascheduler")
__all__ = [
    "CONFIG_FILE",
    "LOG_FILE",
    "PID_FILE",
    "Yascheduler",
    "__version__",
]
