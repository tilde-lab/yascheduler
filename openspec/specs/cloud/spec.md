## Purpose

Decouple cloud-provider VM lifecycle (provisioning, deletion, connection
setup) from the scheduler core. Cloud providers are added, removed, or
retired without changes to task allocation, SSH orchestration, or the
domain model.

## Requirements

### Requirement: Cloud configuration port

The system SHALL expose one structural cloud-configuration port. Every
provider satisfies the port by field shape alone; the system requires no
shared inheritance hierarchy. Application code reads only this surface.

The port SHALL carry the cloud prefix, the node-count limit, the idle
tolerance, the connect-grace window, the SSH username, and the three
jump-host fields. The connect-grace window governs how long the system
tolerates a cloud node's connections failing.

The system SHALL support these providers:

| Prefix  | Provider |
| ---     | ---      |
| az      | Azure    |
| hetzner | Hetzner  |
| upcloud | UpCloud  |
| vastai  | VastAI   |
| vultr   | Vultr    |

#### Scenario: a provider configuration is built per registered prefix

- **WHEN** the INI `[clouds]` section contains keys whose first segment matches a registered prefix
- **THEN** the parser builds one provider configuration per registered prefix
- **AND** keys whose first segment matches no registered prefix are ignored

### Requirement: Per-cloud INI parsing and validation

The cloud parser SHALL derive the cloud prefixes from the `[clouds]`
option names (the segment before the first underscore). For each prefix
whose user key is absent, the parser SHALL copy the remote default
username into the section before constructing the configuration.

Each provider credential (token, API key, login, password) SHALL be
required. The parser SHALL fail at config load when a credential is
absent or empty, and SHALL name the offending field by provider and
field label.

The parser SHALL read the jump-host port from the per-cloud jump-port
key (default 22) and SHALL reject values outside 1–65535 with an error.

#### Scenario: a missing required credential fails at config load

- **GIVEN** an INI `[clouds]` section that omits or empties a provider credential
- **WHEN** the configuration is loaded
- **THEN** the load fails before the daemon starts
- **AND** the error names the provider and the missing field

#### Scenario: the jump-host port is read from the per-cloud key or defaults to 22

- **WHEN** the `[clouds]` section sets the per-cloud jump-port key to an integer in range
- **THEN** the parser uses that integer as the jump-host port
- **AND** when the key is absent, the parser uses 22

#### Scenario: the parser rejects a jump-host port outside 1–65535

- **GIVEN** the `[clouds]` section sets the per-cloud jump-port key to 0 or to a value above 65535
- **WHEN** the configuration is loaded
- **THEN** the load fails before the daemon starts

### Requirement: Cloud adapter purity

The cloud adapter SHALL communicate only with the cloud provider API and
the SSH machine repository. All database writes — node creation, status
changes, deletion — are owned by the application layer.

#### Scenario: a cloud lifecycle event records state through the application layer

- **WHEN** a cloud node is provisioned, set up, or torn down
- **THEN** every database state change is recorded by the application layer, not by the cloud adapter

### Requirement: Cloud node provisioning

Provisioning a cloud node SHALL create the VM on the selected provider,
run cloud-init and engine setup over SSH, and return an enabled node
that reuses the passed identity. The returned node's hostname, address,
SSH username, SSH port, and three jump fields SHALL come from the cloud
provider response. The jump fields are stamped here once; the
authoritative source-selection rule lives in the `domain-entities` spec.

Transient SSH connect failures during setup SHALL be retried before the
connect is treated as a setup failure.

On VM creation failure, provisioning SHALL raise a cloud allocation
error so the caller discards the placeholder node. On a setup failure
that persists — SSH connect failure that persists, cloud-init failure, or
engine install failure — provisioning SHALL disconnect the SSH session
for that node, delete the VM to stop billing, and raise a cloud setup
error. Cancellation arriving mid-setup (e.g. daemon shutdown) SHALL be
treated the same way for resource cleanup — disconnect the SSH session
and delete the VM to stop billing — but the cancellation SHALL propagate
unchanged rather than be converted to a cloud setup error, so the
caller's cancellation/drain semantics are preserved.

#### Scenario: a successful provisioning returns an enabled node

- **WHEN** a placeholder node is allocated to a provider and the VM is created and set up
- **THEN** the returned node reuses the placeholder identity
- **AND** the node carries the cloud-provisioned address, SSH credentials, and three jump fields
- **AND** the node is enabled

#### Scenario: a setup failure tears down the VM

- **WHEN** setup does not succeed after the VM is created
- **THEN** the SSH session for that node is disconnected
- **AND** the VM is deleted on the provider to stop billing
- **AND** a cloud setup error is raised

#### Scenario: cancellation during setup tears down the VM and propagates

