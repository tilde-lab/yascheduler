# region MODULE_CONTRACT
# PURPOSE: Unit tests for domain entities: TaskStatus, MachineState, ProcessResult, Task, Node, ConnectedMachine.
# SCOPE: Enum values, dataclass defaults/frozen semantics, Task lifecycle methods (run/reject/complete/fail/abandon), ConnectedMachine state transitions, materialize_task, Task.error column format contract, public events field, and the input-field split (Task carries no input contents; NewTask does).
# KEYWORDS: TaskStatus, MachineState, ConnectedMachine, Task lifecycle
# endregion MODULE_CONTRACT

import time
from dataclasses import FrozenInstanceError, fields
from datetime import datetime

import pytest

from yascheduler.domain.events import (
    TaskAbandoned,
    TaskAllocated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
)
from yascheduler.domain.exceptions import (
    MachineBusyError,
    TaskNotRunningError,
    TaskNotTodoError,
)
from yascheduler.domain.model import (
    ConnectedMachine,
    Done,
    MachineState,
    NewNode,
    NewTask,
    Node,
    NodeId,
    NodeStatus,
    ProcessResult,
    Running,
    Task,
    TaskId,
    TaskState,
    TaskStatus,
    Todo,
    allocated_node_id_of,
    error_of,
    materialize_task,
    remote_folder_of,
)

_DT = datetime(2025, 1, 1)


def _node(node_id: int = 7, hostname: str = "10.0.0.1") -> Node:
    """Construct a minimal Node for allocate_to tests."""
    return Node(node_id=NodeId(node_id), hostname=hostname, ncpus=4)


def _make_task(**overrides: object) -> Task:
    """Build a Task with typed fields; defaults to a TO_DO state."""
    base: dict[str, object] = {
        "task_id": TaskId(1),
        "label": "test",
        "engine": "cp2k",
        "state": Todo(),
        "webhook_url": None,
        "webhook_custom_params": {},
        "extra": {},
        "created_at": _DT,
        "updated_at": _DT,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type,type-var]


class TestTaskStatus:
    def test_values(self) -> None:
        assert TaskStatus.TO_DO == 0
        assert TaskStatus.RUNNING == 1
        assert TaskStatus.DONE == 2

    def test_is_int(self) -> None:
        assert isinstance(TaskStatus.TO_DO, int)

    def test_members(self) -> None:
        assert TaskStatus.TO_DO is TaskStatus(0)
        assert TaskStatus.RUNNING is TaskStatus(1)
        assert TaskStatus.DONE is TaskStatus(2)


class TestMachineState:
    def test_free_not_equal_busy(self) -> None:
        assert MachineState.FREE != MachineState.BUSY

    def test_members(self) -> None:
        assert MachineState.FREE is MachineState(1)
        assert MachineState.BUSY is MachineState(2)


class TestProcessResult:
    def test_defaults(self) -> None:
        r = ProcessResult(exit_code=0)
        assert r.exit_code == 0
        assert r.stdout == ""
        assert r.stderr == ""

    def test_all_fields(self) -> None:
        r = ProcessResult(exit_code=1, stdout="out", stderr="err")
        assert r.exit_code == 1
        assert r.stdout == "out"
        assert r.stderr == "err"


class TestTaskState:
    def test_todo_default_remote_folder_is_none(self) -> None:
        state = Todo()
        assert state.remote_folder is None

    def test_todo_accepts_remote_folder(self) -> None:
        state = Todo(remote_folder="/prep")
        assert state.remote_folder == "/prep"

    def test_todo_status_classvar_is_to_do(self) -> None:
        assert Todo.status is TaskStatus.TO_DO

    def test_running_requires_both_fields(self) -> None:
        state = Running(allocated_node_id=NodeId(7), remote_folder="/r")
        assert state.allocated_node_id == NodeId(7)
        assert state.remote_folder == "/r"

    def test_running_status_classvar_is_running(self) -> None:
        assert Running.status is TaskStatus.RUNNING

    def test_done_defaults_all_optional(self) -> None:
        state = Done()
        assert state.error is None
        assert state.allocated_node_id is None
        assert state.remote_folder is None

    def test_done_accepts_independent_fields(self) -> None:
        state = Done(error="boom", allocated_node_id=None, remote_folder="/r")
        assert state.error == "boom"
        assert state.allocated_node_id is None
        assert state.remote_folder == "/r"

    def test_done_status_classvar_is_done(self) -> None:
        assert Done.status is TaskStatus.DONE

    def test_task_state_union_accepts_all_three(self) -> None:
        states: list[TaskState] = [Todo(), Running(NodeId(1), "/r"), Done()]
        assert isinstance(states[0], Todo)
        assert isinstance(states[1], Running)
        assert isinstance(states[2], Done)

    def test_states_are_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            Todo().remote_folder = "/x"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            Running(NodeId(1), "/r").remote_folder = "/x"  # type: ignore[misc]


