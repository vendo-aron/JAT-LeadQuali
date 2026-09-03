"""The SES notifier: what goes on the wire, and what happens when Amazon says no.

Two kinds of double, on purpose. ``moto`` stands in for SES itself where the question is
"does this request work against the real API" — an unverified identity, a real ``MessageId``
coming back — because that is the part a hand-written stub would happily get wrong forever.
A recording stub is used where the question is "what did we put in the request", because
reading the message back out of moto's internals would couple these tests to moto's
private model.

There are no AWS credentials in this environment, and there is no test here that needs
any: everything is offline.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from leadquali.adapters.notify_ses import (
    OFFERED_VERDICTS,
    SesDispatchError,
    SesIdentity,
    SesNotifier,
    SesThrottledError,
)
from leadquali.app.feedback import (
    MIN_TOKEN_SECRET_CHARS,
    TokenAccepted,
    rater_id,
    verify_token,
)
from leadquali.domain.models import (
    Action,
    DimensionScores,
    EscalationReason,
    ExtractedFacts,
    LeadAssessment,
    RoutingDecision,
    Tier,
)
from leadquali.domain.routing import system_failure
from leadquali.prompts.lead import LeadSubmission
from tests.fakes import FakeClock

REGION = "eu-west-1"
SENDER = "LeadQuali <leads@mail.example.com>"
SENDER_ADDRESS = "leads@mail.example.com"
DESTINATION = "sales@northwind.example"
CONFIGURATION_SET = "leadquali-prod"
SECRET = b"s" * MIN_TOKEN_SECRET_CHARS
BASE_URL = "https://api.example.com/prod"
TENANT = "default"
LEAD = "8f14e45f-ceea-467a-9575-1b1e0a4d3c11"

SUBMISSION = LeadSubmission(
    full_name="Dana Whitfield",
    email="dana@northwind.example",
    company="Northwind Logistics",
    role="VP Operations",
    message="Replacing our routing spreadsheet before the Q4 peak.",
)
ASSESSMENT = LeadAssessment(
    dimension_scores=DimensionScores(
        icp_fit=27, intent=22, authority=12, urgency=13, budget_signal=11
    ),
    extracted=ExtractedFacts(
        company_name="Northwind Logistics",
        industry="freight",
        company_size_estimate="200-500",
        role_seniority="vp",
        stated_use_case="replace a routing spreadsheet",
        stated_timeline="before Q4",
    ),
    reasoning="States a deadline and owns the budget line.",
    confidence=0.86,
    missing_information=["current tooling spend"],
    suggested_first_question="What breaks first when the spreadsheet is wrong?",
    spam_or_test_submission=False,
)
HOT = RoutingDecision(
    tier=Tier.HOT, action=Action.EMAIL_SALES, total_score=87.0, note="scored 87.00/100 — hot"
)


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials that only moto will ever see, and a region that is not anyone's default."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": REGION,
    }.items():
        monkeypatch.setenv(name, value)


class RecordingSes:
    """Captures the ``SendEmail`` request, or raises whatever it was told to raise."""

    def __init__(self, *, raises: Exception | None = None, response: Any = None) -> None:
        self.raises = raises
        self.response = response if response is not None else {"MessageId": "ses-message-1"}
        self.requests: list[dict[str, Any]] = []

    def send_email(self, **request: Any) -> Any:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.response

    @property
    def request(self) -> dict[str, Any]:
        assert len(self.requests) == 1, f"expected exactly one send, got {len(self.requests)}"
        return self.requests[0]


def client_error(code: str) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": code,
                # Deliberately quoting the recipient, exactly as SES does. Nothing from
                # this string may reach the exception we raise (invariant 5).
                "Message": f"Maximum sending rate exceeded for {DESTINATION}",
            }
        },
        "SendEmail",
    )


def notifier(client: Any, **overrides: Any) -> SesNotifier:
    settings: dict[str, Any] = {
        "identity": SesIdentity(sender=SENDER, configuration_set=CONFIGURATION_SET),
        "feedback_base_url": BASE_URL,
        "token_secret": SECRET,
        "clock": FakeClock(start=datetime(2026, 9, 3, 9, 0, tzinfo=UTC), step_ms=0),
    }
    settings.update(overrides)
    return SesNotifier(client=client, **settings)


def dispatch(client: Any, **overrides: Any) -> str | None:
    call: dict[str, Any] = {
        "tenant_id": TENANT,
        "lead_id": LEAD,
        "destination": DESTINATION,
        "submission": SUBMISSION,
        "decision": HOT,
        "assessment": ASSESSMENT,
    }
    call.update(overrides)
    return notifier(client).dispatch(**call)


# --------------------------------------------------------------------- against real SES


@mock_aws
def test_a_hot_lead_is_accepted_by_ses_and_returns_a_message_id() -> None:
    client = boto3.client("sesv2", region_name=REGION)
    client.create_email_identity(EmailIdentity=SENDER_ADDRESS)

    message_id = dispatch(client)

    assert message_id, "the provider message id is what routing_events traces a bounce with"
    assert isinstance(message_id, str)


@mock_aws
def test_an_unverified_sender_is_a_dispatch_error_not_a_silent_success() -> None:
    """Nothing was created, so SES has no verified identity to send from."""
    client = boto3.client("sesv2", region_name=REGION)
    with pytest.raises(SesDispatchError):
        dispatch(client)


@mock_aws
def test_the_system_failure_email_also_reaches_ses() -> None:
    """``assessment=None`` must send, not raise: an unassessed lead still needs a human."""
    client = boto3.client("sesv2", region_name=REGION)
    client.create_email_identity(EmailIdentity=SENDER_ADDRESS)

    message_id = dispatch(
        client, assessment=None, decision=system_failure(EscalationReason.API_ERROR)
    )
    assert message_id


# ------------------------------------------------------------------- what we send


def test_the_request_carries_both_parts_the_sender_and_the_configuration_set() -> None:
    client = RecordingSes()
    dispatch(client)
    request = client.request

    assert request["FromEmailAddress"] == SENDER
    assert request["Destination"]["ToAddresses"] == [DESTINATION]
    assert request["ConfigurationSetName"] == CONFIGURATION_SET
    body = request["Content"]["Simple"]["Body"]
    assert body["Text"]["Data"].strip()
    assert body["Html"]["Data"].startswith("<!DOCTYPE html")
    assert body["Text"]["Charset"] == "UTF-8"
    assert body["Html"]["Charset"] == "UTF-8"
    assert "HOT" in request["Content"]["Simple"]["Subject"]["Data"]


def test_the_reply_goes_to_the_lead() -> None:
    client = RecordingSes()
    dispatch(client)
    assert client.request["ReplyToAddresses"] == ["dana@northwind.example"]


def test_a_lead_with_no_address_simply_has_no_reply_to() -> None:
    client = RecordingSes()
    dispatch(client, submission=LeadSubmission(full_name="Anonymous"))
    assert "ReplyToAddresses" not in client.request


def test_no_configuration_set_is_configured_means_none_is_sent() -> None:
    client = RecordingSes()
    notifier(client, identity=SesIdentity(sender=SENDER)).dispatch(
        tenant_id=TENANT,
        lead_id=LEAD,
        destination=DESTINATION,
        submission=SUBMISSION,
        decision=HOT,
        assessment=ASSESSMENT,
    )
    assert "ConfigurationSetName" not in client.request


def test_the_message_is_tagged_for_the_event_destination() -> None:
    client = RecordingSes()
    dispatch(client)
    tags = {tag["Name"]: tag["Value"] for tag in client.request["EmailTags"]}
    assert tags == {"tenant": "default", "tier": "hot", "escalated": "no"}


def test_a_tenant_id_that_ses_would_reject_as_a_tag_is_sanitised() -> None:
    """A tag SES refuses rejects the whole send; a metric label must not cost a lead."""
    client = RecordingSes()
    dispatch(client, tenant_id="acme.co uk")
    tags = {tag["Name"]: tag["Value"] for tag in client.request["EmailTags"]}
    assert tags["tenant"] == "acme_co_uk"


# ------------------------------------------------------------------- the feedback links


def test_every_email_carries_one_signed_link_per_offered_verdict() -> None:
    client = RecordingSes()
    dispatch(client)
    text = client.request["Content"]["Simple"]["Body"]["Text"]["Data"]

    tokens = [
        line.rsplit("/feedback/", 1)[1].strip()
        for line in text.splitlines()
        if "/feedback/" in line
    ]
    assert len(tokens) == len(OFFERED_VERDICTS)

    verdicts = set()
    for token in tokens:
        result = verify_token(
            secret=SECRET, token=token, now=datetime(2026, 9, 3, 9, 1, tzinfo=UTC)
        )
        assert isinstance(result, TokenAccepted), "the link we send must be one we accept"
        assert result.claim.lead_id == LEAD
        assert result.claim.tenant_id == TENANT
        assert result.claim.rater == rater_id(DESTINATION)
        verdicts.add(result.claim.verdict)
    assert verdicts == set(OFFERED_VERDICTS)


def test_the_links_expire_on_the_configured_horizon() -> None:
    client = RecordingSes()
    start = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    notifier(client, clock=FakeClock(start=start, step_ms=0), token_ttl_days=7).dispatch(
        tenant_id=TENANT,
        lead_id=LEAD,
        destination=DESTINATION,
        submission=SUBMISSION,
        decision=HOT,
        assessment=ASSESSMENT,
    )
    text = client.request["Content"]["Simple"]["Body"]["Text"]["Data"]
    token = next(line for line in text.splitlines() if "/feedback/" in line).rsplit("/", 1)[1]
    result = verify_token(secret=SECRET, token=token, now=start)
    assert isinstance(result, TokenAccepted)
    assert result.claim.expires_at == start + timedelta(days=7)


def test_the_system_failure_email_still_carries_feedback_links() -> None:
    """An unassessed lead is exactly the one whose verdict is worth the most."""
    client = RecordingSes()
    dispatch(client, assessment=None, decision=system_failure(EscalationReason.TIMEOUT))
    assert "/feedback/" in client.request["Content"]["Simple"]["Body"]["Text"]["Data"]


def test_two_destinations_get_different_rater_ids() -> None:
    first, second = RecordingSes(), RecordingSes()
    dispatch(first)
    dispatch(second, destination="escalations@example.com")

    def rater_of(client: RecordingSes) -> str:
        text = client.request["Content"]["Simple"]["Body"]["Text"]["Data"]
        token = next(line for line in text.splitlines() if "/feedback/" in line).rsplit("/", 1)[1]
        result = verify_token(secret=SECRET, token=token, now=datetime(2026, 9, 3, 9, tzinfo=UTC))
        assert isinstance(result, TokenAccepted)
        return result.claim.rater

    assert rater_of(first) != rater_of(second)


# ---------------------------------------------------------------------------- failure


@pytest.mark.parametrize(
    "code",
    ["Throttling", "ThrottlingException", "TooManyRequestsException", "SendingPausedException"],
)
def test_throttling_raises_its_own_type_so_the_queue_redelivers(code: str) -> None:
    with pytest.raises(SesThrottledError):
        dispatch(RecordingSes(raises=client_error(code)))


def test_a_rejected_message_raises_a_dispatch_error() -> None:
    with pytest.raises(SesDispatchError) as raised:
        dispatch(RecordingSes(raises=client_error("MessageRejected")))
    assert not isinstance(raised.value, SesThrottledError)


def test_a_network_failure_raises_rather_than_returning_none() -> None:
    """Returning ``None`` would let the pipeline record a lead as delivered to nobody."""
    with pytest.raises(SesDispatchError):
        dispatch(RecordingSes(raises=EndpointConnectionError(endpoint_url="https://ses")))


def test_a_response_without_a_message_id_is_treated_as_a_failure() -> None:
    with pytest.raises(SesDispatchError, match="MessageId"):
        dispatch(RecordingSes(response={}))


def test_a_failure_message_never_quotes_the_recipient() -> None:
    """Invariant 5: botocore's message names the address; ours must not repeat it."""
    with pytest.raises(SesDispatchError) as raised:
        dispatch(RecordingSes(raises=client_error("Throttling")))
    assert DESTINATION not in str(raised.value)
    assert "northwind" not in str(raised.value).lower()
    assert "Throttling" in str(raised.value)


