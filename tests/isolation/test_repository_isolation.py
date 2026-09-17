"""Every repository method, swept for the tenant filter it is supposed to have.

This is the criterion the issue actually turns on: *a newly added repository method that
forgets its tenant filter fails the suite*. A hand-written test per method does not do
that — it passes happily while the new method sits untested — so the sweep is built by
introspection over :data:`~tests.isolation.repositories.REPOSITORIES` and it fails in three
distinct ways, none of which is a skip:

1. a public method with no tenant-scoping parameter, not on the allowlist and not a
   documented ``fleet_`` exception, fails :func:`test_every_public_method_is_accounted_for`;
2. a method that has one and no argument recipe fails the sweep with a message telling the
   author to write one;
3. a method whose statement no longer carries a tenant predicate fails
   :func:`test_every_statement_is_scoped_to_the_tenant_it_was_given`.

**Why this runs without a database.** The two reviews behind this issue both found the same
hole: in #31 the Postgres implementation's cross-tenant filter could be deleted with all
1,727 tests green, because every security property was proved against an in-memory double
and the adapter was only covered by Docker-gated tests; in #33 three mutations to the
billing query's result mapping left 1,903 tests green for the same reason. A property
asserted only by a skipped ``integration`` test is not asserted. So the sweep compiles each
method's statement against the real ``postgresql`` dialect and reads the tenant off the SQL
and off the bound parameters. ``test_repository_isolation_integration.py`` runs the same
recipes against a real server and asserts the behaviour; it skips without Docker, and it is
the *second* line of evidence rather than the only one.

**What the offline half does not prove.** That Postgres agrees. A predicate can be present
and wrong — comparing the tenant to itself, say. That is what the integration half is for,
and ``docs/tenant-isolation.md`` says so rather than implying the SQL check is the whole
story.
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final, NamedTuple

import pytest
from sqlalchemy import ClauseElement

from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.store_postgres import PostgresLeadStore
from leadquali.adapters.store_tenants import PostgresIngestCredentials
from leadquali.api.signing import ACTIVE_STATUS, AuthFailure, CredentialRejected, IngestCredential
from tests.isolation.repositories import (
    ALLOWLIST,
    FLEET_METHODS,
    KEY_ID_A,
    KEY_SECRET_A,
    RECIPES,
    REPOSITORIES,
    SIGNING_SECRET_REF,
    TENANT_A,
    TENANT_A_UUID,
    TENANT_B,
    TENANT_B_UUID,
    ArgumentRecipe,
    DictSecretResolver,
    Repository,
    api_key_for,
    recipe_for,
    tenant_parameter_of,
)
from tests.sqlcapture import CannedResult, SqlCapture, parameters, sql_text

# --------------------------------------------------------------- what counts as scoping

#: Which column carries the tenant, per table. ``tenants`` is its own tenant: a statement
#: about one customer's row is scoped by ``tenants.id`` or ``tenants.slug``, and there is no
#: ``tenants.tenant_id`` to look for.
_TENANT_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    "tenants": frozenset({"id", "slug"}),
}
_DEFAULT_TENANT_COLUMN: Final[str] = "tenant_id"

#: A tenant column being compared to something, anywhere in the statement — a ``WHERE``, a
#: join, an ``ON CONFLICT ... WHERE``, or the ``WHERE`` of a correlated sub-select. Matching
#: the comparison rather than the bare column name is what makes this fail when a filter is
#: deleted from a statement that still *mentions* the column in its select list.
_TENANT_PREDICATE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:\w+\.)?tenant_id\s*(?:=|\bin\b)|\btenants\.(?:id|slug)\s*(?:=|\bin\b)"
)

#: ``INSERT INTO <table> (<columns>)``, so an insert can be scoped by the column it writes
#: the tenant into rather than by a predicate it has no reason to carry.
_INSERT_TARGET: Final[re.Pattern[str]] = re.compile(r"insert\s+into\s+(\w+)\s*\(([^)]*)\)")


def _tenant_columns_of(table: str) -> frozenset[str]:
    return _TENANT_COLUMNS.get(table, frozenset({_DEFAULT_TENANT_COLUMN}))


def scoping_evidence(sql: str) -> frozenset[str]:
    """Every reason this statement can be said to be scoped to one tenant.

    Two kinds, and a statement needs at least one of them:

    ``"predicate"``
        a tenant column compared to a value. Every read and every update must have this.
    ``"inserted column"``
        the tenant column is among the columns an ``INSERT`` writes. An insert has no rows
        to filter; what makes it tenant-safe is that the tenant travels in the row, and the
        composite foreign key then refuses a row whose tenant does not own its lead.
    """
    found: set[str] = set()
    if _TENANT_PREDICATE.search(sql):
        found.add("predicate")
    target = _INSERT_TARGET.search(sql)
    if target is not None:
        columns = {column.strip() for column in target.group(2).split(",")}
        if columns & _tenant_columns_of(target.group(1)):
            found.add("inserted column")
    return frozenset(found)


def _identity_strings(*values: object) -> frozenset[str]:
    """One tenant's identity in every spelling a bound parameter could carry it in."""
    return frozenset(str(value) for value in values)