class TestTask:
    def make_task(self, **overrides: object) -> Task:
        return _make_task(**overrides)

    def test_construction_default_state_is_todo(self) -> None:
        task = self.make_task()
        assert task.task_id == TaskId(1)
        assert task.label == "test"
        assert task.engine == "cp2k"
        assert isinstance(task.state, Todo)
        assert task.status == TaskStatus.TO_DO
        assert allocated_node_id_of(task) is None
        assert task.state.remote_folder is None
        assert error_of(task) is None
        assert not hasattr(task, "allocated_ip")
        assert isinstance(task.created_at, datetime)
        assert isinstance(task.updated_at, datetime)

    def test_has_no_input_entries_field(self) -> None:
        """A persisted task record carries no input contents; reads are explicit."""
        assert "input_entries" not in {f.name for f in fields(Task)}
        assert not hasattr(self.make_task(), "input_entries")

    def test_immutability(self) -> None:
        task = self.make_task()
        with pytest.raises(FrozenInstanceError):
            task.engine = "x"  # type: ignore[misc]

    def test_status_derived_from_state_classvar(self) -> None:
        todo = self.make_task()
        assert todo.status is TaskStatus.TO_DO
        running = self.make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert running.status is TaskStatus.RUNNING
        done = self.make_task(state=Done(error="boom"))
        assert done.status is TaskStatus.DONE

    def test_state_carries_fields_directly(self) -> None:
        running = self.make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert running.state.allocated_node_id == NodeId(7)
        assert running.state.remote_folder == "/r"
        done = self.make_task(
            state=Done(error="e", allocated_node_id=NodeId(9), remote_folder="/d")
        )
        assert done.state.error == "e"
        assert done.state.allocated_node_id == NodeId(9)
        assert done.state.remote_folder == "/d"

    def test_transitions_preserve_output_selections(self) -> None:
        selections = ("OUT.aiida", "**/*.dat")
        task = self.make_task(output_selections=selections)
        running = task.run(NodeId(7), "/r")
        assert running.output_selections is selections
        done = running.complete(local_folder="/l", remote_folder="/r")
        assert done.output_selections is selections

    def test_run_builds_running_state(self) -> None:
        task = self.make_task()
        running = task.run(NodeId(7), "/r")
        assert running.status == TaskStatus.RUNNING
        assert isinstance(running.state, Running)
        assert running.state.allocated_node_id == NodeId(7)
        assert running.state.remote_folder == "/r"
        assert running.task_id == task.task_id
        assert len(running.events) == 1
        evt = running.events[0]
        assert isinstance(evt, TaskAllocated)
        assert evt.node_id == NodeId(7)
        assert evt.engine_name == "cp2k"

    def test_run_on_non_todo_raises(self) -> None:
        task = self.make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        with pytest.raises(TaskNotTodoError) as exc_info:
            task.run(NodeId(8), "/r")
        assert "1" in str(exc_info.value)
        assert task.events == ()

    def test_complete_builds_done_state_carrying_allocation(self) -> None:
        task = self.make_task()
        running = task.run(NodeId(7), "/r")
        done = running.complete(local_folder="/l", remote_folder="/r")
        assert done.status == TaskStatus.DONE
        assert isinstance(done.state, Done)
        state = done.state
        assert state.error is None
        assert state.allocated_node_id == NodeId(7)
        assert state.remote_folder == "/r"
        assert done.local_folder == "/l"
        assert len(done.events) == 2
        assert isinstance(done.events[1], TaskCompleted)
        assert done.events[1].local_folder == "/l"

    def test_complete_on_todo_raises(self) -> None:
        task = self.make_task()
        with pytest.raises(TaskNotRunningError) as exc_info:
            task.complete(local_folder="/l", remote_folder="/r")
        assert "1" in str(exc_info.value)

    def test_fail_builds_done_state_with_error_and_allocation(self) -> None:
        task = self.make_task()
        running = task.run(NodeId(7), "/r")
        failed = running.fail("out of memory", local_folder="/l", remote_folder="/r")
        assert failed.status == TaskStatus.DONE
        assert isinstance(failed.state, Done)
        state = failed.state
        assert state.error == "out of memory"
        assert state.allocated_node_id == NodeId(7)
        assert state.remote_folder == "/r"
        assert failed.local_folder == "/l"
        assert len(failed.events) == 2
        assert isinstance(failed.events[1], TaskFailed)
        assert failed.events[1].reason == "out of memory"

    def test_fail_on_todo_raises(self) -> None:
        task = self.make_task()
        with pytest.raises(TaskNotRunningError) as exc_info:
            task.fail("reason", local_folder="/l", remote_folder="/r")
        assert "1" in str(exc_info.value)

    def test_reject_carries_prefilled_folder_into_done(self) -> None:
        task = self.make_task(state=Todo(remote_folder="/prep"))
        rejected = task.reject("unsupported engine")
        assert rejected.status == TaskStatus.DONE
        assert isinstance(rejected.state, Done)
        state = rejected.state
        assert state.error == "unsupported engine"
        assert state.allocated_node_id is None
        assert state.remote_folder == "/prep"
        assert len(rejected.events) == 1
        assert isinstance(rejected.events[0], TaskFailed)
        assert rejected.events[0].reason == "unsupported engine"

    def test_reject_on_running_raises(self) -> None:
        task = self.make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        with pytest.raises(TaskNotTodoError):
            task.reject("reason")

    def test_abandon_reads_allocation_from_state_and_emits_event(self) -> None:
        task = self.make_task()
        running = task.run(NodeId(7), "/r")
        abandoned = running.abandon()
        assert abandoned.status == TaskStatus.DONE
        assert isinstance(abandoned.state, Done)
        state = abandoned.state
        assert state.error == "node is gone"
        assert state.allocated_node_id == NodeId(7)
        assert state.remote_folder == "/r"
        assert len(abandoned.events) == 2
        evt = abandoned.events[1]
        assert isinstance(evt, TaskAbandoned)
        assert evt.node_id == NodeId(7)

    def test_abandon_accepts_custom_error(self) -> None:
        task = self.make_task()
        running = task.run(NodeId(7), "/r")
        abandoned = running.abandon(error="node deleted")
        assert isinstance(abandoned.state, Done)
        assert abandoned.state.error == "node deleted"

    def test_abandon_on_todo_raises(self) -> None:
        task = self.make_task()
        with pytest.raises(TaskNotRunningError) as exc_info:
            task.abandon()
        assert "1" in str(exc_info.value)

    def test_fields_constrained_by_status_row(self) -> None:
        """Gherkin: a task carries the fields its status row permits."""
        todo = self.make_task()
        assert allocated_node_id_of(todo) is None
        assert error_of(todo) is None

        running = self.make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert running.state.allocated_node_id is not None
        assert running.state.remote_folder is not None
        assert error_of(running) is None

        done = self.make_task(
            state=Done(error="e", allocated_node_id=None, remote_folder=None)
        )
        # DONE is unconstrained: all combinations legal
        assert isinstance(done.state, Done)
        state = done.state
        assert state.error == "e"
        assert state.allocated_node_id is None
        assert state.remote_folder is None


