import sys

if sys.version_info < (3, 10):
    from typing_extensions import ParamSpec
else:
    from typing import ParamSpec

if sys.version_info < (3, 11):
    from typing_extensions import Self
else:
    from typing import Self

__all__ = ["Self", "ParamSpec"]