#: Pinned so the rotation-overlap arithmetic inside the credential source is deterministic.
_NOW: Final[dt.datetime] = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)

_B_IDENTITY: Final[frozenset[str]] = _identity_strings(TENANT_B, TENANT_B_UUID)
_A_IDENTITY: Final[frozenset[str]] = _identity_strings(TENANT_A, TENANT_A_UUID)


def _bound_identities(statement: ClauseElement) -> frozenset[str]:
    """Every bound parameter value, as a string, for comparison against an identity."""
    return frozenset(str(value) for value in parameters(statement).values())


# --------------------------------------------------------------------- the enumeration


def _public_methods(cls: type) -> dict[str, Any]:
    """Every public callable on a class, however it is spelled.

    Plain methods, ``classmethod``\\ s, ``staticmethod``\\ s and properties, because the
    point of the enumeration is that *any* new way of reaching the database is caught. An
    earlier version of this used ``inspect.isfunction``, which silently excluded every
    ``classmethod`` — so a ``from_url``-shaped constructor that also ran a query would have
    been invisible to the sweep.
    """
    members: dict[str, Any] = {}
    for name in dir(cls):
        if name.startswith("_"):
            continue
        static = inspect.getattr_static(cls, name)
        if isinstance(static, property):
            members[name] = static.fget
            continue
        member = getattr(cls, name)
        if inspect.isroutine(member):
            members[name] = member
    return members


def _tenant_scoped_methods(repository: Repository) -> list[str]:
    """The public methods of one repository that take a tenant, in a stable order."""
    scoped: list[str] = []
    for name, member in sorted(_public_methods(repository.cls).items()):
        if name in ALLOWLIST.get(repository.cls, {}):
            continue
        if name in FLEET_METHODS.get(repository.cls, frozenset()):
            continue
        if tenant_parameter_of(inspect.signature(member).parameters) is not None:
            scoped.append(name)
    return scoped


#: Every (repository, method) pair the sweep runs over, built at collection time. A method
#: added to one of the repositories appears here without anybody touching this file.
SWEEP: Final[tuple[tuple[Repository, str], ...]] = tuple(
    (repository, method)
    for repository in REPOSITORIES
    for method in _tenant_scoped_methods(repository)
)


def _sweep_id(case: tuple[Repository, str]) -> str:
    repository, method = case
    return f"{repository.name}.{method}"


def test_the_sweep_is_not_empty() -> None:
    """A sweep that selected nothing would be green and worthless.

    The floor is the six repositories times at least one method each; the real number is
    higher, and it is asserted loosely on purpose — this test is here to catch an
    introspection bug that silently selects nothing, not to be updated every time a method
    is added.
    """
    assert len(SWEEP) >= len(REPOSITORIES), SWEEP
    assert len({repository.name for repository, _ in SWEEP}) == len(REPOSITORIES)


