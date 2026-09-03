"""SES delivery of the routing email, and the feedback links that ride on it.

The :class:`~leadquali.app.ports.NotifierPort` implementation, and the only module in the
system that imports ``boto3`` for email (``CLAUDE.md``'s layering rule). Everything about
*what the email says* lives in :mod:`leadquali.app.lead_email`, which is pure and needs no
credentials to test; everything here is about handing those bytes to Amazon and about what
to do when Amazon says no.

What this adapter is actually for
---------------------------------

Not "sending an email" — the routing email is the *carrier*. `docs/IMPLEMENTATION_PLAN.md`
§7 and the decision record are blunt about it: there is no historical labelled data, so the
golden set can only grow from the ``feedback`` table, and the only thing that writes to that
table is a rep clicking a link in one of these messages. Every send therefore mints two
signed, expiring, single-verdict links (:mod:`leadquali.app.feedback`) and puts them where a
thumb lands. A send that goes out without them is a lead that arrives and teaches us
nothing.

Failure is the queue's problem, not ours
----------------------------------------

``app/qualify.py`` persists the lead and its assessment *before* dispatching, records a
non-final ``FAILED`` routing event if the dispatch raises, and re-raises so SQS redelivers
and, eventually, the DLQ alarms. That ordering is what makes raising safe, so every failure
here raises — a throttle, a rejected identity, a network error, a malformed response. There
is deliberately no swallow-and-return-``None`` path: returning normally tells the pipeline
the lead reached a person, which would let it write a *final* routing event and make
:meth:`~leadquali.app.ports.LeadStorePort.already_routed` true forever. One SES outage would
then turn into permanently lost leads, which is invariant 3 broken by the very mechanism
meant to protect it.

Throttling gets its own exception type (:class:`SesThrottledError`) because it means something
different operationally — the sending rate is above the account's quota and the right
response is to slow the workers down (#26 owns the concurrency), not to page someone about a
broken identity — and because #21's metrics need to tell the two apart.

Boto is configured with standard-mode retries so a single 429 or 5xx is retried inside the
call before it becomes an SQS redelivery: a redelivery costs a whole model call, and a retry
costs a few hundred milliseconds.

Invariant 5 in an adapter that handles addresses
------------------------------------------------

This module necessarily *holds* email addresses — that is what it is for — but it never logs
one. Log lines carry the tenant, the lead id, the SES message id and
:func:`~leadquali.app.feedback.rater_id`'s opaque hash of the destination, which is the same
value the resulting ``feedback`` row is filed under, so an operator can follow one inbox
through the logs and into the database without an address appearing in either. Exception
messages are held to the same rule: a ``ClientError`` from botocore can quote the recipient,
so the raised error is constructed from the error code and never from the SDK's message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from leadquali.adapters.clock_system import SystemClock
from leadquali.app.feedback import (
    DEFAULT_TOKEN_TTL_DAYS,
    Verdict,
    check_token_secret,
    feedback_url,
    load_token_secret,
    mint_token,
    rater_id,
)
from leadquali.app.lead_email import FeedbackLink, RenderedEmail, render_routing_email
from leadquali.app.ports import ClockPort
from leadquali.config import Settings, get_settings
from leadquali.domain.models import LeadAssessment, RoutingDecision
from leadquali.prompts.lead import LeadSubmission

LOGGER = logging.getLogger(__name__)

#: The SES API this adapter speaks. v2 is the current one: it is where configuration sets,
#: per-message tags and the account-level suppression list live, and v1 is maintenance-only.
#: Annotated bare ``Final`` rather than ``Final[str]`` so it keeps its literal type: boto3's
#: stubs overload ``client()`` on the service name, and a widened ``str`` matches no overload.
SES_SERVICE_NAME: Final = "sesv2"

#: Charset for every part of the message. A lead writes in whatever language they like, and
#: an email that mangles a name is an email a rep will not send from.
CHARSET: Final[str] = "UTF-8"

#: The verdicts a routing email offers. ``UNSURE`` is a real verdict in the schema but not a
#: button: three choices on a phone is a decision, and the feature depends on it being a
#: reflex. A rep with no opinion simply does not click.
OFFERED_VERDICTS: Final[tuple[Verdict, ...]] = (Verdict.GOOD, Verdict.BAD)

#: SES error codes that mean "you are going too fast", as opposed to "this message is
#: wrong". Matched case-sensitively because that is how botocore reports them.
THROTTLING_CODES: Final[frozenset[str]] = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "SendingPausedException",
        "LimitExceededException",
        "RequestThrottled",
        "SlowDown",
    }
)

#: Retries inside the SDK before the failure becomes an SQS redelivery. Standard mode
#: retries throttles and transient 5xx with exponential backoff and jitter.
_MAX_ATTEMPTS: Final[int] = 3
_CONNECT_TIMEOUT_SECONDS: Final[int] = 5
_READ_TIMEOUT_SECONDS: Final[int] = 10

#: Tag values SES accepts are ``[A-Za-z0-9_-]`` only, and a rejected tag rejects the whole
#: send — so anything that does not fit is replaced rather than passed through.
_TAG_SAFE: Final[str] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
_MAX_TAG_CHARS: Final[int] = 256


class SesDispatchError(RuntimeError):
    """The routing email did not go out.

    Raised for every failure, because the pipeline's contract is that a dispatch failure
    re-raises so the queue redelivers. The message names the error code and never the
    recipient (invariant 5).
    """


class SesThrottledError(SesDispatchError):
    """SES refused because we are sending faster than the account's quota allows.

    Its own type because the operational response is different: throttling is a capacity
    signal for #26's worker concurrency, not a broken configuration. It is still an
    exception, because a throttled send is a lead that has not reached anybody yet.
    """


@dataclass(frozen=True, slots=True)
class SesIdentity:
    """The sending identity and the SES configuration set, both from configuration.

    Never literals: `docs/runbooks/ses-setup.md` (#20) owns the verified domain, the DKIM
    records and the configuration set that routes bounce and complaint events, and those
    differ per environment. A hardcoded sender is an email that fails to send in staging or,
    worse, one that sends from production's domain.
    """

    sender: str
    """``From``, either a bare address or ``Display Name <address>``."""

    configuration_set: str | None = None
    """The SES configuration set. ``None`` sends without one — legal, and it means bounce
    and complaint events go nowhere, which #20's runbook exists to prevent."""

    def __post_init__(self) -> None:
        if not self.sender.strip():
            raise ValueError("the SES sender identity must not be blank; see SES_SENDER")


class SesNotifier:
    """:class:`~leadquali.app.ports.NotifierPort` over Amazon SES v2.

    The boto client is a constructor argument rather than something built on demand: it is
    deployment-scoped and expensive to create (it resolves credentials and loads service
    models), a Lambda container should build exactly one and reuse it across invocations,
    and a test wants to hand in a ``moto``-backed one. :meth:`from_env` is the production
    convenience that builds all of it from :class:`~leadquali.config.Settings`.
    """

    def __init__(
        self,
        *,
        client: Any,
        identity: SesIdentity,
        feedback_base_url: str,
        token_secret: bytes,
        clock: ClockPort | None = None,
        token_ttl_days: int = DEFAULT_TOKEN_TTL_DAYS,
    ) -> None:
        """Wire the notifier.

        Args:
            client: an SES v2 client, or anything with its ``send_email``.
            identity: the sender and configuration set, from #20's runbook via config.
            feedback_base_url: the public origin the feedback links point at. Configuration,
                not a request header — see :func:`~leadquali.app.feedback.feedback_url`.
            token_secret: the process feedback signing secret.
            clock: time, injected. Defaults to the system clock.
            token_ttl_days: how long a feedback link stays usable.

        Raises:
            ValueError: the base URL is not absolute, the secret is too short, or the TTL is
                not positive. All three are wiring mistakes that would otherwise ship as
                links nobody can use.
        """
        if token_ttl_days <= 0:
            raise ValueError("token_ttl_days must be positive; a link that never works is not one")
        # Fail here rather than on the first lead: both raise on bad input.
        feedback_url(base_url=feedback_base_url, token="probe")  # noqa: S106 — a throwaway probe
        check_token_secret(token_secret)

        self._client = client
        self._identity = identity
        self._base_url = feedback_base_url
        self._secret = token_secret
        self._clock: ClockPort = clock if clock is not None else SystemClock()
        self._ttl = timedelta(days=token_ttl_days)

    @classmethod
    def from_env(
        cls, settings: Settings | None = None, *, clock: ClockPort | None = None
    ) -> SesNotifier:
        """Build the production notifier from the environment.

        Raises:
            RuntimeError: a required value — ``SES_SENDER``, ``FEEDBACK_BASE_URL`` or
                ``FEEDBACK_TOKEN_SECRET`` — is not configured. Loud at wiring time: a worker
                that starts without them would qualify leads and then fail to deliver every
                one of them.
        """
        resolved = settings if settings is not None else get_settings()
        return cls(
            client=build_client(resolved),
            identity=SesIdentity(
                sender=resolved.require_ses_sender(),
                configuration_set=resolved.ses_configuration_set,
            ),
            feedback_base_url=resolved.require_feedback_base_url(),
            token_secret=load_token_secret(resolved.require_feedback_token_secret()),
            clock=clock,
            token_ttl_days=resolved.feedback_token_ttl_days,
        )

    # ------------------------------------------------------------------- the port

    def dispatch(
        self,
        *,
        tenant_id: str,
        lead_id: str,
        destination: str,
        submission: LeadSubmission,
        decision: RoutingDecision,
        assessment: LeadAssessment | None,
    ) -> str | None:
        """Send one routed lead to one destination and return the SES message id.

        ``assessment is None`` is the system-failure path and is **not** an error here: the
        email still goes, carrying #9's "system could not assess" banner, because a lead
        nobody can score is still a lead somebody must call.

        Raises:
            ValueError: ``destination`` is blank. Choosing where a lead goes is policy and
                the caller resolved it; a blank one is a bug upstream, and sending to nobody
                is not a recoverable state.
            SesThrottledError: SES refused for rate. The pipeline records a non-final failure and
                re-raises, and the queue redelivers.
            SesDispatchError: any other delivery failure, for the same treatment.
        """
        if not destination.strip():
            raise ValueError("destination must not be blank; the caller resolves it from policy")

        rater = rater_id(destination)
        email = render_routing_email(
            submission=submission,
            decision=decision,
            assessment=assessment,
            links=self._links(tenant_id=tenant_id, lead_id=lead_id, rater=rater),
            lead_reference=lead_id,
        )
        message_id = self._send(
            destination=destination,
            email=email,
            reply_to=submission.email,
            tenant_id=tenant_id,
            decision=decision,
        )

        # Identifiers and an opaque destination hash. Never the address (invariant 5).
        LOGGER.info(
            "routing email sent tenant=%s lead=%s rater=%s tier=%s assessed=%s message_id=%s",
            tenant_id,
            lead_id,
            rater,
            decision.tier.value,
            assessment is not None,
            message_id,
        )
        return message_id

    # ---------------------------------------------------------------- the pieces

    def _links(self, *, tenant_id: str, lead_id: str, rater: str) -> list[FeedbackLink]:
        """Mint one single-purpose link per offered verdict.

        Separate tokens rather than one token plus a verdict parameter: a verdict outside
        the signature is a verdict anyone holding the link can change, and the link travels
        through mail servers, spam filters and forwarded mailboxes.
        """
        expires_at = self._clock.now() + self._ttl
        return [
            FeedbackLink(
                verdict=verdict,
                url=feedback_url(
                    base_url=self._base_url,
                    token=mint_token(
                        secret=self._secret,
                        tenant_id=tenant_id,
                        lead_id=lead_id,
                        verdict=verdict,
                        rater=rater,
                        expires_at=expires_at,
                    ),
                ),
            )
            for verdict in OFFERED_VERDICTS
        ]

    def _send(
        self,
        *,
        destination: str,
        email: RenderedEmail,
        reply_to: str | None,
        tenant_id: str,
        decision: RoutingDecision,
    ) -> str | None:
        """One ``SendEmail`` call, with every failure converted into a raise."""
        request: dict[str, Any] = {
            "FromEmailAddress": self._identity.sender,
            "Destination": {"ToAddresses": [destination]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": email.subject, "Charset": CHARSET},
                    "Body": {
                        # Both parts, always: SES assembles the multipart/alternative, and a
                        # message with no text part reads as spam to filters and as nothing
                        # at all to a screen reader.
                        "Text": {"Data": email.text_body, "Charset": CHARSET},
                        "Html": {"Data": email.html_body, "Charset": CHARSET},
                    },
                }
            },
            "EmailTags": _tags(tenant_id=tenant_id, decision=decision),
        }
        # Reply-To is the lead, so a rep hits reply and reaches the human being who filled
        # in the form. The address is in the body either way; this removes the copy-paste
        # step that is the difference between a reply and a lead going cold.
        if reply_to and reply_to.strip():
            request["ReplyToAddresses"] = [reply_to.strip()]
        if self._identity.configuration_set:
            request["ConfigurationSetName"] = self._identity.configuration_set

        try:
            response = self._client.send_email(**request)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", "Unknown"))
            # The SDK's message can quote the recipient; only the code crosses this line.
            if code in THROTTLING_CODES:
                raise SesThrottledError(f"SES throttled the send (code {code})") from None
            raise SesDispatchError(f"SES refused the send (code {code})") from None
        except BotoCoreError as error:
            raise SesDispatchError(
                f"SES send failed before a response ({type(error).__name__})"
            ) from None

        message_id = response.get("MessageId") if isinstance(response, dict) else None
        if not message_id:
            # Every successful SendEmail returns one. Nothing without it is a success we can
            # trace a bounce back to, and treating it as one would put a lie in
            # routing_events.provider_message_id.
            raise SesDispatchError("SES returned no MessageId; treating the send as failed")
        return str(message_id)