class TestNodeStatus:
    def test_other_value(self) -> None:
        """NodeStatus.OTHER is defined with value 'OTHER'."""
        assert NodeStatus.OTHER == "OTHER"
        assert NodeStatus.OTHER.value == "OTHER"

    def test_is_strenum(self) -> None:
        """NodeStatus members are str instances (StrEnum)."""
        assert isinstance(NodeStatus.OTHER, str)

    def test_name_lookup(self) -> None:
        """NodeStatus['OTHER'] returns NodeStatus.OTHER (name-based lookup)."""
        assert NodeStatus["OTHER"] is NodeStatus.OTHER


class TestNode:
    def test_defaults(self) -> None:
        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=4)
        assert node.node_id == NodeId(1)
        assert node.hostname == "10.0.0.1"
        assert node.ncpus == 4
        assert node.enabled is True
        assert node.cloud is None
        assert node.username == "root"
        assert node.port == 22
        assert node.jump_host is None
        assert node.jump_port == 22
        assert node.jump_username == "root"
        assert node.external_id is None
        assert node.status == NodeStatus.OTHER
        assert isinstance(node.created_at, datetime)
        assert isinstance(node.updated_at, datetime)

    def test_ncpus_none_means_discover_at_spawn(self) -> None:
        """Node with ncpus=None: orchestrator discovers at spawn via session cache."""
        node = Node(node_id=NodeId(1), hostname="10.0.0.1", ncpus=None)
        assert node.ncpus is None

    def test_full_construction(self) -> None:
        node = Node(
            node_id=NodeId(7),
            hostname="10.0.0.1",
            ncpus=8,
            enabled=False,
            cloud="hetzner",
            username="admin",
            port=2222,
            jump_host="jump.example.com",
            jump_port=2222,
            jump_username="jumpuser",
            external_id="ext-123",
            status=NodeStatus.OTHER,
        )
        assert node.node_id == NodeId(7)
        assert node.hostname == "10.0.0.1"
        assert node.ncpus == 8
        assert node.enabled is False
        assert node.cloud == "hetzner"
        assert node.username == "admin"
        assert node.port == 2222
        assert node.jump_host == "jump.example.com"
        assert node.jump_port == 2222
        assert node.jump_username == "jumpuser"
        assert node.external_id == "ext-123"
        assert node.status == NodeStatus.OTHER