@pytest.mark.parametrize("repository", REPOSITORIES, ids=lambda item: item.name)
def test_every_public_method_is_accounted_for(repository: Repository) -> None:
    """A public method that names no tenant is a failure unless it is explicitly excused.

    This is the half of the sweep that catches the *newly added* method. Writing
    ``def leads_for(self, lead_id: str)`` on the lead store — no tenant anywhere — would
    pass every other test in this repository and fail here, by name.
    """
    allowed = ALLOWLIST.get(repository.cls, {})
    fleet = FLEET_METHODS.get(repository.cls, frozenset())
    unaccounted: list[str] = []
    for name, member in sorted(_public_methods(repository.cls).items()):
        if name in allowed or name in fleet:
            continue
        if tenant_parameter_of(inspect.signature(member).parameters) is None:
            unaccounted.append(name)
    assert not unaccounted, (
        f"{repository.name}.{', '.join(unaccounted)} takes no tenant.\n"
        "Every method that reaches the database is scoped to one tenant (CLAUDE.md "
        "invariant 4). If this one genuinely is not — a constructor, or a fleet-wide "
        "operator tool — add it to ALLOWLIST or FLEET_METHODS in "
        "tests/isolation/repositories.py with the reason, and say so in "
        "docs/tenant-isolation.md. Do not delete this test."
    )


@pytest.mark.parametrize("repository", REPOSITORIES, ids=lambda item: item.name)
def test_every_allowlist_entry_carries_a_reason(repository: Repository) -> None:
    """An allowlist is only safe while every entry on it says why it is there."""
    for name, reason in ALLOWLIST.get(repository.cls, {}).items():
        assert name in _public_methods(repository.cls), (
            f"{repository.name}.{name} is allowlisted and no longer exists; remove it"
        )
        assert len(reason) >= 10, f"{repository.name}.{name}: give the exemption a reason"


@pytest.mark.parametrize("repository", REPOSITORIES, ids=lambda item: item.name)
def test_a_fleet_method_says_so_in_its_name_and_takes_no_tenant(repository: Repository) -> None:
    """The documented exception, held to its own contract.

    ``fleet_billable_leads`` and ``fleet_daily_spend`` exist because reconciliation and
    infrastructure allocation need figures across every tenant. Two rules keep that from
    becoming a hole: the name says ``fleet_`` so it is visible at every call site, and the
    method takes no tenant — a fleet query that also accepted a tenant would be a normal
    query somebody forgot to filter.
    """
    for name in FLEET_METHODS.get(repository.cls, frozenset()):
        member = _public_methods(repository.cls).get(name)
        assert member is not None, f"{repository.name}.{name} is listed and does not exist"
        assert name.startswith("fleet_"), f"{repository.name}.{name} must be named fleet_*"
        signature = inspect.signature(member).parameters
        assert tenant_parameter_of(signature) is None, (
            f"{repository.name}.{name} is a fleet method and takes a tenant"
        )


@pytest.mark.parametrize("repository", REPOSITORIES, ids=lambda item: item.name)
def test_no_recipe_outlives_its_method(repository: Repository) -> None:
    """A recipe for a method that no longer exists is a test that quietly stopped running."""
    selected = set(_tenant_scoped_methods(repository))
    stale = sorted(set(RECIPES.get(repository.cls, {})) - selected)
    assert not stale, (
        f"{repository.name}: RECIPES has entries for {stale}, which are no longer "
        "tenant-scoped public methods. Delete them, or find out why they stopped being "
        "swept."
    )


# -------------------------------------------------------------------------- the sweep


def _call(repository: Repository, method: str, recipe: ArgumentRecipe, capture: SqlCapture) -> Any:
    """Invoke one method with tenant B's identity and the recipe's arguments."""
    store = repository.build(capture.sessions)
    bound = getattr(store, method)
    parameter = tenant_parameter_of(inspect.signature(bound).parameters)
    assert parameter is not None, f"{repository.name}.{method} lost its tenant parameter"
    arguments = dict(recipe.arguments)
    arguments.setdefault(parameter, TENANT_B)
    return bound(**arguments)


def _capture(
    repository: Repository, method: str, recipe: ArgumentRecipe
) -> tuple[ClauseElement, ...]:
    """Every statement one method builds when it is called for tenant B."""
    capture = SqlCapture()
    return capture.run(lambda: _call(repository, method, recipe, capture), results=recipe.results)


class _CredentialRow(NamedTuple):
    """The shape ``PostgresIngestCredentials.resolve`` reads off its one ``SELECT``."""

    slug: str
    status: str
    hmac_secret_ref: str | None
    key_hash: str
    revoked_at: None
    expires_at: None


