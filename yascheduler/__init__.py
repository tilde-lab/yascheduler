import importlib.metadata

from .client import Yascheduler
from .variables import CONFIG_FILE, LOG_FILE, PID_FILE

__version__ = importlib.metadata.version("yascheduler")
__all__ = [
    "CONFIG_FILE",
    "LOG_FILE",
    "PID_FILE",
    "Yascheduler",
    "__version__",
]