class TestNodeId:
    def test_post_init_rejects_non_positive(self) -> None:
        for bad in (0, -1, -100):
            with pytest.raises(ValueError):
                NodeId(bad)

    def test_post_init_accepts_positive(self) -> None:
        NodeId(1)
        NodeId(99999)

    def test_str_renders_bare_int(self) -> None:
        assert str(NodeId(5)) == "5"
        assert f"{NodeId(42)}" == "42"

    def test_not_equal_to_int(self) -> None:
        assert (NodeId(5) == 5) is False
        assert (NodeId(5) != 5) is True

    def test_hashable_and_usable_as_dict_key(self) -> None:
        assert hash(NodeId(5)) == hash(NodeId(5))
        d = {NodeId(1): "a"}
        assert d[NodeId(1)] == "a"

    def test_equality_same_value(self) -> None:
        assert NodeId(5) == NodeId(5)
        assert NodeId(5) != NodeId(6)


class TestNewNode:
    def test_has_no_node_id_attribute(self) -> None:
        n = NewNode()
        assert not hasattr(n, "node_id")

    def test_defaults(self) -> None:
        n = NewNode(hostname="x", ncpus=4)
        assert n.enabled is True
        assert n.cloud is None
        assert n.username == "root"
        assert n.port == 22
        assert n.jump_host is None
        assert n.jump_port == 22
        assert n.jump_username == "root"
        assert n.external_id is None
        assert n.status == NodeStatus.OTHER

    def test_full_construction(self) -> None:
        n = NewNode(
            hostname="10.0.0.1",
            ncpus=8,
            enabled=False,
            cloud="aws",
            username="admin",
            port=2222,
            jump_host="jump.example.com",
            jump_port=2222,
            jump_username="jumpuser",
            external_id="ext-123",
            status=NodeStatus.OTHER,
        )
        assert n.hostname == "10.0.0.1"
        assert n.ncpus == 8
        assert n.enabled is False
        assert n.cloud == "aws"
        assert n.username == "admin"
        assert n.port == 2222
        assert n.jump_host == "jump.example.com"
        assert n.jump_port == 2222
        assert n.jump_username == "jumpuser"
        assert n.external_id == "ext-123"
        assert n.status == NodeStatus.OTHER

    def test_defaults_ncpus_to_none(self) -> None:
        """NewNode instantiated with only cloud and enabled defaults ncpus to None."""
        n = NewNode(cloud="aws", enabled=False)
        assert n.ncpus is None

    def test_tmp_reservation_defaults(self) -> None:
        n = NewNode(cloud="aws", enabled=False)
        assert n.hostname == ""
        assert n.ncpus is None
        assert n.username == "root"
        assert n.port == 22
        assert n.jump_host is None
        assert n.jump_port == 22
        assert n.jump_username == "root"
        assert n.external_id is None
        assert n.status == NodeStatus.OTHER
        assert n.enabled is False
        assert n.cloud == "aws"

    def test_explicit_hostname_ncpus_override_defaults(self) -> None:
        n = NewNode(hostname="10.0.0.1", ncpus=4)
        assert n.hostname == "10.0.0.1"
        assert n.ncpus == 4