def _resolve_under(tenant_id: str, *, owner: str) -> Any:
    """Resolve ``owner``'s live key while claiming to be ``tenant_id``.

    The database is stood in for by one canned row, which is what the indexed read on
    ``key_id`` would return — so this exercises the adapter's real decision path with no
    server, which is the whole point: the cross-tenant rejection here is a comparison in
    Python and nothing about it is visible in the SQL.
    """
    from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher

    verifier = Argon2KeyHasher()
    resolver = DictSecretResolver()
    capture = SqlCapture()
    source = PostgresIngestCredentials(
        capture.sessions,
        verifier=verifier,
        resolver=resolver,
        now=lambda: _NOW,
        last_used_coarseness=None,
    )
    row = _CredentialRow(
        slug=owner,
        status=ACTIVE_STATUS,
        hmac_secret_ref=SIGNING_SECRET_REF,
        key_hash=verifier.hash_secret(KEY_SECRET_A),
        revoked_at=None,
        expires_at=None,
    )
    capture.sessions.reset([CannedResult(row=row)])
    lookup = source.resolve(tenant_id=tenant_id, api_key=api_key_for(KEY_ID_A, KEY_SECRET_A))
    return lookup, resolver


def _assert_python_scoped(repository: Repository, method: str) -> None:
    """The behavioural assertion for the one method whose tenant check is not in the SQL."""
    assert (repository.cls, method) == (PostgresIngestCredentials, "resolve"), (
        f"{repository.name}.{method} is marked python_scoped and there is no behavioural "
        "assertion for it. Write one here, or give it a tenant predicate in its SQL."
    )
    lookup, resolver = _resolve_under(TENANT_B, owner=TENANT_A)
    assert isinstance(lookup, CredentialRejected), lookup
    assert lookup.failure is AuthFailure.UNKNOWN_TENANT
    assert resolver.calls == [], (
        "the signing secret was fetched for a credential that was about to be refused"
    )


@pytest.mark.parametrize("case", SWEEP, ids=_sweep_id)
def test_every_statement_is_scoped_to_the_tenant_it_was_given(
    case: tuple[Repository, str],
) -> None:
    """Invariant 4 read off the SQL Postgres would receive, method by method.

    Two assertions per statement, because either one alone has a hole. The predicate check
    alone passes a statement whose filter compares the tenant to a constant; the bound-value
    check alone passes a statement that binds the tenant somewhere harmless, such as an
    ``INSERT`` whose ``WHERE`` was deleted. Together they say: this statement filters on a
    tenant column, and the value it filters on is the tenant the caller named.
    """
    repository, method = case
    recipe = recipe_for(repository.cls, method)
    if recipe.python_scoped:
        _assert_python_scoped(repository, method)
        return

    statements = _capture(repository, method, recipe)
    for index, statement in enumerate(statements):
        sql = sql_text(statement)
        evidence = scoping_evidence(sql)
        assert evidence, (
            f"{repository.name}.{method} statement {index + 1} of {len(statements)} has no "
            f"tenant predicate and writes no tenant column.\n{sql}\n"
            "Every statement is filtered on the tenant, including the ones where the key "
            "is unique anyway (CLAUDE.md invariant 4)."
        )
        assert _bound_identities(statement) & _B_IDENTITY, (
            f"{repository.name}.{method} statement {index + 1} mentions a tenant column but "
            f"does not bind the tenant it was called with.\n{sql}\nbound: "
            f"{sorted(_bound_identities(statement))}"
        )


@pytest.mark.parametrize("case", SWEEP, ids=_sweep_id)
def test_no_statement_carries_the_other_tenants_identity(case: tuple[Repository, str]) -> None:
    """Called for B while holding A's row ids, nothing sends A's identity to the database.

    The complement of the test above, and the one that would catch a filter built from the
    wrong variable — a predicate that is present, binds *a* tenant, and binds the wrong one.
    A's lead id and key id are deliberately still in the arguments: naming another tenant's
    row is exactly the attack, and the answer is that the row is unreachable, not that the
    id was rejected.
    """
    repository, method = case
    recipe = recipe_for(repository.cls, method)
    if recipe.python_scoped:
        pytest.skip("the tenant is not in this method's SQL; see _assert_python_scoped")

    for index, statement in enumerate(_capture(repository, method, recipe)):
        leaked = _bound_identities(statement) & _A_IDENTITY
        assert not leaked, (
            f"{repository.name}.{method} statement {index + 1} binds {sorted(leaked)}, which "
            f"is tenant A, on a call made as tenant B.\n{sql_text(statement)}"
        )


