"""Define calculation engine value objects and deploy strategy types."""
# region MODULE_CONTRACT
# PURPOSE: Describe calculation engines and their deploy artefacts so the scheduler can match tasks to compatible machines and ship inputs deterministically.
# SCOPE:
# - Engine value object, EngineRepository collection, and the Deploy strategy union (LocalFilesDeploy, LocalArchiveDeploy, RemoteArchiveDeploy).
# - NOT: INI parsing of engines (entrypoints.config_parser) or remote deployment execution (infra.ssh.operations).
# INVARIANTS: Engines and repositories are frozen; repository keys are unique engine names.
# RATIONALE:
# - Q: Why are these types in the domain layer instead of in config?
#   A: They encode business rules (platform matching, input validation) consumed by use cases; config only parses INI into them. Keeping them in domain prevents use cases from depending on the config module.
# KEYWORDS: engine, calculation, spawn, platforms, deploy, EngineRepository, validate inputs
# endregion MODULE_CONTRACT

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import chain
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence, ValuesView
    from pathlib import PurePath

from .exceptions import MissingInputFileError

__all__ = [
    "Deploy",
    "Engine",
    "EngineRepository",
    "LocalArchiveDeploy",
    "LocalFilesDeploy",
    "RemoteArchiveDeploy",
]


@dataclass(frozen=True)
class LocalFilesDeploy:
    """Deploy local files configuration."""

    files: tuple[PurePath, ...] = ()


@dataclass(frozen=True)
class LocalArchiveDeploy:
    """Deploy local archive configuration."""

    file: PurePath


@dataclass(frozen=True)
class RemoteArchiveDeploy:
    """Deploy remote archive configuration."""

    url: str


Deploy = Union[
    LocalFilesDeploy,
    LocalArchiveDeploy,
    RemoteArchiveDeploy,
]


# region CLASS_Engine
# PURPOSE: Specify a calculation engine's spawn command, platform support, and deploy artefacts so tasks can be matched to compatible machines and provisioned reproducibly.
@dataclass(frozen=True)
class Engine:
    """Calculation engine specification with spawn command, platforms, deploy strategies."""

    name: str
    spawn: str
    input_files: tuple[str, ...] = ()
    output_files: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    check_cmd: str | None = None
    check_pname: str | None = None
    deployable: tuple[Deploy, ...] = ()
    platform_packages: tuple[str, ...] = ()
    check_cmd_code: int = 0
    sleep_interval: int = 10
    # Hostnames never considered for this engine's tasks, even when free and
    # platform-compatible. Lets heterogeneous fleets keep small-RAM or
    # special-purpose nodes out of engine classes they cannot serve.
    exclude_nodes: frozenset[str] = frozenset()

    # region METHOD_validate_inputs
    # PURPOSE: Verify every required engine input file is present in the task payload, failing fast before deployment.
    # REQUIRES: extra keys are input-file names.
    def validate_inputs(self, extra: Mapping[str, object]) -> None:
        """Verify all required engine input files exist in the task extra payload."""
        for filename in self.input_files:
            if filename not in extra:
                raise MissingInputFileError(self.name, filename)

    # endregion METHOD_validate_inputs


# endregion CLASS_Engine


# region CLASS_EngineRepository
# PURPOSE: Hold the set of known engines as a frozen, queryable collection so allocation and setup can filter by predicate or platform without mutating shared state.
# RATIONALE:
# - Q: Why is EngineRepository unhashable despite being a frozen dataclass?
#   A: Frozen dataclasses only generate __hash__ when all fields are hashable. Mapping[str, Engine] is not hashable (dict is unhashable in Python), so EngineRepository deliberately does not define __hash__.
@dataclass(frozen=True)
class EngineRepository:
    """Frozen collection of engines keyed by name."""

    data: Mapping[str, Engine] = field(default_factory=dict)

    def get(self, name: str) -> Engine | None:
        """Return the engine for the given name, or ``None``."""
        return self.data.get(name)

    def __getitem__(self, name: str) -> Engine:
        return self.data[name]

    def __contains__(self, name: object) -> bool:
        return name in self.data

    def values(self) -> ValuesView[Engine]:
        """Return a view of all engine values."""
        return self.data.values()

    def keys(self) -> list[str]:
        """Return a list of all engine names."""
        return list(self.data.keys())

    def items(self) -> list[tuple[str, Engine]]:
        """Return a list of all (name, engine) pairs."""
        return list(self.data.items())

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    # region METHOD_filter
    # PURPOSE: Return a new repository retaining only engines the predicate accepts.
    def filter(self, fn: Callable[[Engine], bool]) -> EngineRepository:
        """Filter engines by predicate and return a new frozen repository."""
        return EngineRepository(data={k: v for k, v in self.data.items() if fn(v)})

    # endregion METHOD_filter

    # region METHOD_filter_platforms
    # PURPOSE: Narrow the repository to engines that support at least one of the given platforms.
    # ENSURES: Result engines have non-empty platform intersection with the argument.
    def filter_platforms(self, platforms: Sequence[str]) -> EngineRepository:
        """Filter engines by supported platforms and return a new frozen repository."""
        return self.filter(lambda e: bool(set(e.platforms) & set(platforms)))

    # endregion METHOD_filter_platforms

    # region METHOD_get_platform_packages
    # PURPOSE: Gather the unique platform packages across all engines for provisioning decisions.
    # ENSURES: Result has no duplicate entries.
    def get_platform_packages(self) -> list[str]:
        """Collect the unique union of platform_packages across all engines."""
        mapped = (e.platform_packages for e in self.values())
        return list(set(chain(*mapped)))

    # endregion METHOD_get_platform_packages


# endregion CLASS_EngineRepository


Mapping.register(EngineRepository)