class TestConnectedMachine:
    def make_machine(self, **overrides: object) -> ConnectedMachine:
        defaults: dict[str, object] = {"node_id": NodeId(1), "platforms": ("linux",)}
        defaults.update(overrides)
        return ConnectedMachine(**defaults)  # type: ignore[arg-type]

    def test_is_compatible_free_and_match(self) -> None:
        m = self.make_machine(state=MachineState.FREE, platforms=("linux",))
        assert m.is_compatible(("linux", "windows")) is True

    def test_is_compatible_busy_not_match(self) -> None:
        m = self.make_machine(state=MachineState.BUSY, platforms=("linux",))
        assert m.is_compatible(("linux",)) is False

    def test_is_compatible_platform_no_match(self) -> None:
        m = self.make_machine(state=MachineState.FREE, platforms=("windows",))
        assert m.is_compatible(("linux",)) is False

    def test_is_compatible_empty_platforms(self) -> None:
        m = self.make_machine(state=MachineState.FREE, platforms=("linux",))
        assert m.is_compatible(()) is False

    def test_is_compatible_broad_engine_matches_specific_host(self) -> None:
        """A broad engine platform tag matches a host detected as a more specific variant of it."""
        m = self.make_machine(
            state=MachineState.FREE,
            platforms=("linux", "debian-like", "debian", "debian-12"),
        )
        assert m.is_compatible(("debian",)) is True
        assert m.is_compatible(("linux",)) is True

    def test_occupy_transitions_to_busy(self) -> None:
        m = self.make_machine(state=MachineState.FREE)
        occupied = m.occupy()
        assert occupied.state == MachineState.BUSY
        assert m.state == MachineState.FREE

    def test_occupy_when_already_busy(self) -> None:
        m = self.make_machine(state=MachineState.BUSY)
        with pytest.raises(MachineBusyError) as exc_info:
            m.occupy()
        assert exc_info.value.node_id == NodeId(1)
        assert not hasattr(exc_info.value, "hostname")

    def test_release_sets_free_and_timestamp(self) -> None:
        before = time.monotonic()
        m = self.make_machine(state=MachineState.BUSY)
        released = m.release()
        after = time.monotonic()
        assert released.state == MachineState.FREE
        assert released.free_since is not None
        assert before <= released.free_since <= after
        assert m.state == MachineState.BUSY
        assert m.free_since is None


class TestTaskErrorFormat:
    def test_error_is_none_on_success(self) -> None:
        task = _make_task()
        running = task.run(NodeId(7), "/r")
        done = running.complete(local_folder="/l", remote_folder="/r")
        assert done.state.error is None

    def test_error_is_bare_reason_on_reject(self) -> None:
        task = _make_task()
        rejected = task.reject("unsupported engine")
        assert rejected.state.error == "unsupported engine"

    def test_error_is_bare_reason_on_fail(self) -> None:
        task = _make_task()
        running = task.run(NodeId(7), "/r")
        failed = running.fail("node is gone", local_folder="/l", remote_folder="/r")
        assert failed.state.error == "node is gone"