- **WHEN** setup is cancelled after the VM is created (SSH connect, cloud-init, or engine install)
- **THEN** the SSH session for that node is disconnected
- **AND** the VM is deleted on the provider to stop billing
- **AND** the cancellation propagates unchanged (no cloud setup error is raised)

#### Scenario: a failure after partial cloud resources are created cleans them up

- **WHEN** a provider's create call fails after one or more billable cloud resources have already been created (for example a network interface created before the VM, or an instance accepted before a usable identifier is returned)
- **THEN** the provider best-effort deletes the partially created resources before re-raising
- **AND** the original create error propagates to the caller
- **AND** any cleanup failure is logged so a still-billing orphan can be reconciled manually

### Requirement: Vultr SSH key reuse

Before provisioning a Vultr bare-metal instance, the provider SHALL list the
account SSH keys and reuse the identifier of an entry whose `ssh_key` public
key equals the scheduler public key under whitespace-trimmed comparison
(the exported scheduler key carries a trailing newline the provider may strip
on storage). The provider SHALL create an SSH key only when no listed public
key matches. Concurrent key lookups within one process SHALL be serialized
so at most one SSH key is created per unique public key. A listed SSH-key
entry without a string `id` or `ssh_key` SHALL be rejected as an invalid API
response.

#### Scenario: listed key has the scheduler public key but no fingerprint

- **WHEN** the Vultr SSH-key list contains an `id` and an `ssh_key` equal to
the scheduler public key modulo surrounding whitespace, and does not contain
a fingerprint
- **THEN** the provider uses that entry's identifier for provisioning
- **AND** the provider does not create another SSH key

#### Scenario: no listed public key matches

- **WHEN** no Vultr SSH-key list entry has an `ssh_key` equal to the scheduler
public key under whitespace-trimmed comparison
- **THEN** the provider creates one SSH key with the scheduler public key

#### Scenario: two allocations request the key concurrently

- **WHEN** two Vultr node allocations run the key lookup concurrently in one
process with no matching key on the account
- **THEN** the provider creates at most one SSH key
- **AND** both allocations use the same SSH-key identifier

#### Scenario: listed entry omits the public key

- **WHEN** the Vultr SSH-key list includes an entry without a string `ssh_key`
- **THEN** the provider raises an API response validation error
- **AND** the provider does not create an SSH key

### Requirement: Cloud node deallocation

Deallocation SHALL delete the VM on the provider named by the node's
cloud field, using the cloud-issued identifier stamped at provisioning.
The delete SHALL be idempotent: an already-deleted VM is a no-op that
logs a warning and returns without error.

When the node has no cloud, or the named provider has no registered
adapter or no configuration, deallocation SHALL log a warning and return
without calling the provider.

#### Scenario: an already-deleted VM is a no-op

- **WHEN** a delete is requested for a VM the provider no longer knows
- **THEN** the provider logs that the node is unknown
- **AND** the call returns without raising

#### Scenario: an unknown cloud is skipped

- **WHEN** a delete is requested for a node whose cloud is unset, or whose provider has no registered adapter or configuration
- **THEN** the deallocator logs a warning
- **AND** no provider delete is attempted

### Requirement: Per-provider external identity

Each provider's create response SHALL carry the cloud-issued identifier
the system uses for all later deletes. The identifier format is:

| Provider | External identity |
| ---      | ---               |
| Azure    | VM IP address     |
| UpCloud  | VM IP address     |
| Vultr    | VM IP address     |
| Hetzner  | numeric server ID |
| VastAI   | instance ID       |

The hostname SHALL be the VM's IP for every provider. The SSH port
SHALL be the provider's configured port, except for VastAI, which SHALL
report the SSH port the instance publishes at readiness.

#### Scenario: a provider create response carries the per-provider identity

- **WHEN** a provider creates a VM successfully
- **THEN** the returned identity follows the per-provider format in the table above
- **AND** the hostname is the VM's IP address

### Requirement: Cloud-init document

The cloud-init renderer SHALL emit a `#cloud-config` header followed by
a JSON serialization of every cloud-init field. The cloud-init document
SHALL be driven by the per-cloud configuration; no field is sourced from
a global default.

#### Scenario: cloud-init output is JSON after the cloud-config header

- **WHEN** a cloud-init document is rendered for a provider
- **THEN** the first line is the `#cloud-config` header
- **AND** the body is a JSON serialization of every cloud-init field sourced from the per-cloud configuration

### Requirement: Cloud provisioner lifecycle

The cloud provisioner SHALL select a provider by capacity, priority, and
platform fit. The selection performs no I/O and returns no value when no
provider has capacity or when the chosen provider's concurrency slot is
held.

On shutdown, the provisioner SHALL close every SSH connection it opened
for cloud setup.

#### Scenario: shutdown closes every cloud SSH connection

- **WHEN** the provisioner is stopped
- **THEN** every SSH connection opened for cloud setup is closed