# ------------------------------------------------------------- the statements worth naming


def _upsert_cases() -> Iterator[tuple[str, Repository, str]]:
    """The two statements that repeat the tenant where a unique index already implies it."""
    for repository in REPOSITORIES:
        for method in ("upsert_lead", "record_feedback"):
            if method in RECIPES.get(repository.cls, {}):
                yield f"{repository.name}.{method}", repository, method


@pytest.mark.parametrize(
    "case", list(_upsert_cases()), ids=lambda item: item[0] if isinstance(item, tuple) else ""
)
def test_an_upsert_repeats_the_tenant_in_its_conflict_clause(
    case: tuple[str, Repository, str],
) -> None:
    """``ON CONFLICT ... WHERE tenant_id = ...``, redundant on purpose.

    Both upserts conflict on a constraint that already contains ``tenant_id``, so this
    predicate can never change a result. It is there because invariant 4 is only worth
    something if there is no statement in which the tenant filter is optional — and because
    a reviewer reading the SQL should not have to go and look up what the constraint covers.
    The sweep above would not catch its removal: the tenant would still be an inserted
    column and still be bound.
    """
    _, repository, method = case
    recipe = recipe_for(repository.cls, method)
    statements = _capture(repository, method, recipe)
    upserts = [
        sql_text(statement) for statement in statements if "on conflict" in sql_text(statement)
    ]
    assert upserts, f"{repository.name}.{method} no longer emits an upsert"
    for sql in upserts:
        _, _, conflict = sql.partition("on conflict")
        assert _TENANT_PREDICATE.search(conflict), (
            f"{repository.name}.{method}: the ON CONFLICT clause has lost its tenant "
            f"predicate.\n{sql}"
        )


def test_the_lead_status_update_is_tenant_scoped() -> None:
    """The second statement of both write paths, which is easy to forget about.

    ``record_assessment`` and ``record_routing_event`` each insert a row and then move the
    lead's lifecycle status. The insert is protected by the composite foreign key; the
    ``UPDATE`` is protected by nothing but its own ``WHERE``, and ``leads.id`` alone would
    find the row.
    """
    repository = next(item for item in REPOSITORIES if item.cls is PostgresLeadStore)
    for method in ("record_assessment", "record_routing_event"):
        statements = _capture(repository, method, recipe_for(PostgresLeadStore, method))
        updates = [
            sql_text(item) for item in statements if sql_text(item).startswith("update leads")
        ]
        assert len(updates) == 1, f"{method} no longer updates the lead's status exactly once"
        assert "leads.tenant_id =" in updates[0], updates[0]
        assert "leads.id =" in updates[0], updates[0]


def test_the_credential_source_accepts_a_key_under_its_own_tenant() -> None:
    """The positive control for :func:`_assert_python_scoped`.

    A rejection test with no matching acceptance test proves only that the code says no to
    everything. This is the same row, the same key and the same argon2 verification, asked
    for under the tenant that owns it.
    """
    lookup, resolver = _resolve_under(TENANT_A, owner=TENANT_A)
    assert isinstance(lookup, IngestCredential), lookup
    assert lookup.tenant_id == TENANT_A
    assert lookup.key_id == KEY_ID_A
    assert resolver.calls == [SIGNING_SECRET_REF]