class TestMaterializeTask:
    def test_materialize_task_adds_task_created_event(self) -> None:
        task = _make_task(events=())
        result = materialize_task(task)
        assert len(result.events) == 1
        evt = result.events[0]
        assert isinstance(evt, TaskCreated)
        assert evt.task_id == task.task_id
        assert evt.webhook_url == task.webhook_url
        assert evt.webhook_custom_params == task.webhook_custom_params
        assert evt.engine_name == task.engine
        # All other fields preserved
        assert result.label == task.label
        assert result.status == task.status
        assert result.state.remote_folder == task.state.remote_folder


class TestTaskId:
    def test_post_init_rejects_non_positive(self) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError):
                TaskId(bad)

    def test_post_init_accepts_positive(self) -> None:
        TaskId(1)
        TaskId(99999)

    def test_str_renders_bare_int(self) -> None:
        assert str(TaskId(5)) == "5"
        assert f"{TaskId(5)}" == "5"

    def test_not_equal_to_int(self) -> None:
        assert (TaskId(5) == 5) is False
        assert (TaskId(5) != 5) is True

    def test_hashable_and_usable_as_dict_key(self) -> None:
        assert hash(TaskId(5)) == hash(TaskId(5))
        d = {TaskId(1): "a"}
        assert d[TaskId(1)] == "a"

    def test_equality_same_value(self) -> None:
        assert TaskId(5) == TaskId(5)
        assert TaskId(5) != TaskId(6)


class TestNewTask:
    def test_constructs_with_defaults(self) -> None:
        nt = NewTask(label="x", engine="cp2k")
        assert nt.label == "x"
        assert nt.engine == "cp2k"
        assert nt.local_folder is None
        assert nt.webhook_url is None
        assert nt.webhook_custom_params == {}
        assert nt.extra == {}
        assert nt.output_selections == ()
        assert not hasattr(nt, "allocated_ip")
        assert not hasattr(nt, "created_at")
        assert not hasattr(nt, "updated_at")
        assert not hasattr(nt, "status")
        assert not hasattr(nt, "allocated_node_id")

    def test_has_input_entries_field(self) -> None:
        """Submission is the one boundary that carries input contents."""
        assert "input_entries" in {f.name for f in fields(NewTask)}

    def test_has_no_task_id(self) -> None:
        nt = NewTask(label="x", engine="cp2k")
        assert not hasattr(nt, "task_id")

    def test_has_no_events_attribute(self) -> None:
        nt = NewTask(label="x", engine="cp2k")
        assert not hasattr(nt, "_events")

    def test_has_no_remote_folder_or_error(self) -> None:
        nt = NewTask(label="x", engine="cp2k")
        assert not hasattr(nt, "remote_folder")
        assert not hasattr(nt, "error")

    def test_has_no_lifecycle_methods(self) -> None:
        nt = NewTask(label="x", engine="cp2k")
        for method in (
            "run",
            "complete",
            "fail",
            "reject",
            "abandon",
        ):
            assert not hasattr(nt, method)


class TestGenericTask:
    """Generic Task[S_co] subscriptability and state-parameterized aliases."""

    def test_task_subscriptable_with_state(self) -> None:
        """Task[Todo] is a valid runtime subscript (Task is Generic)."""
        alias = Task[Todo]
        assert alias is not None

    def test_task_aliases_defined(self) -> None:
        """TodoTask/RunningTask/DoneTask/AnyTask aliases exist in model module."""
        from yascheduler.domain import model

        assert hasattr(model, "TodoTask")
        assert hasattr(model, "RunningTask")
        assert hasattr(model, "DoneTask")
        assert hasattr(model, "AnyTask")

    def test_task_aliases_in_all(self) -> None:
        """Narrow aliases are exported via __all__."""
        from yascheduler.domain import model

        for name in ("TodoTask", "RunningTask", "DoneTask", "AnyTask"):
            assert name in model.__all__