def test_a_blank_destination_is_refused_before_anything_is_sent() -> None:
    client = RecordingSes()
    with pytest.raises(ValueError, match="destination"):
        dispatch(client, destination="   ")
    assert client.requests == []


# ------------------------------------------------------------------------ wiring errors


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"feedback_base_url": "/feedback"}, "absolute"),
        ({"token_secret": b"short"}, "at least"),
        ({"token_ttl_days": 0}, "positive"),
    ],
    ids=["relative-base-url", "short-secret", "zero-ttl"],
)
def test_a_misconfigured_notifier_fails_at_construction(
    overrides: dict[str, Any], match: str
) -> None:
    """Not on the first lead, and not as a link nobody can use."""
    with pytest.raises(ValueError, match=match):
        notifier(RecordingSes(), **overrides)


def test_a_blank_sender_identity_is_refused() -> None:
    with pytest.raises(ValueError, match="sender"):
        SesIdentity(sender="  ")


# ------------------------------------------------------------------------------- logs


def test_no_address_of_any_kind_reaches_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Invariant 5, on the one adapter that necessarily handles addresses."""
    caplog.set_level(logging.DEBUG, logger="leadquali")
    dispatch(RecordingSes())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged, "the send is supposed to be logged"
    assert DESTINATION not in logged
    assert "dana@northwind.example" not in logged
    assert "Dana Whitfield" not in logged
    assert "@" not in logged
    # ...and what is there instead: identifiers and the opaque destination hash.
    assert LEAD in logged
    assert rater_id(DESTINATION) in logged


def test_a_failed_send_logs_nothing_about_the_lead(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="leadquali")
    with pytest.raises(SesDispatchError):
        dispatch(RecordingSes(raises=client_error("MessageRejected")))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert DESTINATION not in logged
    assert "dana@northwind.example" not in logged
