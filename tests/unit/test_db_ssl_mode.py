"""Unit tests: PostgresDbConfig ssl mode validation + cached SSL context reuse."""
import pytest

pytestmark = pytest.mark.unit


def test_db_config_ssl_validation():
    """ssl mode defaults to 'prefer' and rejects unknown values."""
    from yascheduler.infra.persistence.db_config import PostgresDbConfig

    assert PostgresDbConfig().ssl == "prefer"
    assert PostgresDbConfig(ssl="off").ssl == "off"
    assert PostgresDbConfig(ssl="on").ssl == "on"
    with pytest.raises(ValueError, match="ssl must be one of prefer/on/off"):
        PostgresDbConfig(ssl="maybe")


def test_ssl_context_cached_per_process():
    """The 'on' mode reuses one process-wide SSLContext."""
    pytest.importorskip("pg8000")
    from yascheduler.infra.persistence import postgres_uow

    assert postgres_uow._get_ssl_context() is postgres_uow._get_ssl_context()


def test_create_connection_ssl_off_passes_false(monkeypatch):
    """ssl='off' must pass ssl_context=False (no context built at all)."""
    pytest.importorskip("pg8000")
    import yascheduler.infra.persistence.postgres_uow as mod
    from yascheduler.infra.persistence.db_config import PostgresDbConfig
    from yascheduler.infra.persistence.postgres_uow import PostgresUnitOfWork

    captured: dict = {}

    class FakeConnection:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(mod, "Connection", FakeConnection)

    uow = object.__new__(PostgresUnitOfWork)
    uow._config = PostgresDbConfig(ssl="off")
    uow._create_connection()
    assert captured["ssl_context"] is False

    captured.clear()
    uow._config = PostgresDbConfig(ssl="prefer")
    uow._create_connection()
    assert "ssl_context" not in captured  # default behaviour unchanged