def build_client(settings: Settings | None = None) -> Any:
    """Build the SES v2 client this adapter uses in production.

    Region comes from configuration when set and from the ambient AWS chain otherwise, so a
    Lambda picks up its own region without being told. Retries are standard mode so a single
    throttle or 5xx costs a backoff rather than a redelivery and another model call.
    """
    resolved = settings if settings is not None else get_settings()
    config = Config(
        retries={"max_attempts": _MAX_ATTEMPTS, "mode": "standard"},
        connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        read_timeout=_READ_TIMEOUT_SECONDS,
    )
    if resolved.aws_region:
        return boto3.client(SES_SERVICE_NAME, region_name=resolved.aws_region, config=config)
    return boto3.client(SES_SERVICE_NAME, config=config)


def _tags(*, tenant_id: str, decision: RoutingDecision) -> list[dict[str, str]]:
    """Per-message tags, which become dimensions on the configuration set's events.

    This is what makes "which tenant's hot leads are bouncing" answerable in #21 without
    joining anything. Values are sanitised because SES accepts only ``[A-Za-z0-9_-]`` in a
    tag and rejects the entire send otherwise — a metric label must never be able to stop a
    lead reaching sales.
    """
    return [
        {"Name": "tenant", "Value": _tag_value(tenant_id)},
        {"Name": "tier", "Value": _tag_value(decision.tier.value)},
        {"Name": "escalated", "Value": "yes" if decision.escalated else "no"},
    ]


def _tag_value(value: str) -> str:
    cleaned = "".join(character if character in _TAG_SAFE else "_" for character in value)
    return cleaned[:_MAX_TAG_CHARS] or "unknown"


__all__ = [
    "CHARSET",
    "OFFERED_VERDICTS",
    "SES_SERVICE_NAME",
    "THROTTLING_CODES",
    "SesDispatchError",
    "SesIdentity",
    "SesNotifier",
    "SesThrottledError",
    "build_client",
]
