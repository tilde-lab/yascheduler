## MODIFIED Requirements

### Requirement: Yascheduler public client contract

The `Yascheduler` client SHALL expose synchronous and asynchronous queue
methods to submit and query tasks. Submission SHALL return the new task's
identity as an integer. Query methods SHALL return one mapping per matched
task: a sequence for list queries, and a single mapping or none for a
single-task query. The public task projection SHALL have this shape:

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

Input entries SHALL be ordered by task-relative path and use these JSON-safe
forms. Standard base64 SHALL include padding and contain no line breaks.

| Entry kind | Public query representation |
| --- | --- |
| File | `{"path": "basis/Si.orb", "kind": "file", "content_base64": "..."}` |
| Directory | `{"path": "basis", "kind": "directory"}` |

The metadata projection SHALL contain non-null typed metadata fields, unrelated
extra payload, and a legacy metadata view of the task's own input files: one
filename/value key per file entry, derived from the immutable snapshot with
the legacy CLI encoding (UTF-8-decodable bytes as a plain string, otherwise
canonical padded base64). Directory entries SHALL NOT appear in metadata.
Exact input bytes SHALL be recovered from `input_entries`, which remains the
canonical projection. Typed metadata fields SHALL NOT be overwritten by input
paths, and stored legacy extra copies SHALL keep precedence over derived
values while they exist. `task_id` remains a bare integer, `status` remains
the task-status enum, and `node` remains null for an unallocated task or a
mapping keyed by `hostname` for an allocated task. This contract SHALL be
identical across all public client facades. This explicit input projection
augments the previous query-shape guarantee without removing legacy
filename/value metadata keys.

#### Scenario: a query returns the documented task shape

- **WHEN** a consumer queries an existing task through any public client facade
- **THEN** the returned mapping has exactly the top-level keys `task_id`, `label`, `status`, `metadata`, `input_entries`, and `node`; `node` is null for an unallocated task, otherwise it carries `hostname` with the node's connection fields, and input bytes are recoverable from the documented `input_entries`

#### Scenario: query keeps legacy metadata keys and adds typed inputs

- **WHEN** a consumer queries an existing task that has input files
- **THEN** the mapping's `input_entries` carry the input paths and exact bytes, its `metadata` still contains one filename/value key per input file under the legacy encoding (text as a string, binary as base64), and unrelated metadata is preserved unchanged

## ADDED Requirements

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