def test_the_last_used_write_is_tenant_scoped_like_every_other_write() -> None:
    """``_touch`` is swept by hand because the sweep cannot reach it.

    It stamps ``tenant_api_keys.last_used_at`` and it is private, so the enumeration above
    skips it — and it is a *write*, on the request path, which makes it the last place to
    tolerate an exception to invariant 4. It has none: the ``UPDATE`` filters on the tenant
    as well as on ``key_id``, redundantly against a unique index and against the row the
    same call has just read, because the exception is what a later reader copies.

    The second assertion is about *when* rather than *where*: the write is the second
    statement, issued after the credential decision has already been made on the read
    before it. A ``last_used`` stamp that ran before the decision would be a write on
    behalf of a caller who turns out not to be authenticated.
    """
    verifier = Argon2KeyHasher()
    capture = SqlCapture()
    source = PostgresIngestCredentials(
        capture.sessions,
        verifier=verifier,
        resolver=DictSecretResolver(),
        now=lambda: _NOW,
        last_used_coarseness=dt.timedelta(hours=1),
    )
    row = _CredentialRow(
        slug=TENANT_A,
        status=ACTIVE_STATUS,
        hmac_secret_ref=SIGNING_SECRET_REF,
        key_hash=verifier.hash_secret(KEY_SECRET_A),
        revoked_at=None,
        expires_at=None,
    )
    capture.sessions.reset([CannedResult(row=row)])
    lookup = source.resolve(tenant_id=TENANT_A, api_key=api_key_for(KEY_ID_A, KEY_SECRET_A))

    assert isinstance(lookup, IngestCredential)
    touches = [
        sql_text(statement)
        for statement in capture.sessions.statements
        if sql_text(statement).startswith("update tenant_api_keys")
    ]
    assert len(touches) == 1, capture.sessions.statements
    assert "tenant_api_keys.key_id =" in touches[0]
    assert scoping_evidence(touches[0]) == frozenset({"predicate"}), touches[0]
    assert "tenants.slug =" in touches[0], (
        "the tenant is resolved from the slug the decision was made against, not from a "
        f"uuid recomputed here:\n{touches[0]}"
    )
    # It is the *second* statement: the decision is made on the read before it.
    assert len(capture.sessions.statements) == 2
    assert sql_text(capture.sessions.statements[0]).startswith("select")


def test_a_deleted_filter_is_visible_to_the_evidence_rules() -> None:
    """The negative control for the sweep's own mechanism, on synthetic SQL.

    :func:`scoping_evidence` is the thing every other assertion in this file rests on, so
    it gets tested rather than trusted. The hand-run mutation recorded in
    docs/tenant-isolation.md is the end-to-end version of this; this is the unit that makes
    it fail fast and locally.
    """
    filtered = "select leads.id from leads where leads.tenant_id = %(t)s and leads.id = %(l)s"
    unfiltered = "select leads.id from leads where leads.id = %(l)s"
    assert scoping_evidence(filtered) == frozenset({"predicate"})
    assert scoping_evidence(unfiltered) == frozenset()

    # A column named in the select list is not a filter.
    assert scoping_evidence("select leads.tenant_id from leads") == frozenset()

    # An insert is scoped by the column it writes the tenant into...
    assert "inserted column" in scoping_evidence(
        "insert into feedback (tenant_id, lead_id) values (%(t)s, %(l)s)"
    )
    # ... and an insert that has stopped writing one is not scoped at all.
    assert scoping_evidence("insert into feedback (lead_id) values (%(l)s)") == frozenset()

    # `tenants` is its own tenant, and is scoped by id or slug.
    assert scoping_evidence("select tenants.name from tenants where tenants.slug = %(s)s")
    assert scoping_evidence("update tenants set status=%(x)s where tenants.id = %(i)s::uuid")
    assert scoping_evidence("select tenants.name from tenants") == frozenset()


def test_every_repository_has_a_recipe_table() -> None:
    """A repository listed with no recipes at all is a repository nothing sweeps."""
    for repository in REPOSITORIES:
        assert RECIPES.get(repository.cls), f"{repository.name} has no recipes"


def test_the_tenant_identities_are_distinguishable() -> None:
    """The fixtures the whole package rests on share no substring.

    Every assertion in this suite is of the form "B's identifier does not appear in A's
    output". If the two slugs overlapped — ``acme`` inside ``acme-demo`` — those assertions
    would fail or pass for reasons that have nothing to do with tenancy.
    """
    assert TENANT_A not in TENANT_B and TENANT_B not in TENANT_A
    assert TENANT_A_UUID != TENANT_B_UUID
    assert isinstance(TENANT_A_UUID, uuid.UUID)
    assert not _A_IDENTITY & _B_IDENTITY


def test_statement_capture_records_every_statement_in_order() -> None:
    """The harness itself: a two-statement method yields two statements, not one.

    The mechanism is worth a test of its own, because a capture that silently returned only
    the first statement would make every multi-statement method half-swept while the sweep
    stayed green.
    """
    repository = next(item for item in REPOSITORIES if item.cls is PostgresLeadStore)
    statements: Sequence[ClauseElement] = _capture(
        repository, "record_assessment", recipe_for(PostgresLeadStore, "record_assessment")
    )
    assert len(statements) == 2
    assert sql_text(statements[0]).startswith("insert into assessments")
    assert sql_text(statements[1]).startswith("update leads")
