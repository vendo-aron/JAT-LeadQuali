"""Invariant 4, asserted on the SQL itself, with no database anywhere.

``CLAUDE.md`` invariant 4: ``tenant_id`` on every table and every repository method filters
on it. ``tests/integration/test_store_tenants.py`` proves the behaviour — one tenant cannot
reach another's key — but it skips without Docker, which is most machines and, as
configured, CI. A missing ``WHERE`` in a *write* is exactly the kind of defect that must not
depend on whether Postgres happened to be running.

So the statements are built and compiled here instead. The session factory is a double that
records what it was handed; nothing connects, nothing executes, and the assertion is on the
text of the SQL that would have been sent. White-box on purpose: "every write names its
tenant" is a structural claim, so a structural test is the honest way to state it — the same
reasoning as ``test_layering.py`` reading the AST rather than trusting a convention.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from leadquali.adapters.store_tenants import (
    PostgresIngestCredentials,
    PostgresTenantAdminStore,
)
from leadquali.app.tenants import TenantStatus

#: A Postgres dialect to compile against. SQLAlchemy does not annotate the constructor, so
#: it is cast once here rather than silenced at every call site.
DIALECT: Any = cast("Callable[[], Any]", postgresql.dialect)()

NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
SLUG = "acme-demo"
KEY_ID = "3f1c9a02b7d45e68"


class FakeResult:
    """Whatever the caller asks for, shaped so the method under test runs to its end."""

    def __init__(self, row: Any) -> None:
        self._row = row

    def one(self) -> Any:
        return self._row

    def one_or_none(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return [self._row] if self._row is not None else []

    def scalar_one_or_none(self) -> Any:
        return self._row


class RecordingSession:
    """A session that records statements and returns a canned row."""

    def __init__(self, recorder: list[Any], row: Any) -> None:
        self._recorder = recorder
        self._row = row

    def execute(self, statement: Any) -> FakeResult:
        self._recorder.append(statement)
        return FakeResult(self._row)


class RecordingSessions:
    """Stands in for a ``sessionmaker``: ``begin()`` is the only thing the store uses."""

    def __init__(self, row: Any = None) -> None:
        self.statements: list[Any] = []
        self.row = row

    @contextmanager
    def begin(self) -> Iterator[RecordingSession]:
        yield RecordingSession(self.statements, self.row)


class AnyRow:
    """One row that answers to every column any of these methods reads.

    Deliberately permissive: what is under test is the SQL that was built, not the mapping
    of the result, so the double's job is only to let each method run to its end.
    """

    def __init__(self) -> None:
        self.id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        self.slug = SLUG
        self.name = "Acme"
        self.status = "active"
        self.icp_config: dict[str, Any] = {}
        self.hmac_secret_ref = None
        self.rate_limit_per_minute = 60
        self.rate_limit_burst = 10
        self.created_at = NOW
        self.updated_at = NOW
        self.key_id = KEY_ID
        self.key_prefix = f"lq_live_{KEY_ID}"
        self.label = None
        self.key_hash = "$argon2id$stub$x"
        self.expires_at = None
        self.revoked_at = None
        self.last_used_at = None

    def __getitem__(self, index: int) -> Any:
        """``rate_limit_for`` reads its two columns positionally."""
        return (self.rate_limit_per_minute, self.rate_limit_burst)[index]


def rendered(statement: Any) -> str:
    """The SQL as Postgres would receive it, with its bound parameters appended.

    Compiled without ``literal_binds`` because a ``jsonb`` value has no literal renderer,
    and the parameters are appended as text instead — so an assertion can look for the
    predicate in the SQL *and* for the tenant slug among the values, which together are
    what "this statement filters on the tenant" actually means.
    """
    compiled = statement.compile(dialect=DIALECT)
    return f"{compiled}\n-- params: {compiled.params}"


def writes(store_call: Callable[[sessionmaker[Session]], object], row: Any) -> list[str]:
    """Run one store method against a recording double and return the SQL it emitted."""
    recorder = RecordingSessions(row)
    store_call(cast("sessionmaker[Session]", recorder))
    return [rendered(statement) for statement in recorder.statements]


# ---------------------------------------------------------------------------- writes


def test_expiring_a_key_names_the_tenant_as_well_as_the_key() -> None:
    """The one the integration suite covers and nothing offline did.

    ``key_id`` is globally unique, so this predicate is redundant against the index — and
    that is precisely the argument that removes it, one refactor at a time, until a support
    tool passing the wrong slug sets somebody else's rotation deadline.
    """
    (sql,) = writes(
        lambda s: PostgresTenantAdminStore(s).expire_key(slug=SLUG, key_id=KEY_ID, expires_at=NOW),
        AnyRow(),
    )
    assert "UPDATE tenant_api_keys" in sql
    assert KEY_ID in sql
    assert "tenants.slug" in sql, "expire_key does not filter on the tenant"
    assert SLUG in sql


def test_revoking_a_key_names_the_tenant_as_well_as_the_key() -> None:
    sql = writes(
        lambda s: PostgresTenantAdminStore(s).revoke_key(slug=SLUG, key_id=KEY_ID, revoked_at=NOW),
        AnyRow(),
    )[0]
    assert "UPDATE tenant_api_keys" in sql
    assert "tenants.slug" in sql, "revoke_key does not filter on the tenant"
    assert SLUG in sql


def test_recording_a_keys_last_use_names_the_tenant() -> None:
    """The only write on the request path, and the one place the docstring said invariant 4
    held while the SQL did not."""
    recorder = RecordingSessions(None)
    source = PostgresIngestCredentials(
        cast("sessionmaker[Session]", recorder),
        verifier=_AlwaysNo(),
        resolver=_NoSecrets(),
        now=lambda: NOW,
    )
    source._touch(key_id=KEY_ID, tenant_slug=SLUG, now=NOW)

    (sql,) = [rendered(statement) for statement in recorder.statements]
    assert "UPDATE tenant_api_keys" in sql
    assert "last_used_at" in sql
    assert "tenants.slug" in sql, "_touch does not filter on the tenant"
    assert SLUG in sql


@pytest.mark.parametrize(
    "method",
    [
        lambda store: store.update_config(slug=SLUG, config={"tenant_id": SLUG}),
        lambda store: store.set_status(slug=SLUG, status=TenantStatus.SUSPENDED),
    ],
    ids=["update_config", "set_status"],
)
def test_every_tenant_write_filters_on_the_tenant(method: Any) -> None:
    (sql,) = writes(lambda s: method(PostgresTenantAdminStore(s)), AnyRow())
    assert "UPDATE tenants" in sql
    assert "tenants.slug" in sql
    assert SLUG in sql


# ----------------------------------------------------------------------------- reads


@pytest.mark.parametrize(
    "method",
    [
        lambda store: store.get_tenant(slug=SLUG),
        lambda store: store.list_keys(slug=SLUG),
        lambda store: store.rate_limit_for(SLUG),
    ],
    ids=["get_tenant", "list_keys", "rate_limit_for"],
)
def test_every_per_tenant_read_filters_on_the_tenant(method: Any) -> None:
    recorder = RecordingSessions(AnyRow())
    method(PostgresTenantAdminStore(cast("sessionmaker[Session]", recorder)))
    for statement in recorder.statements:
        sql = rendered(statement)
        assert "tenants.slug" in sql
        assert SLUG in sql


def test_the_credential_lookup_is_a_single_indexed_read_by_key_id() -> None:
    """The claim the whole "argon2 is affordable" design rests on: one statement, keyed on
    the unique ``key_id``, and no scan of anything."""
    recorder = RecordingSessions(None)
    source = PostgresIngestCredentials(
        cast("sessionmaker[Session]", recorder),
        verifier=_AlwaysNo(),
        resolver=_NoSecrets(),
        now=lambda: NOW,
    )
    source.resolve(tenant_id=SLUG, api_key=f"lq_live_{KEY_ID}_" + "a" * 43)

    assert len(recorder.statements) == 1
    sql = rendered(recorder.statements[0])
    assert "SELECT" in sql
    assert "tenant_api_keys.key_id = " in sql
    assert KEY_ID in sql
    assert "JOIN tenants" in sql


class _AlwaysNo:
    def verify_secret(self, *, key_id: str, secret: str, key_hash: str) -> bool:
        del key_id, secret, key_hash
        return False


class _NoSecrets:
    def resolve(self, secret_arn: str) -> str:
        del secret_arn
        raise AssertionError("no secret should be read in these tests")

    def resolve_mapping(self, secret_arn: str) -> dict[str, str]:
        del secret_arn
        return {}
