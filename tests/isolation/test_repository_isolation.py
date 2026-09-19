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

import ast
import datetime as dt
import importlib
import inspect
import pkgutil
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest
from sqlalchemy import (
    ClauseElement,
    Delete,
    Insert,
    Select,
    Update,
    insert,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, ColumnClause
from sqlalchemy.sql.selectable import Join

from leadquali import adapters, api
from leadquali.adapters.db_schema import Feedback, Lead, Tenant, TenantApiKey
from leadquali.adapters.keyhash_argon2 import Argon2KeyHasher
from leadquali.adapters.store_postgres import (
    FEEDBACK_IDEMPOTENCY_CONSTRAINT,
    PostgresLeadStore,
)
from leadquali.adapters.store_tenants import (
    PostgresIngestCredentials,
    PostgresTenantAdminStore,
)
from leadquali.api.signing import ACTIVE_STATUS, AuthFailure, CredentialRejected, IngestCredential
from tests.isolation.repositories import (
    ALLOWLIST,
    EXCLUDED_REPOSITORIES,
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
from tests.sqlcapture import PG_DIALECT, CannedResult, SqlCapture, parameters, sql_text

# --------------------------------------------------------------- what counts as scoping

#: Which column carries the tenant, per table. ``tenants`` is its own tenant: a statement
#: about one customer's row is scoped by ``tenants.id`` or ``tenants.slug``, and there is no
#: ``tenants.tenant_id`` to look for.
_TENANT_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    "tenants": frozenset({"id", "slug"}),
}
_DEFAULT_TENANT_COLUMN: Final[str] = "tenant_id"

#: How long an entry on the allowlist or the exclusion list has to justify itself. Long
#: enough that the word "constructor" alone does not clear it: an exemption nobody had to
#: explain is an exemption nobody reviewed, and a one-word reason is what a later reader
#: copies onto the method that should not have had one.
_MINIMUM_REASON_CHARS: Final[int] = 30

#: ``INSERT INTO <table> (<columns>)``. The one place this module still reads the rendered
#: SQL, and it is the one place where the text *is* the structure: the column list of an
#: insert is not a predicate and there is nothing about it a cleverer clause could fake.
_INSERT_TARGET: Final[re.Pattern[str]] = re.compile(r"insert\s+into\s+(\w+)\s*\(([^)]*)\)")


def _tenant_columns_of(table: str) -> frozenset[str]:
    """The columns that identify the tenant of a row in ``table``."""
    return _TENANT_COLUMNS.get(table, frozenset({_DEFAULT_TENANT_COLUMN}))


@dataclass(frozen=True, slots=True)
class Scope:
    """One filterable clause of a statement, and the tables it may be scoped by."""

    label: str
    whereclause: ClauseElement | None
    tables: frozenset[str]


def _leaf_tables(element: Any) -> set[str]:
    """The table names underneath a ``FROM`` entry, flattening joins and aliases."""
    if isinstance(element, Join):
        return _leaf_tables(element.left) | _leaf_tables(element.right)
    name = getattr(element, "name", None)
    return {str(name)} if name is not None else set()


def _scopes(statement: ClauseElement) -> list[Scope]:
    """Every clause of ``statement`` that could carry a tenant filter, and where it applies.

    An ``UPDATE`` or ``DELETE`` has one, over its own target table. A ``SELECT`` has one,
    over the leaf tables of its ``FROM`` — both, for the joins this codebase writes, because
    ``list_keys`` filters ``tenant_api_keys`` through ``tenants.slug``. An ``INSERT`` has
    none of its own, but may carry the ``WHERE`` of an ``ON CONFLICT DO UPDATE`` or, when it
    is an ``INSERT ... FROM SELECT``, the ``WHERE`` of the select that feeds it.
    """
    if isinstance(statement, Update | Delete):
        return [Scope("WHERE", statement.whereclause, frozenset(_leaf_tables(statement.table)))]
    if isinstance(statement, Select):
        tables = set[str]()
        for entry in statement.get_final_froms():
            tables |= _leaf_tables(entry)
        return [Scope("WHERE", statement.whereclause, frozenset(tables))]
    if isinstance(statement, Insert):
        scopes: list[Scope] = []
        source = statement.select
        if source is not None and isinstance(source, Select):
            scopes.extend(
                replace(scope, label="the WHERE of its source SELECT") for scope in _scopes(source)
            )
        conflict = getattr(statement, "_post_values_clause", None)
        conflict_where = getattr(conflict, "update_whereclause", None)
        if conflict_where is not None:
            scopes.append(
                Scope(
                    "ON CONFLICT ... WHERE",
                    conflict_where,
                    frozenset(_leaf_tables(statement.table)),
                )
            )
        return scopes
    return []


def _conjuncts(where: ClauseElement | None) -> list[ClauseElement]:
    """The top-level ``AND`` terms of a ``WHERE``, and deliberately not more.

    ``OR`` is never descended into, because a tenant predicate inside one is not a
    constraint: ``tenant_id = :x OR true`` mentions the column, binds the tenant, renders
    beautifully, and matches every row in the table.
    """
    if where is None:
        return []
    if isinstance(where, BooleanClauseList) and where.operator is operators.and_:
        return list(where.clauses)
    return [where]


def _is_tenant_column(element: Any, tables: frozenset[str]) -> bool:
    """Whether ``element`` is the tenant column of one of ``tables``."""
    if not isinstance(element, ColumnClause):
        return False
    table = getattr(element, "table", None)
    name = getattr(table, "name", None)
    if name is None or str(name) not in tables:
        return False
    return str(element.name) in _tenant_columns_of(str(name))


def _binds_a_value(element: Any) -> bool:
    """Whether ``element`` carries a bound parameter anywhere inside it.

    Compiling the fragment is the cheapest honest way to ask. It is what distinguishes a
    constraint from a tautology: ``tenant_id = tenant_id`` binds nothing, an unfiltered
    ``tenant_id IN (SELECT id FROM tenants)`` binds nothing, and the correlated subquery
    this codebase actually writes binds the slug it was given.
    """
    try:
        return bool(element.compile(dialect=PG_DIALECT).params)
    except Exception:  # not a compilable fragment; it cannot be a constraint either
        return False


def _constrains_a_tenant(conjunct: ClauseElement, tables: frozenset[str]) -> bool:
    """Whether one top-level term restricts these tables' rows to one tenant."""
    if not isinstance(conjunct, BinaryExpression):
        return False
    for near, far in ((conjunct.left, conjunct.right), (conjunct.right, conjunct.left)):
        if _is_tenant_column(near, tables) and _binds_a_value(far):
            return True
    return False


def scoping_failure(statement: ClauseElement) -> str | None:
    """Why this statement is not scoped to one tenant, or ``None`` if it is.

    Read off the SQLAlchemy construct rather than off the rendered SQL, because the text
    cannot answer the question that matters. An earlier version of this was a regex asking
    whether *a* tenant column was compared to *something*, anywhere in the statement, and a
    review found four shapes that satisfy it and constrain nothing — the worst being an
    uncorrelated ``EXISTS (SELECT id FROM tenants WHERE slug = :slug)``, which is true for
    any slug that exists and let tenant B expire tenant A's live API key with the whole
    suite green. All five are in
    :func:`test_a_deleted_filter_is_visible_to_the_evidence_rules`.

    The rule, stated once:

    * **every** filterable clause of the statement — its ``WHERE``, the ``WHERE`` of an
      ``ON CONFLICT DO UPDATE``, the ``WHERE`` of the select feeding an
      ``INSERT ... FROM SELECT`` — must have a **top-level ``AND`` term** that compares a
      **tenant column of one of that clause's own tables** to something that **binds a
      value**;
    * an ``INSERT`` must additionally write every tenant column of its target table, since
      an insert has no rows to filter and what makes it safe is that the tenant travels in
      the row for the composite foreign key to check.

    What it does not reach: a predicate inside a scalar subquery in a ``SELECT``'s column
    list. ``rollup_day`` has one, counting ``leads``, and
    ``test_metering_isolation.py::test_the_rollup_filters_both_source_tables_on_the_tenant``
    covers it by name.
    """
    scopes = _scopes(statement)
    if isinstance(statement, Insert):
        rendered = sql_text(statement)
        target = _INSERT_TARGET.search(rendered)
        if target is None:
            return "the INSERT names no columns"
        written = {column.strip() for column in target.group(2).split(",")}
        required = _tenant_columns_of(target.group(1))
        missing = sorted(required - written)
        if missing:
            return (
                f"the INSERT into {target.group(1)} does not write {', '.join(missing)}; "
                "an insert has no rows to filter, so the tenant has to travel in the row"
            )
    elif not scopes or all(scope.whereclause is None for scope in scopes):
        return "the statement has no WHERE clause at all"

    for scope in scopes:
        if scope.whereclause is None:
            return f"{scope.label} is empty"
        if not any(
            _constrains_a_tenant(conjunct, scope.tables)
            for conjunct in _conjuncts(scope.whereclause)
        ):
            wanted = sorted(
                f"{table}.{column}"
                for table in sorted(scope.tables)
                for column in sorted(_tenant_columns_of(table))
            )
            return (
                f"no top-level AND term of {scope.label} constrains a tenant column "
                f"({', '.join(wanted)}) to a bound value"
            )
    return None


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


def _adapter_classes_over_a_session_factory() -> dict[str, type]:
    """Every concrete adapter class constructed from a ``sessionmaker``.

    Discovered rather than listed, and discovered by *constructor signature* rather than by
    name: a class whose ``__init__`` takes a session factory is a class that reaches the
    database, whatever it is called. A ``Postgres*`` prefix rule would have missed a
    ``TenantQueryService`` and matched a ``PostgresUrlParser``.
    """
    found: dict[str, type] = {}
    for module_info in pkgutil.iter_modules(adapters.__path__):
        module = importlib.import_module(f"{adapters.__name__}.{module_info.name}")
        for name, member in vars(module).items():
            if name.startswith("_") or not inspect.isclass(member):
                continue
            if member.__module__ != module.__name__:
                continue  # re-exported from somewhere else; it is swept where it is defined
            try:
                parameters = inspect.signature(member.__init__).parameters
            except (TypeError, ValueError):
                continue
            annotations = [str(parameter.annotation) for parameter in parameters.values()]
            if any("sessionmaker" in annotation for annotation in annotations):
                found[name] = member
    return found


def test_every_adapter_over_a_session_factory_is_swept() -> None:
    """A whole repository class cannot go uncovered, which the rest of this file cannot say.

    Every other check here starts from :data:`REPOSITORIES`, a hand-written tuple — so the
    sweep catches a forgotten *method* and would never catch a forgotten *class*. That is
    not hypothetical: a review of this suite found five adapter classes on branches further
    up the stack that nothing here touches, the worst being #36's admin-console read surface
    over other tenants' leads and feedback.

    So the inventory is checked against the package rather than trusted. This passes on this
    branch and is **expected to fail** when #35's and #36's stores arrive, which is the
    mechanism working: those branches have to add the class and its recipes, or name it in
    ``EXCLUDED_REPOSITORIES`` with a reason, before they can go green.
    """
    swept = {repository.cls.__name__ for repository in REPOSITORIES}
    discovered = _adapter_classes_over_a_session_factory()

    assert swept <= set(discovered), (
        f"REPOSITORIES names {sorted(swept - set(discovered))}, which is not a class in "
        "leadquali.adapters that takes a session factory. Did it move or get renamed?"
    )

    unswept = sorted(set(discovered) - swept - set(EXCLUDED_REPOSITORIES))
    reaches = "reaches" if len(unswept) == 1 else "reach"
    assert not unswept, (
        f"{', '.join(unswept)} {reaches} the database, and nothing in the isolation suite "
        "touches them.\n"
        "Add each to REPOSITORIES in tests/isolation/repositories.py with an argument "
        "recipe per tenant-scoped method, or to EXCLUDED_REPOSITORIES with the reason it "
        "does not need one. A class that serves one tenant's rows and is not swept is the "
        "hole this file exists to close."
    )


def test_the_control_planes_enumeration_is_not_reachable_from_the_api() -> None:
    """``list_tenants`` returns every tenant's config and signing-secret reference.

    It is on the allowlist because an operator running ``tenantctl`` needs it and a tenant
    filter would make it meaningless. The exemption is only safe while nothing serving a
    request can call it — and ``api/main.py`` *does* construct a
    :class:`~leadquali.adapters.store_tenants.PostgresTenantAdminStore`, for the rate
    limiter's allowance lookup, so the class is genuinely in reach of the request path.

    docs/tenant-isolation.md opens by promising every claim in it has a test behind it, so
    this is that test: no module under ``leadquali.api`` names any method of the admin store
    except the one the rate limiter needs. Static, because the alternative is a runtime
    check that only fires on the code path that already went wrong.
    """
    surface = set(_public_methods(PostgresTenantAdminStore)) - {"from_url", "from_env"}
    permitted = {"rate_limit_for"}
    forbidden = surface - permitted
    assert "list_tenants" in forbidden, surface

    offenders: list[str] = []
    for path in sorted(Path(api.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                offenders.append(f"{path.name}:{node.lineno}: .{node.attr}()")
    assert not offenders, (
        "the request path reaches a control-plane method it has no business calling:\n"
        + "\n".join(offenders)
        + f"\nOnly {sorted(permitted)} is permitted there. list_tenants returns every "
        "tenant's icp_config and hmac_secret_ref, so anything that can reach it from an "
        "authenticated request is a cross-tenant read."
    )


def test_no_exclusion_outlives_the_class_it_excuses() -> None:
    """An exclusion for a class that no longer exists is a hole nobody can see."""
    discovered = _adapter_classes_over_a_session_factory()
    for name, reason in EXCLUDED_REPOSITORIES.items():
        assert name in discovered, f"{name} is excluded from the sweep and does not exist"
        assert len(reason) >= _MINIMUM_REASON_CHARS, f"{name}: give the exclusion a reason"


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
        assert len(reason) >= _MINIMUM_REASON_CHARS, (
            f"{repository.name}.{name}: the exemption's reason is {len(reason)} characters. "
            "Say what makes it safe, not which category it is in."
        )


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
        failure = scoping_failure(statement)
        assert failure is None, (
            f"{repository.name}.{method} statement {index + 1} of {len(statements)} is not "
            f"scoped to one tenant: {failure}.\n{sql}\n"
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
    This is asserted separately from the sweep because the two say different things. The
    sweep requires *every* scope of a statement to be tenant-constrained, which covers this
    clause; this test says the clause has to *exist*, so that deleting it altogether — and
    with it the scope the sweep was checking — is a failure rather than one fewer thing to
    check.
    """
    _, repository, method = case
    recipe = recipe_for(repository.cls, method)
    upserts = [
        statement
        for statement in _capture(repository, method, recipe)
        if "on conflict" in sql_text(statement)
    ]
    assert upserts, f"{repository.name}.{method} no longer emits an upsert"
    for statement in upserts:
        conflict = [scope for scope in _scopes(statement) if scope.label == "ON CONFLICT ... WHERE"]
        assert conflict, (
            f"{repository.name}.{method}: the ON CONFLICT clause has lost its tenant "
            f"predicate.\n{sql_text(statement)}"
        )
        assert scoping_failure(statement) is None, scoping_failure(statement)


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
        statement
        for statement in capture.sessions.statements
        if sql_text(statement).startswith("update tenant_api_keys")
    ]
    assert len(touches) == 1, capture.sessions.statements
    written = touches[0]
    assert scoping_failure(written) is None, scoping_failure(written)
    rendered = sql_text(written)
    assert "tenant_api_keys.key_id =" in rendered
    assert "tenants.slug =" in rendered, (
        "the tenant is resolved from the slug the decision was made against, not from a "
        f"uuid recomputed here:\n{rendered}"
    )
    # It is the *second* statement: the decision is made on the read before it.
    assert len(capture.sessions.statements) == 2
    assert sql_text(capture.sessions.statements[0]).startswith("select")


def test_a_deleted_filter_is_visible_to_the_scoping_rule() -> None:
    """The negative control for the sweep's own mechanism, on real constructs.

    :func:`scoping_failure` is what every other assertion in this file rests on, so it is
    tested rather than trusted. The hand-run mutations recorded in
    docs/tenant-isolation.md are the end-to-end version; this is the unit that fails fast
    and locally, and it is built from the real tables so a statement that passes here is a
    statement Postgres would accept.
    """
    tenant = uuid.uuid4()
    lead = uuid.uuid4()

    # The shape the adapters actually write.
    assert scoping_failure(select(Lead.id).where(Lead.tenant_id == tenant, Lead.id == lead)) is None
    # ... and the same statement with the filter deleted.
    assert scoping_failure(select(Lead.id).where(Lead.id == lead)) is not None
    # A column named in the select list is not a filter.
    assert scoping_failure(select(Lead.tenant_id)) is not None
    # `tenants` is its own tenant, scoped by either of its two identifying columns.
    assert scoping_failure(select(Tenant.name).where(Tenant.slug == "alpha")) is None
    assert scoping_failure(update(Tenant).where(Tenant.id == tenant).values(status="x")) is None
    assert scoping_failure(select(Tenant.name)) is not None


def test_the_scoping_rule_refuses_a_predicate_that_constrains_nothing() -> None:
    """The five shapes a review found that satisfy "a tenant column is compared to
    something" and restrict no rows at all.

    The rule used to be a regex over the rendered SQL, which cannot tell a constraint from
    a mention. The first of these is not hypothetical: swapping ``_update_key``'s
    correlated ``IN (SELECT ...)`` for an uncorrelated ``EXISTS (...)`` lets tenant B
    expire tenant A's live API key, renders SQL a reviewer would nod at, and left the whole
    suite green.
    """
    tenant = uuid.uuid4()
    key_id = "a1a1a1a1a1a1a1a1"
    owned = select(Tenant.id).where(Tenant.slug == "alpha")

    # 1. The exploit: an EXISTS that is true for any slug that exists, joined to nothing.
    exploit = (
        update(TenantApiKey)
        .where(TenantApiKey.key_id == key_id, owned.exists())
        .values(revoked_at=None)
    )
    assert scoping_failure(exploit) is not None, sql_text(exploit)

    # 2. The correlated form it replaced, which does constrain, and must still pass.
    correct = (
        update(TenantApiKey)
        .where(TenantApiKey.key_id == key_id, TenantApiKey.tenant_id.in_(owned))
        .values(revoked_at=None)
    )
    assert scoping_failure(correct) is None, sql_text(correct)

    # 3. A tautology alongside a real bind, which a "column compared to something" rule
    #    reads as a filter.
    vacuous = select(Lead.id).where(Lead.tenant_id == Lead.tenant_id, Lead.submission_id == "sub-1")
    assert scoping_failure(vacuous) is not None, sql_text(vacuous)

    # 4. `... OR true`, which mentions the tenant, binds it, and matches every row.
    always = select(Lead.id).where(or_(Lead.tenant_id == tenant, true()))
    assert scoping_failure(always) is not None, sql_text(always)

    # 5. An unfiltered subquery: the column is constrained to *every* tenant.
    everyone = select(Lead.id).where(Lead.tenant_id.in_(select(Tenant.id)))
    assert scoping_failure(everyone) is not None, sql_text(everyone)

    # 6. A CTE that mentions the tenant without the outer statement joining it.
    mine = select(Tenant.id).where(Tenant.slug == "alpha").cte("mine")
    detached = select(Lead.id).where(Lead.id.in_(select(mine.c.id)))
    assert scoping_failure(detached) is not None, sql_text(detached)


def test_an_insert_must_write_the_tenant_and_filter_on_it_where_it_can() -> None:
    """Both kinds of evidence, because either alone has a hole.

    An ``INSERT`` has no rows to filter, so what makes it safe is that the tenant travels
    in the row for the composite foreign key to check. But an upsert also carries a
    ``WHERE``, and a rule that accepted *either* would pass an upsert that had stopped
    writing its tenant column — the ``ON CONFLICT`` predicate alone would satisfy it, and
    the row would land under whatever tenant the database defaulted to.
    """
    tenant = uuid.uuid4()
    values = {"tenant_id": tenant, "lead_id": uuid.uuid4(), "rater": "r", "verdict": "good"}

    complete = pg_insert(Feedback).values(**values)
    assert (
        scoping_failure(
            complete.on_conflict_do_update(
                constraint=FEEDBACK_IDEMPOTENCY_CONSTRAINT,
                set_={"verdict": complete.excluded.verdict},
                where=Feedback.tenant_id == tenant,
            )
        )
        is None
    )

    # The tenant column is gone from the row; the ON CONFLICT predicate is untouched.
    without = pg_insert(Feedback).values(**{k: v for k, v in values.items() if k != "tenant_id"})
    assert (
        scoping_failure(
            without.on_conflict_do_update(
                constraint=FEEDBACK_IDEMPOTENCY_CONSTRAINT,
                set_={"verdict": without.excluded.verdict},
                where=Feedback.tenant_id == tenant,
            )
        )
        is not None
    )

    # The row is complete; the ON CONFLICT predicate has been deleted.
    assert (
        scoping_failure(
            complete.on_conflict_do_update(
                constraint=FEEDBACK_IDEMPOTENCY_CONSTRAINT,
                set_={"verdict": complete.excluded.verdict},
                where=true(),
            )
        )
        is not None
    )


def test_creating_a_tenant_must_write_both_of_its_identifying_columns() -> None:
    """``tenants`` is keyed two ways, and a row that has only one of them is unreachable.

    Every later call resolves a slug to a row id with ``tenant_uuid``, which is a UUID5 of
    the slug. An insert that let the database default ``id`` would create a tenant under a
    random UUID that no subsequent lookup by slug-derived id could ever find — and would
    look, from the outside, like a tenant whose leads had silently vanished.
    """
    good = insert(Tenant).values(
        id=uuid.uuid4(), slug="alpha", name="Alpha", icp_config={}, hmac_secret_ref=None
    )
    assert scoping_failure(good) is None

    without_id = insert(Tenant).values(
        slug="alpha", name="Alpha", icp_config={}, hmac_secret_ref=None
    )
    failure = scoping_failure(without_id)
    assert failure is not None and "id" in failure, failure


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
