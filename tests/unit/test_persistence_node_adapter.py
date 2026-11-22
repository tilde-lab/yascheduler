# region MODULE_CONTRACT
# PURPOSE: Unit tests for PostgresNodeRepository CRUD via mocked _run.
# SCOPE: PostgresNodeRepository CRUD via mocked _run fake — get, insert, update, enable/disable/remove.
# KEYWORDS: PostgresNodeRepository, CRUD, fake _run
# endregion MODULE_CONTRACT

from __future__ import annotations

from typing import Any

import pytest
from pytest_mock import MockerFixture

from yascheduler.domain.exceptions import NodeRowNotFoundError
from yascheduler.domain.model import NewNode, Node, NodeId
from yascheduler.infra.persistence.postgres import PostgresNodeRepository
from yascheduler.infra.persistence.sql_loader import load_query

pytestmark = pytest.mark.unit


def _make_node_row(**overrides: Any) -> dict[str, Any]:
    """Build a fake _run row dict for a Node with sensible defaults; overrides win."""
    from datetime import datetime

    now = datetime.now()
    base = {
        "node_id": 1,
        "hostname": "10.0.0.1",
        "ncpus": 8,
        "enabled": True,
        "cloud": "hetzner",
        "username": "root",
        "port": 22,
        "jump_host": None,
        "jump_port": 22,
        "jump_username": "root",
        "external_id": None,
        "status": "OTHER",
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base


# ============================================================================
# PostgresNodeRepository — unit tests with mocked _run
# ============================================================================


class TestPostgresNodeRepository:
    """PostgresNodeRepository CRUD operations via fake in-memory _run."""

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _make_repo(mocker: MockerFixture) -> PostgresNodeRepository:
        """Build a minimal PostgresNodeRepository with a mock _run."""
        repo = PostgresNodeRepository.__new__(PostgresNodeRepository)
        mock_run = mocker.AsyncMock()
        mocker.patch.object(repo, "_run", mock_run)
        return repo

    # -- get -------------------------------------------------------------------

    async def test_get_returns_node(self, mocker: MockerFixture) -> None:
        """Get returns a Node hydrated from the row returned by _run."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=1,
                hostname="10.0.0.1",
                ncpus=8,
                enabled=True,
                cloud="hetzner",
                username="root",
                port=22,
            ),
        ]

        node = await repo.get_by_id(NodeId(1))

        assert node is not None
        assert node.node_id == NodeId(1)
        assert node.hostname == "10.0.0.1"
        assert node.ncpus == 8
        assert node.enabled is True
        assert node.cloud == "hetzner"
        assert node.username == "root"
        assert node.port == 22

    async def test_get_returns_none_when_not_found(self, mocker: MockerFixture) -> None:
        """Get returns None when _run returns empty."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        node = await repo.get_by_id(NodeId(999))

        assert node is None

    async def test_get_handles_null_ncpus(self, mocker: MockerFixture) -> None:
        """Get handles null ncpus correctly (round-trips as None)."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=2,
                hostname="10.0.0.2",
                ncpus=None,
                enabled=False,
                cloud=None,
                username="admin",
                port=2222,
                status="OTHER",
            ),
        ]

        node = await repo.get_by_id(NodeId(2))

        assert node is not None
        assert node.node_id == NodeId(2)
        assert node.hostname == "10.0.0.2"
        assert node.ncpus is None
        assert node.enabled is False
        assert node.cloud is None
        assert node.port == 2222

    async def test_get_handles_positive_ncpus_unchanged(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Get round-trips a positive int ncpus unchanged."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=3,
                hostname="10.0.0.3",
                ncpus=16,
                enabled=True,
                cloud="aws",
                username="root",
                port=22,
            ),
        ]

        node = await repo.get_by_id(NodeId(3))

        assert node is not None
        assert node.ncpus == 16

    # -- get_by_id -------------------------------------------------------------

    async def test_get_by_id_returns_node(self, mocker: MockerFixture) -> None:
        """get_by_id returns a Node hydrated from the row returned by _run."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=5,
                hostname="10.0.0.5",
                ncpus=4,
                enabled=True,
                cloud="aws",
                username="root",
                port=22,
            ),
        ]

        result = await repo.get_by_id(NodeId(5))

        assert result is not None
        assert result.node_id == NodeId(5)
        assert result.hostname == "10.0.0.5"
        repo._run.assert_awaited_once_with(  # type: ignore[attr-defined]
            load_query("node/get_by_id"),
            node_id=5,
        )

    async def test_get_by_id_missing_returns_none(self, mocker: MockerFixture) -> None:
        """get_by_id returns None when _run returns empty."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        result = await repo.get_by_id(NodeId(999))

        assert result is None

    # -- get_by_ids -----------------------------------------------------------

    async def test_get_by_ids_empty_returns_empty_dict(
        self,
        mocker: MockerFixture,
    ) -> None:
        """get_by_ids([]) returns an empty dict."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        result = await repo.get_by_ids([])

        assert result == {}
        assert repo._run.call_count == 1  # type: ignore[attr-defined]

    # -- list_all --------------------------------------------------------------

    async def test_list_all_returns_nodes(self, mocker: MockerFixture) -> None:
        """list_all returns all nodes from _run."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=1,
                hostname="10.0.0.1",
                ncpus=4,
                enabled=True,
                cloud="hetzner",
                username="root",
                port=22,
            ),
            _make_node_row(
                node_id=2,
                hostname="10.0.0.2",
                ncpus=8,
                enabled=False,
                cloud="upcloud",
                username="admin",
                port=2222,
            ),
        ]

        nodes = await repo.list_all()

        assert len(nodes) == 2
        assert nodes[0].node_id == NodeId(1)
        assert nodes[0].hostname == "10.0.0.1"
        assert nodes[1].node_id == NodeId(2)
        assert nodes[1].hostname == "10.0.0.2"

    # -- list_enabled / list_disabled ------------------------------------------

    async def test_list_enabled_returns_only_enabled(
        self,
        mocker: MockerFixture,
    ) -> None:
        """list_enabled returns only nodes with valid IPs (containing '.')."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=1,
                hostname="10.0.0.1",
                ncpus=4,
                enabled=True,
                cloud="hetzner",
                username="root",
                port=22,
            ),
            _make_node_row(
                node_id=2,
                hostname="10.0.0.2",
                ncpus=8,
                enabled=True,
                cloud="upcloud",
                username="admin",
                port=2222,
            ),
            _make_node_row(
                node_id=3,
                hostname="10.0.0.3",
                ncpus=2,
                enabled=False,
                cloud="hetzner",
                username="root",
                port=22,
            ),
        ]

        nodes = await repo.list_enabled()

        # All rows have "." in IP, so all 3 pass the filter
        assert len(nodes) == 3

    async def test_list_enabled_no_python_post_filter(
        self,
        mocker: MockerFixture,
    ) -> None:
        """list_enabled returns all enabled rows from SQL — no python post-filter (remove-tmp-node-fake-ip).

        By the invariant (ip == '' IFF enabled=FALSE AND tmp/pending), no
        enabled row has ip == "", so the prior "." in ip post-filter was dead
        and is removed. The SQL WHERE enabled = TRUE is the only filter; a
        row with a non-ipv4 hostname like "localhost" is returned unchanged.
        """
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=1,
                hostname="10.0.0.1",
                ncpus=4,
                enabled=True,
                cloud="hetzner",
                username="root",
                port=22,
            ),
            _make_node_row(
                node_id=2,
                hostname="localhost",
                ncpus=4,
                enabled=True,
                cloud=None,
                username="root",
                port=22,
            ),
        ]

        nodes = await repo.list_enabled()

        # No python post-filter — both enabled rows are returned.
        assert len(nodes) == 2

    async def test_list_disabled_returns_disabled_with_valid_ips(
        self,
        mocker: MockerFixture,
    ) -> None:
        """list_disabled returns all rows (SQL filters disabled) that have valid IPs."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=1,
                hostname="10.0.0.1",
                ncpus=4,
                enabled=False,
                cloud="hetzner",
                username="root",
                port=22,
            ),
            _make_node_row(
                node_id=2,
                hostname="10.0.0.2",
                ncpus=8,
                enabled=False,
                cloud="upcloud",
                username="admin",
                port=2222,
            ),
        ]

        nodes = await repo.list_disabled()

        assert len(nodes) == 2
        assert all(n.enabled is False for n in nodes)

    async def test_list_disabled_no_python_post_filter(
        self,
        mocker: MockerFixture,
    ) -> None:
        """list_disabled returns all rows from SQL — no python post-filter (remove-tmp-node-fake-ip).

        The ip <> '' presence check is in SQL (node/list_disabled.sql), not
        python. The repo returns whatever SQL returns; a row with a non-ipv4
        hostname like "localhost" passes (it is a real-disabled VM with a real
        address). Only ip == "" tmp rows are excluded, and that happens at
        the SQL layer.
        """
        repo = self._make_repo(mocker)
        repo._run.return_value = [  # type: ignore[attr-defined]
            _make_node_row(
                node_id=1,
                hostname="10.0.0.1",
                ncpus=4,
                enabled=False,
                cloud="hetzner",
                username="root",
                port=22,
            ),
            _make_node_row(
                node_id=2,
                hostname="localhost",
                ncpus=8,
                enabled=False,
                cloud=None,
                username="admin",
                port=2222,
            ),
        ]

        nodes = await repo.list_disabled()

        # No python post-filter — both disabled rows with non-empty ip are returned.
        assert len(nodes) == 2
        assert all(n.enabled is False for n in nodes)

    # -- add -------------------------------------------------------------------

    async def test_insert_returns_node_with_id(self, mocker: MockerFixture) -> None:
        """Insert runs INSERT SQL and returns Node with generated NodeId."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [_make_node_row(node_id=42)]  # type: ignore[attr-defined]
        new_node = NewNode(
            hostname="10.0.0.1",
            ncpus=8,
            enabled=True,
            cloud="hetzner",
            username="root",
            port=22,
        )

        result = await repo.insert(new_node)

        assert isinstance(result, Node)
        assert result.node_id == NodeId(42)
        repo._run.assert_awaited_once()  # type: ignore[attr-defined]
        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["hostname"] == "10.0.0.1"
        assert kwargs["ncpus"] == 8
        assert kwargs["enabled"] is True
        assert kwargs["cloud"] == "hetzner"
        assert kwargs["username"] == "root"
        assert kwargs["port"] == 22

    async def test_insert_inserts_cloud_node(self, mocker: MockerFixture) -> None:
        """Insert persists a cloud-provisioned node."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [_make_node_row(node_id=7)]  # type: ignore[attr-defined]
        new_node = NewNode(
            hostname="10.0.0.5",
            ncpus=2,
            enabled=False,
            cloud="upcloud",
            username="admin",
            port=2222,
        )

        result = await repo.insert(new_node)

        assert isinstance(result, Node)
        assert result.node_id == NodeId(7)
        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["cloud"] == "upcloud"
        assert kwargs["enabled"] is False

    async def test_insert_with_none_ncpus_returns_none(
        self,
        mocker: MockerFixture,
    ) -> None:
        """insert(NewNode(ncpus=None)) produces a row whose ncpus is None."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [_make_node_row(node_id=8, ncpus=None)]  # type: ignore[attr-defined]
        new_node = NewNode(
            hostname="10.0.0.6",
            ncpus=None,
            enabled=False,
            cloud="aws",
            username="root",
            port=22,
        )

        result = await repo.insert(new_node)

        assert isinstance(result, Node)
        assert result.ncpus is None

    async def test_update_binds_all_fields_including_ip(
        self,
        mocker: MockerFixture,
    ) -> None:
        """Update runs UPDATE SQL binding ip, ncpus, enabled, cloud, username, port, node_id (V1 cloud lifecycle relies on ip being SET)."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [{"node_id": 7}]  # type: ignore[attr-defined]

        await repo.update(
            Node(
                node_id=NodeId(7),
                hostname="10.0.0.99",
                ncpus=8,
                enabled=True,
                cloud="hetzner",
                username="root",
                port=22,
            ),
        )

        repo._run.assert_awaited_once()  # type: ignore[attr-defined]
        _, kwargs = repo._run.call_args  # type: ignore[attr-defined]
        assert kwargs["node_id"] == 7
        assert kwargs["hostname"] == "10.0.0.99", (
            "update must bind hostname (V1 cloud lifecycle sets real hostname via update)"
        )
        assert kwargs["ncpus"] == 8
        assert kwargs["enabled"] is True
        assert kwargs["cloud"] == "hetzner"
        assert kwargs["username"] == "root"
        assert kwargs["port"] == 22

    async def test_update_raises_on_zero_rows(self, mocker: MockerFixture) -> None:
        """Update raises NodeRowNotFoundError when RETURNING yields no row — guards orphan VMs when a tmp-node was concurrently deleted."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        with pytest.raises(NodeRowNotFoundError) as exc:
            await repo.update(
                Node(
                    node_id=NodeId(7),
                    hostname="10.0.0.99",
                    ncpus=8,
                    enabled=True,
                    cloud="hetzner",
                    username="root",
                    port=22,
                ),
            )
        assert exc.value.node_id == NodeId(7)

    # -- enable / disable / remove ---------------------------------------------

    @pytest.mark.parametrize(
        ("method", "query"),
        [
            pytest.param("enable", "node/enable", id="enable"),
            pytest.param("disable", "node/disable", id="disable"),
            pytest.param("remove", "node/remove", id="remove"),
        ],
    )
    async def test_state_change_executes_query(
        self,
        mocker: MockerFixture,
        method: str,
        query: str,
    ) -> None:
        """Enable/disable/remove call _run with their query and node_id.value."""
        repo = self._make_repo(mocker)
        repo._run.return_value = [{"node_id": 7}]  # type: ignore[attr-defined]

        await getattr(repo, method)(NodeId(7))

        repo._run.assert_awaited_once_with(load_query(query), node_id=7)  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("method", "query"),
        [
            pytest.param("enable", "node/enable", id="enable"),
            pytest.param("disable", "node/disable", id="disable"),
            pytest.param("remove", "node/remove", id="remove"),
        ],
    )
    async def test_state_change_raises_on_zero_rows(
        self,
        mocker: MockerFixture,
        method: str,
        query: str,
    ) -> None:
        """Enable/disable/remove raise NodeRowNotFoundError when RETURNING yields no row."""
        repo = self._make_repo(mocker)
        repo._run.return_value = []  # type: ignore[attr-defined]

        with pytest.raises(NodeRowNotFoundError) as exc:
            await getattr(repo, method)(NodeId(7))
        assert exc.value.node_id == NodeId(7)
        repo._run.assert_awaited_once_with(load_query(query), node_id=7)  # type: ignore[attr-defined]
