"""Tenant administration: onboarding, config writes, and the life of an API key.

The acceptance criteria of #31 are what this file is organised around, and two of them get
their own section because they are the ones that would be easy to claim and hard to prove:

* *Onboarding tenant #2 takes minutes and touches no Python.* Asserted by driving the real
  ``tenants/acme-demo.json`` through the real service and then showing that the same
  identical assessment lands in a different tier, with a different action, for the two
  tenants — using ``TenantConfig``'s own lookups, not a mock of the difference.
* *Keys are unrecoverable from the database; a revoked key is rejected immediately.* The
  first half is asserted where the key is issued (nothing but the return value ever holds
  it); the second is asserted in ``test_api_signing.py``, at the door.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from leadquali.app.api_keys import KeyEnvironment, parse_api_key
from leadquali.app.tenant_ids import tenant_id_for
from leadquali.app.tenants import (
    DEFAULT_ROTATION_OVERLAP,
    ApiKeyRecord,
    IssuedApiKey,
    TenantAdminError,
    TenantAlreadyExistsError,
    TenantService,
    TenantStatus,
    UnknownApiKeyError,
    UnknownTenantError,
)
from leadquali.domain.models import (
    Action,
    DimensionScores,
    ExtractedFacts,
    LeadAssessment,
    Tier,
)
from leadquali.domain.routing import decide
from leadquali.domain.tenant_config import TenantConfig, TenantConfigError
from tests.fakes import (
    ExplodingTenantSecrets,
    FakeClock,
    FakeSecretHasher,
    FakeTenantSecrets,
    InMemoryTenantAdminStore,
)

TENANTS_DIR = Path(__file__).resolve().parents[2] / "tenants"
NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)


def tenant_file(slug: str) -> dict[str, Any]:
    """The shipped config for a tenant, exactly as an operator would paste it."""
    document: dict[str, Any] = json.loads((TENANTS_DIR / f"{slug}.json").read_text("utf-8"))
    return document


def a_config(slug: str = "acme", **overrides: Any) -> dict[str, Any]:
    """A minimal valid rubric for ``slug``."""
    config: dict[str, Any] = {
        "tenant_id": slug,
        "name": slug.title(),
        "icp_description": "Companies with inbound web-form volume.",
        "routing_rules": {
            "hot": {"action": "email_sales", "destination": "sales@example.invalid"},
            "warm": {"action": "email_sales", "destination": "sales@example.invalid"},
            "cold": {"action": "email_sales", "destination": "sales@example.invalid"},
            "disqualified": {"action": "suppress"},
        },
    }
    config.update(overrides)
    return config


class Harness:
    """A service wired to in-memory doubles, with its collaborators kept to hand."""

    def __init__(self) -> None:
        self.store = InMemoryTenantAdminStore()
        self.hasher = FakeSecretHasher()
        self.secrets = FakeTenantSecrets()
        self.clock = FakeClock(start=NOW, step_ms=0)
        self.service = TenantService(
            store=self.store, hasher=self.hasher, secrets=self.secrets, clock=self.clock
        )

    def onboard(self, slug: str = "acme", **overrides: Any) -> None:
        self.service.create_tenant(slug=slug, name=slug.title(), config=a_config(slug, **overrides))


@pytest.fixture
def harness() -> Harness:
    return Harness()


# ---------------------------------------------------------------------- onboarding


def test_creating_a_tenant_stores_the_config_verbatim(harness: Harness) -> None:
    record = harness.service.create_tenant(slug="acme", name="Acme", config=a_config("acme"))
    assert record.slug == "acme"
    assert record.name == "Acme"
    assert record.status is TenantStatus.ACTIVE
    assert record.config == a_config("acme")


def test_the_row_id_is_derived_from_the_slug(harness: Harness) -> None:
    """A database cannot compute a uuid5 in a CHECK, so the service is what enforces it —
    and it is the reason a fixture or a support query is portable between environments."""
    record = harness.service.create_tenant(slug="acme", name="Acme", config=a_config("acme"))
    assert record.id == tenant_id_for("acme")


def test_onboarding_provisions_a_signing_secret_and_records_only_its_arn(
    harness: Harness,
) -> None:
    record = harness.service.create_tenant(slug="acme", name="Acme", config=a_config("acme"))
    assert record.hmac_secret_ref == harness.secrets.created["acme"]
    assert harness.secrets.calls == 1


def test_a_duplicate_slug_is_refused(harness: Harness) -> None:
    harness.onboard()
    with pytest.raises(TenantAlreadyExistsError, match="acme"):
        harness.onboard()


def test_a_duplicate_slug_does_not_touch_the_secret_store_again(harness: Harness) -> None:
    """Refused before provisioning, so a mistyped onboarding cannot mint a second secret
    for a customer whose forms are already signing with the first."""
    harness.onboard()
    with pytest.raises(TenantAlreadyExistsError):
        harness.onboard()
    assert harness.secrets.calls == 1


def test_a_config_naming_a_different_tenant_is_refused(harness: Harness) -> None:
    """Two names for one tenant is how a config ends up applied to the wrong leads."""
    with pytest.raises(TenantAdminError, match="tenant_id"):
        harness.service.create_tenant(slug="acme", name="Acme", config=a_config("other"))
    assert harness.store.tenants == {}


@pytest.mark.parametrize("slug", ["", "Acme", "acme demo", "-acme", "a" * 64, "acme/../etc"])
def test_a_malformed_slug_is_refused(harness: Harness, slug: str) -> None:
    with pytest.raises((TenantAdminError, TenantConfigError)):
        harness.service.create_tenant(slug=slug, name="x", config=a_config(slug))
    assert harness.store.tenants == {}


def test_an_invalid_config_is_refused_before_anything_is_provisioned(
    harness: Harness,
) -> None:
    broken = a_config("acme", thresholds={"hot": 40.0, "warm": 55.0, "cold": 30.0})
    with pytest.raises(TenantConfigError):
        harness.service.create_tenant(slug="acme", name="Acme", config=broken)

    assert harness.store.tenants == {}
    assert harness.secrets.calls == 0


def test_a_failure_to_provision_leaves_no_half_made_tenant() -> None:
    """A tenant row with no signing secret cannot authenticate anything, so it must not
    exist: the insert happens after the secret, and nothing catches the failure."""
    harness = Harness()
    service = TenantService(
        store=harness.store,
        hasher=harness.hasher,
        secrets=ExplodingTenantSecrets(),
        clock=harness.clock,
    )
    with pytest.raises(RuntimeError, match="secrets manager"):
        service.create_tenant(slug="acme", name="Acme", config=a_config("acme"))
    assert harness.store.tenants == {}


def test_an_unknown_tenant_is_an_error_not_a_none(harness: Harness) -> None:
    with pytest.raises(UnknownTenantError, match="nobody"):
        harness.service.get_tenant(slug="nobody")


def test_tenants_list_oldest_first(harness: Harness) -> None:
    harness.onboard("acme")
    harness.onboard("beta")
    assert [record.slug for record in harness.service.list_tenants()] == ["acme", "beta"]


# ------------------------------------------------------------------ config updates


def test_a_config_update_replaces_the_stored_document(harness: Harness) -> None:
    harness.onboard()
    updated = harness.service.update_config(
        slug="acme", config=a_config("acme", min_confidence=0.9)
    )
    assert updated.config["min_confidence"] == 0.9
    assert harness.store.tenants["acme"].config["min_confidence"] == 0.9


@pytest.mark.parametrize(
    ("description", "broken"),
    [
        ("a gap between the tier bands", {"thresholds": {"hot": 40.0, "warm": 55.0, "cold": 30.0}}),
        ("a weight for a dimension nothing scores", {"weights": {"vibes": 1.0}}),
        ("a tier with no routing rule", {"routing_rules": {"hot": {"action": "suppress"}}}),
        (
            "a delivering action with nowhere to deliver",
            {
                "routing_rules": {
                    "hot": {"action": "email_sales"},
                    "warm": {"action": "email_sales", "destination": "s@example.invalid"},
                    "cold": {"action": "email_sales", "destination": "s@example.invalid"},
                    "disqualified": {"action": "suppress"},
                }
            },
        ),
        ("a confidence gate that is not a probability", {"min_confidence": 1.5}),
        ("a confidence gate pasted as a boolean", {"min_confidence": True}),
    ],
)
def test_a_bad_paste_cannot_take_a_tenant_down(
    harness: Harness, description: str, broken: dict[str, Any]
) -> None:
    """The acceptance criterion, one failure mode per case.

    Every one of these is a config that *parses* — it is a JSON object with the right keys
    — and that would mis-route or strand this tenant's leads if it were stored. Validation
    runs before the store is touched, so the tenant keeps running on the config it had.
    """
    harness.onboard()
    good = dict(harness.store.tenants["acme"].config)

    with pytest.raises(TenantConfigError):
        harness.service.update_config(slug="acme", config=a_config("acme", **broken))

    assert harness.store.tenants["acme"].config == good, description


def test_a_config_update_naming_another_tenant_is_refused(harness: Harness) -> None:
    harness.onboard("acme")
    harness.onboard("beta")
    with pytest.raises(TenantAdminError, match="tenant_id"):
        harness.service.update_config(slug="acme", config=a_config("beta"))
    assert harness.store.tenants["acme"].config["tenant_id"] == "acme"


def test_updating_an_unknown_tenant_is_an_error(harness: Harness) -> None:
    with pytest.raises(UnknownTenantError):
        harness.service.update_config(slug="nobody", config=a_config("nobody"))


# --------------------------------------------------------------------- suspension


@pytest.mark.parametrize("status", [TenantStatus.SUSPENDED, TenantStatus.DISABLED])
def test_a_tenant_can_be_suspended_and_resumed(harness: Harness, status: TenantStatus) -> None:
    harness.onboard()
    assert harness.service.set_status(slug="acme", status=status).status is status
    assert harness.service.set_status(slug="acme", status=TenantStatus.ACTIVE).status is (
        TenantStatus.ACTIVE
    )


def test_suspending_one_tenant_leaves_the_other_alone(harness: Harness) -> None:
    """The acceptance criterion, at the control-plane end. The ingest end of it is in
    ``test_api_ingest.py``."""
    harness.onboard("acme")
    harness.onboard("beta")
    harness.service.set_status(slug="acme", status=TenantStatus.SUSPENDED)
    assert harness.service.get_tenant(slug="beta").status is TenantStatus.ACTIVE


def test_suspension_does_not_disturb_the_stored_config(harness: Harness) -> None:
    harness.onboard()
    before = dict(harness.store.tenants["acme"].config)
    harness.service.set_status(slug="acme", status=TenantStatus.SUSPENDED)
    assert harness.store.tenants["acme"].config == before


# --------------------------------------------------------------------------- keys


def test_an_issued_key_parses_and_matches_its_record(harness: Harness) -> None:
    harness.onboard()
    issued = harness.service.issue_key(slug="acme", label="acme website")

    parsed = parse_api_key(issued.key)
    assert parsed is not None
    assert parsed.key_id == issued.record.key_id
    assert parsed.environment is KeyEnvironment.LIVE
    assert issued.record.key_prefix == parsed.prefix
    assert issued.record.label == "acme website"


def test_only_the_hash_of_the_secret_reaches_the_store(harness: Harness) -> None:
    """ "Keys are unrecoverable from the database", asserted at the one boundary where the
    key still exists: what the store was handed contains the secret's hash and never the
    key, the key_id part, or anything an attacker could present."""
    harness.onboard()
    issued = harness.service.issue_key(slug="acme")
    parsed = parse_api_key(issued.key)
    assert parsed is not None

    stored = harness.store.hashes[issued.record.key_id]
    assert issued.key not in stored
    assert stored == f"$argon2id$fake${parsed.secret}"
    assert harness.hasher.calls == 1


def test_a_key_record_carries_no_hash_and_no_secret(harness: Harness) -> None:
    harness.onboard()
    issued = harness.service.issue_key(slug="acme")
    fields = set(ApiKeyRecord.__dataclass_fields__)
    assert "key_hash" not in fields
    assert "secret" not in fields
    assert issued.key not in repr(issued.record)


def test_the_issued_key_never_renders_itself(harness: Harness) -> None:
    """A repr ends up in a traceback and a traceback ends up in a log."""
    harness.onboard()
    issued = harness.service.issue_key(slug="acme")
    rendered = repr(issued)
    assert issued.key not in rendered
    assert "<redacted>" in rendered
    assert issued.record.key_id in rendered


def test_two_issued_keys_differ(harness: Harness) -> None:
    harness.onboard()
    first = harness.service.issue_key(slug="acme")
    second = harness.service.issue_key(slug="acme")
    assert first.key != second.key
    assert first.record.key_id != second.record.key_id
    assert len(harness.service.list_keys(slug="acme")) == 2


def test_a_test_key_is_labelled_as_one_in_the_key_itself(harness: Harness) -> None:
    harness.onboard()
    issued = harness.service.issue_key(slug="acme", environment=KeyEnvironment.TEST)
    assert issued.key.startswith("lq_test_")
    assert issued.record.key_prefix.startswith("lq_test_")


def test_issuing_a_key_to_an_unknown_tenant_is_an_error(harness: Harness) -> None:
    with pytest.raises(UnknownTenantError):
        harness.service.issue_key(slug="nobody")


def test_a_key_listing_is_scoped_to_its_tenant(harness: Harness) -> None:
    """Invariant 4 at the control plane: one customer's listing cannot show another's."""
    harness.onboard("acme")
    harness.onboard("beta")
    mine = harness.service.issue_key(slug="acme")
    theirs = harness.service.issue_key(slug="beta")

    assert [record.key_id for record in harness.service.list_keys(slug="acme")] == [
        mine.record.key_id
    ]
    assert [record.key_id for record in harness.service.list_keys(slug="beta")] == [
        theirs.record.key_id
    ]


# ------------------------------------------------------------------------ rotation


def test_rotation_issues_a_new_key_and_gives_the_old_one_a_deadline(harness: Harness) -> None:
    harness.onboard()
    old = harness.service.issue_key(slug="acme", label="acme website")

    new = harness.service.rotate_key(slug="acme", key_id=old.record.key_id)

    assert new.record.key_id != old.record.key_id
    assert new.key != old.key
    keys = {record.key_id: record for record in harness.service.list_keys(slug="acme")}
    assert keys[old.record.key_id].expires_at == NOW + DEFAULT_ROTATION_OVERLAP
    assert keys[new.record.key_id].expires_at is None


def test_both_keys_are_live_during_the_overlap(harness: Harness) -> None:
    """The property the overlap exists for: the customer redeploys their form when they
    like, and nothing is refused in between."""
    harness.onboard()
    old = harness.service.issue_key(slug="acme")
    new = harness.service.rotate_key(slug="acme", key_id=old.record.key_id)

    keys = {record.key_id: record for record in harness.service.list_keys(slug="acme")}
    inside = NOW + DEFAULT_ROTATION_OVERLAP - timedelta(hours=1)
    assert keys[old.record.key_id].is_live(inside)
    assert keys[new.record.key_id].is_live(inside)

    after = NOW + DEFAULT_ROTATION_OVERLAP + timedelta(seconds=1)
    assert not keys[old.record.key_id].is_live(after)
    assert keys[new.record.key_id].is_live(after)


def test_the_default_overlap_is_a_week() -> None:
    """The number in the runbook and the number in the code have to be the same number."""
    assert DEFAULT_ROTATION_OVERLAP.days == 7
    assert DEFAULT_ROTATION_OVERLAP.seconds == 0


def test_a_custom_overlap_is_honoured(harness: Harness) -> None:
    harness.onboard()
    old = harness.service.issue_key(slug="acme")
    harness.service.rotate_key(slug="acme", key_id=old.record.key_id, overlap=timedelta(hours=1))
    keys = {record.key_id: record for record in harness.service.list_keys(slug="acme")}
    assert keys[old.record.key_id].expires_at == NOW + timedelta(hours=1)


def test_a_zero_overlap_retires_the_old_key_at_once(harness: Harness) -> None:
    """What an operator reaches for when a key has leaked: rotate with no grace at all."""
    harness.onboard()
    old = harness.service.issue_key(slug="acme")
    harness.service.rotate_key(slug="acme", key_id=old.record.key_id, overlap=timedelta(0))
    keys = {record.key_id: record for record in harness.service.list_keys(slug="acme")}
    assert not keys[old.record.key_id].is_live(NOW)


def test_a_negative_overlap_is_refused(harness: Harness) -> None:
    harness.onboard()
    old = harness.service.issue_key(slug="acme")
    with pytest.raises(TenantAdminError, match="overlap"):
        harness.service.rotate_key(
            slug="acme", key_id=old.record.key_id, overlap=timedelta(days=-1)
        )


def test_a_rotated_key_inherits_its_predecessors_label_and_environment(
    harness: Harness,
) -> None:
    """Rotating a test key must not hand the customer a live one: the environment literal
    exists to make that mistake visible, and it must not be the thing that causes it."""
    harness.onboard()
    old = harness.service.issue_key(
        slug="acme", label="acme website", environment=KeyEnvironment.TEST
    )
    new = harness.service.rotate_key(slug="acme", key_id=old.record.key_id)
    assert new.record.label == "acme website"
    assert new.key.startswith("lq_test_")


def test_rotating_a_key_that_is_not_this_tenants_is_refused(harness: Harness) -> None:
    harness.onboard("acme")
    harness.onboard("beta")
    theirs = harness.service.issue_key(slug="beta")
    with pytest.raises(UnknownApiKeyError):
        harness.service.rotate_key(slug="acme", key_id=theirs.record.key_id)


# ----------------------------------------------------------------------- revocation


def test_revoking_a_key_stamps_it(harness: Harness) -> None:
    harness.onboard()
    issued = harness.service.issue_key(slug="acme")
    revoked = harness.service.revoke_key(slug="acme", key_id=issued.record.key_id)
    assert revoked.revoked_at == NOW
    assert not revoked.is_live(NOW)


def test_a_revoked_key_stays_in_the_listing(harness: Harness) -> None:
    """ "Which key did we revoke, and when?" is a question an incident asks, and a row that
    disappeared cannot answer it."""
    harness.onboard()
    issued = harness.service.issue_key(slug="acme")
    harness.service.revoke_key(slug="acme", key_id=issued.record.key_id)
    listed = harness.service.list_keys(slug="acme")
    assert [record.key_id for record in listed] == [issued.record.key_id]
    assert listed[0].revoked_at is not None


def test_revoking_twice_keeps_the_first_timestamp(harness: Harness) -> None:
    """The first revocation is when the key stopped working, which is the only thing this
    column is asked for afterwards."""
    harness.onboard()
    issued = harness.service.issue_key(slug="acme")
    first = harness.service.revoke_key(slug="acme", key_id=issued.record.key_id)
    harness.clock.start = NOW + timedelta(days=1)
    second = harness.service.revoke_key(slug="acme", key_id=issued.record.key_id)
    assert second.revoked_at == first.revoked_at


def test_one_tenant_cannot_revoke_anothers_key(harness: Harness) -> None:
    """Invariant 4, stated as the thing it prevents."""
    harness.onboard("acme")
    harness.onboard("beta")
    theirs = harness.service.issue_key(slug="beta")

    with pytest.raises(UnknownApiKeyError):
        harness.service.revoke_key(slug="acme", key_id=theirs.record.key_id)

    assert harness.service.list_keys(slug="beta")[0].revoked_at is None


def test_revoking_an_unknown_key_is_an_error(harness: Harness) -> None:
    harness.onboard()
    with pytest.raises(UnknownApiKeyError):
        harness.service.revoke_key(slug="acme", key_id="0" * 16)


# ============================================================================
# The acceptance criterion that matters most: a second tenant is a config write
# ============================================================================


def an_assessment() -> LeadAssessment:
    """One lead's judgment. Deliberately middling, and deliberately shaped like the lead
    the two tenants disagree about: strong on authority and budget, weak on intent and
    urgency. A lead that is obviously hot or obviously junk would land in the same tier
    under any sane rubric and would prove nothing at all."""
    return LeadAssessment(
        dimension_scores=DimensionScores(
            icp_fit=18, intent=6, authority=12, urgency=3, budget_signal=11
        ),
        extracted=ExtractedFacts(
            company_name="Rheintal Fertigung",
            industry="manufacturing",
            company_size_estimate="450 employees",
            role_seniority="head of operations",
            stated_use_case="line-side downtime reporting",
            stated_timeline=None,
        ),
        reasoning="Mid-market manufacturer with a named problem and no stated timeline.",
        confidence=0.82,
        missing_information=["budget"],
        suggested_first_question="Which line would the pilot run on?",
        spam_or_test_submission=False,
    )


def test_the_two_shipped_tenants_have_genuinely_different_policies() -> None:
    """If the two configs were near-identical, the test below would prove nothing."""
    default = TenantConfig.from_dict(tenant_file("default"))
    acme = TenantConfig.from_dict(tenant_file("acme-demo"))

    assert default.icp_description != acme.icp_description
    assert default.weights != acme.weights
    assert set(acme.weights.values()) != {1.0}, "acme must not be on the neutral weights"
    assert default.thresholds != acme.thresholds
    assert default.min_confidence != acme.min_confidence
    assert default.action_for(Tier.WARM) is not acme.action_for(Tier.WARM)


def test_one_identical_lead_is_tiered_and_routed_differently_by_the_two_tenants() -> None:
    """The acceptance criterion: *two tenants with different thresholds produce different
    tiers for the identical lead payload* — and, because routing is configuration too, a
    different action as well.

    The real ``TenantConfig.tier_for`` and ``action_for`` do the work. Nothing here mocks
    the difference: the only thing that differs between the two calls is which JSON file
    was loaded, which is precisely the claim being made.
    """
    assessment = an_assessment()
    default = TenantConfig.from_dict(tenant_file("default"))
    acme = TenantConfig.from_dict(tenant_file("acme-demo"))

    default_decision = decide(assessment, default)
    acme_decision = decide(assessment, acme)

    assert default_decision.tier is not acme_decision.tier
    assert default_decision.action is not acme_decision.action

    # Named, so a future rubric change that flattens the difference fails loudly rather
    # than leaving a test that passes by accident. The default tenant weights every
    # dimension at 1.0 and scores this lead 50/100 — below its warm band, so cold, and
    # cold goes to the sales inbox. Acme weights authority at 2.0 and budget at 1.75,
    # which lifts the same lead to 59.8 — inside its much lower warm band — and acme sends
    # warm leads to a human rather than to an inbox.
    assert (default_decision.tier, default_decision.action) == (Tier.COLD, Action.EMAIL_SALES)
    assert (acme_decision.tier, acme_decision.action) == (Tier.WARM, Action.ESCALATE_HUMAN)
    assert default_decision.total_score == 50.0
    assert acme_decision.total_score == 59.8
    assert acme.destination_for(Tier.WARM) == "inside-sales-triage@acme-demo.invalid"


def test_onboarding_the_second_tenant_is_a_config_write(harness: Harness) -> None:
    """Driven through the real service with the real file. Nothing about ``acme-demo`` is
    special-cased anywhere: the same two calls onboard it as onboard the default tenant."""
    for slug in ("default", "acme-demo"):
        document = tenant_file(slug)
        record = harness.service.create_tenant(
            slug=slug, name=str(document["name"]), config=document
        )
        assert record.config == document
        assert record.id == tenant_id_for(slug)

    assert [record.slug for record in harness.service.list_tenants()] == [
        "acme-demo",
        "default",
    ] or [record.slug for record in harness.service.list_tenants()] == ["default", "acme-demo"]


def test_no_code_path_branches_on_a_customers_slug() -> None:
    """The structural half of "onboarding touches no Python".

    A conditional on a customer's name is how "config, not code" rots: the first special
    case is always small and always reasonable. What this looks for is a *string literal*
    equal to a shipped customer slug anywhere under ``src`` — the shape any such branch
    would have to take, and one that prose in a docstring cannot trigger by accident.

    ``default`` is excluded: it is a genuine default in several places (the CLI's
    ``--tenant``, the seed script's fallback slug) rather than a branch on a customer.
    """
    customers = {path.stem for path in TENANTS_DIR.glob("*.json")} - {"default"}
    assert customers, "expected a second tenant to ship; #31's whole point is that it can"

    src = Path(__file__).resolve().parents[2] / "src" / "leadquali"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        if literals & customers:
            offenders.append(path.relative_to(src).as_posix())
    assert offenders == [], f"{offenders} name a specific customer"


def test_the_service_never_branches_on_which_tenant_it_is_given(harness: Harness) -> None:
    """The behavioural half: the same sequence of collaborator calls for either tenant."""
    calls: list[str] = []

    class RecordingSecrets:
        def create_tenant_hmac_secret(self, slug: str) -> str:
            calls.append("provision")
            return f"arn:{slug}"

    service = TenantService(
        store=harness.store,
        hasher=harness.hasher,
        secrets=RecordingSecrets(),
        clock=harness.clock,
    )
    for slug in ("default", "acme-demo"):
        before = len(calls)
        service.create_tenant(slug=slug, name=slug, config=tenant_file(slug))
        service.issue_key(slug=slug, label="website")
        assert calls[before:] == ["provision"]


def test_an_issued_key_is_the_only_thing_that_differs_between_two_onboardings(
    harness: Harness,
) -> None:
    """Onboarding produces the same shape of result for both tenants: a record, an ARN and
    exactly one key. Whatever a customer's rubric says, the credentials story is identical."""
    issued: list[IssuedApiKey] = []
    for slug in ("default", "acme-demo"):
        harness.service.create_tenant(slug=slug, name=slug, config=tenant_file(slug))
        issued.append(harness.service.issue_key(slug=slug, label="website"))

    assert len({key.record.key_id for key in issued}) == 2
    for slug, key in zip(("default", "acme-demo"), issued, strict=True):
        assert harness.service.get_tenant(slug=slug).hmac_secret_ref is not None
        assert [record.key_id for record in harness.service.list_keys(slug=slug)] == [
            key.record.key_id
        ]