class TestLocalFolderPlacement:
    """local_folder is a status-independent Task field (D6): preserved across run(), overwritten by complete/fail."""

    def test_submit_intent_survives_run(self) -> None:
        task = _make_task(local_folder="/submit")
        running = task.run(NodeId(7), "/r")
        assert running.local_folder == "/submit"

    def test_complete_overwrites_local_folder_with_download_path(self) -> None:
        task = _make_task(local_folder="/submit")
        running = task.run(NodeId(7), "/r")
        done = running.complete(local_folder="/download", remote_folder="/r")
        assert done.local_folder == "/download"

    def test_fail_overwrites_local_folder_with_download_path(self) -> None:
        task = _make_task(local_folder="/submit")
        running = task.run(NodeId(7), "/r")
        failed = running.fail("boom", local_folder="/download", remote_folder="/r")
        assert failed.local_folder == "/download"

    def test_todo_state_has_no_local_folder(self) -> None:
        assert not hasattr(Todo(), "local_folder")

    def test_running_state_has_no_local_folder(self) -> None:
        running = Running(allocated_node_id=NodeId(7), remote_folder="/r")
        assert not hasattr(running, "local_folder")

    def test_done_state_has_no_local_folder(self) -> None:
        assert not hasattr(Done(), "local_folder")

    def test_task_has_local_folder_attribute(self) -> None:
        task = _make_task()
        assert hasattr(task, "local_folder")
        assert task.local_folder is None


class TestFreeHelpers:
    """Free any-status reader functions dispatch on state, return Optional."""

    def test_helpers_in_all(self) -> None:
        from yascheduler.domain import model

        for name in (
            "allocated_node_id_of",
            "remote_folder_of",
            "error_of",
        ):
            assert name in model.__all__

    def test_allocated_node_id_of_dispatches_on_state(self) -> None:
        from yascheduler.domain.model import allocated_node_id_of

        todo = _make_task()
        assert allocated_node_id_of(todo) is None
        running = _make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert allocated_node_id_of(running) == NodeId(7)
        done = _make_task(state=Done(allocated_node_id=NodeId(9)))
        assert allocated_node_id_of(done) == NodeId(9)
        done_none = _make_task(state=Done())
        assert allocated_node_id_of(done_none) is None

    def test_remote_folder_of_dispatches_on_state(self) -> None:

        assert remote_folder_of(_make_task()) is None
        assert (
            remote_folder_of(_make_task(state=Todo(remote_folder="/prep"))) == "/prep"
        )
        running = _make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert remote_folder_of(running) == "/r"
        assert remote_folder_of(_make_task(state=Done(remote_folder="/d"))) == "/d"

    def test_error_of_dispatches_on_state(self) -> None:
        from yascheduler.domain.model import error_of

        assert error_of(_make_task()) is None
        running = _make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert error_of(running) is None
        assert error_of(_make_task(state=Done(error="boom"))) == "boom"
        assert error_of(_make_task(state=Done())) is None


class TestNarrowingHelpers:
    """is_todo/is_running/is_done return correct bool per task state."""

    def test_helpers_in_all(self) -> None:
        from yascheduler.domain import model

        for name in ("is_todo", "is_running", "is_done"):
            assert name in model.__all__

    def test_is_todo_returns_true_only_for_todo(self) -> None:
        from yascheduler.domain.model import is_todo

        assert is_todo(_make_task()) is True
        running = _make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert is_todo(running) is False
        assert is_todo(_make_task(state=Done())) is False

    def test_is_running_returns_true_only_for_running(self) -> None:
        from yascheduler.domain.model import is_running

        assert is_running(_make_task()) is False
        running = _make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert is_running(running) is True
        assert is_running(_make_task(state=Done())) is False

    def test_is_done_returns_true_only_for_done(self) -> None:
        from yascheduler.domain.model import is_done

        assert is_done(_make_task()) is False
        running = _make_task(
            state=Running(allocated_node_id=NodeId(7), remote_folder="/r")
        )
        assert is_done(running) is False
        assert is_done(_make_task(state=Done())) is True
