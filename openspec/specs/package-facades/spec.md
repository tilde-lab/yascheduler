## Purpose

Define the package-facade discipline for `yascheduler`: the static
import layering, the layer facades as the sole cross-layer public
surface, the public Python client's stable JSON contract, and the
backward-compatibility guarantees for downstream consumers.

## Requirements

### Requirement: Package layering

The system SHALL enforce the import direction
`entrypoints → infra → application → domain → shared` statically. The
`[tool.importlinter]` layers contract in `pyproject.toml` is the
authority for both direct and indirect imports, and `yascheduler.shared`
is the bottom layer that imports nothing from a higher `yascheduler`
layer.

#### Scenario: an import against the layer direction fails the build

- **WHEN** a module in a lower layer imports from a higher layer
- **THEN** the import linter reports a contract violation and the build fails

### Requirement: Layer facades as public surface

Each architectural layer SHALL expose its cross-layer public surface
through the layer facade only. The domain, application, and infra
facades SHALL re-export the entities, ports, use cases, and adapters
their consumers need. Symbols are added to a facade lazily — only when
an external consumer needs them — and an empty facade is valid.

#### Scenario: a consumer resolves a symbol through the layer facade

- **WHEN** an adapter or the composition root needs a symbol from another layer
- **THEN** the symbol resolves through that layer's facade, not through a deep submodule path

### Requirement: Yascheduler public client contract

The `Yascheduler` client SHALL expose synchronous and asynchronous queue
methods to submit and query tasks. Submission SHALL accept an optional list of
output selections to record with the accepted task. Submission SHALL return the
new task's identity as an integer. Query methods SHALL return one mapping per
matched task: a sequence for list queries, and a single mapping or none for a
single-task query. This contract SHALL be identical across all public client
facades.

#### Scenario: submissions return integer identities through every facade

- **WHEN** a task is submitted through the synchronous or asynchronous client method of any public facade
- **THEN** the call returns the new task's identity as an integer

#### Scenario: every facade accepts supplied output selections

- **WHEN** a task is submitted with an explicit output selection list through the synchronous or the asynchronous client method
- **THEN** the accepted task records that list

### Requirement: Public task projection shape

Each publicly queried task mapping SHALL have exactly the top-level keys
`task_id`, `label`, `status`, `metadata`, `input_entries`, and `node`.
`task_id` SHALL remain a bare integer and `status` SHALL remain the
task-status enum. `node` SHALL be null for an unallocated task, otherwise a
mapping keyed by `hostname` carrying the node's connection fields. `metadata`
and `input_entries` SHALL follow the input projection requirements below.

#### Scenario: a query returns the documented task shape

- **WHEN** a consumer queries an existing task through any public client facade
- **THEN** the returned mapping has exactly the top-level keys `task_id`, `label`, `status`, `metadata`, `input_entries`, and `node`, shaped as:

```text
{
  "task_id": <int>,
  "label": <str>,
  "status": <status enum member>,
  "metadata": {"engine": <str>, "...non-null typed fields...": ..., "...unrelated extra payload...": ...},
  "input_entries": [<input entry>, ...],
  "node": {"hostname": <str>, "port": <int>, "username": <str>, "cloud": <str|null>, "...": ...} | null
}
```

- **AND** `node` is null for an unallocated task, and input bytes are recoverable from the documented `input_entries`

### Requirement: Typed input entries project as JSON-safe values

Query results SHALL carry task inputs as the top-level `input_entries`
sequence, ordered by task-relative path, as the canonical input projection.
File entries SHALL use the form `{"path": ..., "kind": "file",
"content_base64": ...}` with standard base64 including padding and no line
breaks; directory entries SHALL use `{"path": ..., "kind": "directory"}`
and carry no content field.

#### Scenario: entries are ordered, padded, and content-free for directories

- **WHEN** a queried task has multiple input entries, including binary files and directories
- **THEN** `input_entries` are ordered by task-relative path, file bytes decode from single-line padded base64, and directory entries carry only their path and kind

### Requirement: Metadata keeps a derived legacy input view

The `metadata` projection SHALL contain non-null typed fields, unrelated
extra payload, and a derived legacy view of the task's input files: one
filename/value key per file entry under the legacy CLI encoding, derived from
the immutable snapshot. The legacy encoding SHALL be UTF-8 text, except the
exact `fort.9` name, which SHALL always be canonical base64 — the same
meaning its submission accepts, so a value taken from `metadata` and decoded
as base64 yields the stored bytes. Directory entries SHALL NOT appear in
metadata; exact input bytes SHALL be recovered from `input_entries`. Typed
fields SHALL NOT be overwritten by input paths, and stored legacy extra
copies keep precedence over derived values while they exist.

#### Scenario: query keeps legacy metadata keys and adds typed inputs

- **WHEN** a consumer queries an existing task that has input files
- **THEN** the mapping's `input_entries` carry the input paths and exact bytes, its `metadata` still contains one filename/value key per input file under the legacy encoding (text as a string, binary as base64, `fort.9` always base64), and unrelated metadata is preserved unchanged

### Requirement: Public API stability

The system SHALL preserve the existing public API surface across
changes. Backward-compatible extensions (new optional parameters, new
public symbols) are permitted; breaking changes (removing or
repositioning parameters, changing a return shape, removing an exported
symbol) SHALL be treated as a new capability requiring explicit spec
coverage.

#### Scenario: the public client resolves through the compat shim

- **WHEN** a downstream consumer imports the public client through the backward-compatibility shim
- **THEN** the symbol resolves without error

#### Scenario: the AiiDA scheduler plugin loads under its entry point

- **WHEN** the AiiDA scheduler plugin is discovered through its scheduler entry-point group
- **THEN** the entry point named `yascheduler` resolves to the plugin

### Requirement: Public submission normalizes legacy and explicit inputs

Existing positional submission arguments and webhook options SHALL remain
supported. Both synchronous and asynchronous submission SHALL accept an
optional keyword-only `input_entries` argument containing typed file or
directory entries; file entries SHALL carry bytes and directory entries SHALL
carry no content. Submitted entries SHALL satisfy `task-file-selection`
before acceptance. Supplying explicit entries together with legacy input
payload keys SHALL fail as ambiguous.

#### Scenario: an ambiguous or invalid snapshot is rejected

- **WHEN** a caller mixes explicit entries with legacy input payload keys, or supplies invalid paths, duplicate explicit paths, conflicting entry kinds, invalid content, or an unsatisfied engine selection
- **THEN** submission fails with a diagnostic identifying the ambiguity or invalid input and no task is accepted

### Requirement: Legacy metadata values normalize to the typed snapshot

When explicit entries are absent, the boundary SHALL interpret engine-declared
legacy file values in metadata as inputs and retain other metadata as user
payload. Legacy strings SHALL encode as UTF-8, except the existing `fort.9`
base64 convention; the boundary SHALL not guess base64 for other filenames.
Normalization SHALL produce the same typed snapshot used for filesystem
submissions and SHALL not read the caller's filesystem to fill missing values.

#### Scenario: legacy and explicit submission use the same snapshot path

- **WHEN** a caller submits valid legacy file metadata or explicit typed entries for a configured engine
- **THEN** the system accepts a task with a typed snapshot and returns an integer identity, preserving unrelated metadata
