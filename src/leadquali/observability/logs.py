"""One JSON object per line, configured once, safe to configure again.

**The field set is the contract.** Every record carries ``timestamp``, ``level``,
``logger``, ``message``, ``service`` and ``env``; every record written by our own code also
carries ``event`` (a stable dotted name) and whatever identifiers are bound to the context
— ``trace_id`` first among them. Event-specific fields sit flat at the root next to those,
in ``snake_case``, because ``fields @message | filter tier = "hot"`` in Logs Insights is
one line and ``json_parse`` gymnastics over a nested object is not. Nothing is nested
except ``exception``, which has to be.

**Why ``configure_logging`` is idempotent rather than guarded by a module flag.** A Lambda
container is reused across invocations and a handler added per invocation is a line
duplicated per invocation — by the hundredth call the log bill is two orders of magnitude
off and every metric is counted a hundred times. A flag would fix that and break the other
case: the same process may legitimately reconfigure (a test, a CLI that reads its settings
after import, ``uvicorn --reload``). So the function is written to *converge* — it removes
the handler it installed before and installs exactly one — which is correct however many
times it is called, in whatever order.

It also takes the root logger over rather than adding alongside. In Lambda the runtime
pre-installs its own handler, and leaving it in place means every line appears twice: once
as JSON and once with a request-id prefix. Anything already on the root logger is a
formatter we did not choose, which by definition is not the format this module exists to
guarantee.

**Local development gets prose.** ``ENV=local`` selects
:class:`HumanLogFormatter` — the same fields, on one readable line — because a person
tailing a terminal reads ``lead.routed tenant_id=acme tier=hot`` and does not read a JSON
object. Every deployed environment gets JSON. Nothing about *what* is emitted changes with
the format; only how it is rendered.

Both formatters run every rendered message and every traceback through
:func:`~leadquali.observability.pii.redact_emails` on the way out (invariant 5).
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Final, Literal, TextIO

from leadquali.config import Environment, Settings, get_settings
from leadquali.observability.context import current_context
from leadquali.observability.metrics import MetricPayload, to_emf, to_text
from leadquali.observability.pii import redact_emails

#: Rendered as JSON, one object per line. Every deployed environment.
LOG_FORMAT_JSON: Final[str] = "json"

#: Rendered for a person. The default when ``ENV=local``.
LOG_FORMAT_HUMAN: Final[str] = "human"

type LogFormat = Literal["json", "human"]

#: Stamped on every record so one log group can hold more than one service.
SERVICE_NAME: Final[str] = "leadquali"

#: Fields the formatter owns. An event field of the same name is overwritten rather than
#: honoured: a caller must not be able to move a record's own timestamp or level.
RESERVED_FIELDS: Final[frozenset[str]] = frozenset(
    {"timestamp", "level", "logger", "message", "event", "service", "env", "exception", "_aws"}
)

#: Third-party loggers pinned no lower than ``WARNING``. At ``LOG_LEVEL=DEBUG`` these emit
#: every SQL statement, every HTTP request and every retry, which buries our own lines and
#: — for SQLAlchemy's echo of bound parameters — would put a lead's email address in the
#: log, through a code path no amount of discipline at our own call sites can reach.
QUIET_LOGGERS: Final[tuple[str, ...]] = (
    "anthropic",
    "boto3",
    "botocore",
    "httpcore",
    "httpx",
    "s3transfer",
    "sqlalchemy.engine",
    "urllib3",
)

#: Attribute the payload of a structured event is stashed on. One key, rather than
#: splatting fields onto the record, because ``extra=`` silently corrupts a record when a
#: field happens to be called ``name``, ``msg``, ``args`` or ``levelname``.
_PAYLOAD_ATTR: Final[str] = "_lq"

#: Marks the handler this module installed, so a second call can find and replace it.
_HANDLER_MARK: Final[str] = "_leadquali_handler"


@dataclass(frozen=True, slots=True)
class EventPayload:
    """The structured half of one log record: its event name, fields and metrics."""

    event: str
    fields: dict[str, Any] = field(default_factory=dict)
    metrics: MetricPayload | None = None


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    exc_info: BaseException | bool | None = None,
    metrics: MetricPayload | None = None,
    fields: Mapping[str, Any] | None = None,
    **inline_fields: Any,
) -> None:
    """Emit one structured record.

    Args:
        logger: the module's logger, so the ``logger`` field says where this came from.
        event: the stable dotted event name, e.g. ``lead.routed``. Doubles as the message
            when none is given — a name is more useful than a sentence to filter on, and
            two spellings of the same event is how a dashboard quietly stops matching.
        level: the log level.
        message: a human sentence, when the event name alone is not enough.
        exc_info: an exception to attach. Its type, its redacted message and its traceback
            are recorded; nothing else from the frame is.
        metrics: what this event publishes to CloudWatch, if anything.
        fields: event-specific fields built programmatically — a flattened
            :class:`~leadquali.app.assessment_result.CallMetering`, say. Exists alongside
            ``**inline_fields`` because splatting a ``dict[str, Any]`` into a signature
            with typed keyword-only parameters is not something a type checker can accept,
            and losing the typing on ``level`` and ``metrics`` to save this parameter would
            be the wrong trade.
        **inline_fields: the same thing, written out at the call site. Merged over
            ``fields`` on a clash. ``None`` values are dropped rather than emitted as
            ``null``, so a query for "records with an escalation reason" means what it
            says.

    Note:
        Fields must be PII-free by construction — ``contact_email_hash``, never ``email``;
        a decision's note, never the lead's message. The formatter's redaction is a net for
        what arrives from outside, not a licence to log a submission.
    """
    logger.log(
        level,
        message if message is not None else event,
        exc_info=exc_info,
        # So that `record.pathname`, `funcName` and `lineno` name the call site rather than
        # this line. Nothing in the JSON format uses them, but pytest's log capture and any
        # `%(lineno)d` formatter do, and a whole codebase whose logs all claim to come from
        # `logs.py` is a debugging tax paid forever to save one argument here.
        stacklevel=2,
        extra={
            _PAYLOAD_ATTR: EventPayload(
                event=event,
                fields={**(fields or {}), **inline_fields},
                metrics=metrics,
            )
        },
    )


class _BaseFormatter(logging.Formatter):
    """Shared record → fields mapping. Rendering is the subclass's business."""

    def __init__(self, *, env: str) -> None:
        super().__init__()
        self._env = env

    def _payload(self, record: logging.LogRecord) -> EventPayload | None:
        candidate = getattr(record, _PAYLOAD_ATTR, None)
        return candidate if isinstance(candidate, EventPayload) else None

    def _core(self, record: logging.LogRecord, payload: EventPayload | None) -> dict[str, Any]:
        """The record as a flat mapping, in the order a person reads it.

        The formatter's own fields go first — a line starts with when, how bad, and what
        happened — then the ambient identifiers, then the event's own fields. Precedence
        runs the other way and is enforced by filtering rather than by ordering: neither
        the context nor an event field can occupy a name in :data:`RESERVED_FIELDS`, so a
        caller cannot move a record's timestamp or relabel its level, and an explicit field
        still beats the ambient context because it is merged afterwards.
        """
        merged: dict[str, Any] = {
            "timestamp": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_emails(record.getMessage()),
            "service": SERVICE_NAME,
            "env": self._env,
        }
        if payload is not None:
            merged["event"] = payload.event
        merged.update(
            {
                key: _redact_value(value)
                for key, value in current_context().items()
                if key not in RESERVED_FIELDS
            }
        )
        if payload is not None:
            merged.update(
                {
                    key: _redact_value(value)
                    for key, value in payload.fields.items()
                    if value is not None and key not in RESERVED_FIELDS
                }
            )
        return merged

    def _exception(self, record: logging.LogRecord) -> dict[str, str] | None:
        """The exception, flattened and redacted.

        Only the type, the message and the formatted traceback. Local variables are never
        rendered — a frame in this codebase holds a ``LeadSubmission``, and a traceback
        that printed locals would put a stranger's name, address and free text in the log
        on the one path nobody rehearses. The message and the traceback text still go
        through the redactor, because the message is written by whatever raised.
        """
        if record.exc_info is None or record.exc_info[1] is None:
            return None
        _, error, tb = record.exc_info
        stack = "".join(traceback.format_exception(type(error), error, tb))
        return {
            "type": type(error).__name__,
            "message": redact_emails(str(error)),
            "stack": redact_emails(stack),
        }


class JsonLogFormatter(_BaseFormatter):
    """One JSON object per line, with EMF metrics merged into the same object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        document = self._core(record, payload)
        exception = self._exception(record)
        if exception is not None:
            document["exception"] = exception
        if payload is not None and payload.metrics is not None:
            document.update(to_emf(payload.metrics, timestamp_ms=int(record.created * 1000)))
        return json.dumps(document, default=_json_default, ensure_ascii=False)


class HumanLogFormatter(_BaseFormatter):
    """The same fields on one readable line. Local development only."""

    def format(self, record: logging.LogRecord) -> str:
        payload = self._payload(record)
        document = self._core(record, payload)
        head = f"{document['timestamp']} {record.levelname:<7} {record.name}"
        title = payload.event if payload is not None else document["message"]
        rest = " ".join(
            f"{key}={_json_default(value) if not isinstance(value, str | int | float) else value}"
            for key, value in document.items()
            if key not in RESERVED_FIELDS
        )
        line = f"{head} {title}"
        if payload is not None and document["message"] != payload.event:
            line = f"{line} {document['message']!r}"
        if rest:
            line = f"{line} {rest}"
        if payload is not None and payload.metrics is not None:
            line = f"{line} {to_text(payload.metrics)}"
        exception = self._exception(record)
        if exception is not None:
            line = f"{line}\n{exception['stack'].rstrip()}"
        return line


def configure_logging(
    settings: Settings | None = None,
    *,
    stream: TextIO | None = None,
    log_format: LogFormat | str | None = None,
    replace_existing: bool = True,
) -> logging.Handler:
    """Install exactly one handler on the root logger. Safe to call any number of times.

    Args:
        settings: where ``LOG_LEVEL`` and ``ENV`` come from. ``None`` reads the process
            settings, which is what an entry point wants; the tests pass their own.
        stream: where lines go. Defaults to ``stdout``, which is what CloudWatch Logs and
            ``docker logs`` read. ``stderr`` would work equally in Lambda and would
            interleave badly everywhere else.
        log_format: ``"json"`` or ``"human"``. ``None`` — the default — derives it from
            ``ENV``: prose locally, JSON in every deployed environment.
        replace_existing: also detach handlers this module did not install. True by
            default, because the AWS Lambda runtime pre-installs one and leaving it
            produces every line twice. Pass ``False`` when logging must coexist with a
            harness that owns the root logger.

    Returns:
        The installed handler, so a caller (or a test) can address it directly.
    """
    resolved = settings if settings is not None else get_settings()
    chosen = log_format or (
        LOG_FORMAT_HUMAN if resolved.env is Environment.LOCAL else LOG_FORMAT_JSON
    )
    formatter: logging.Formatter = (
        HumanLogFormatter(env=resolved.env.value)
        if chosen == LOG_FORMAT_HUMAN
        else JsonLogFormatter(env=resolved.env.value)
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        if replace_existing or getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(resolved.log_level)

    floor = max(logging.WARNING, root.level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(floor)
    return handler


def _timestamp(created: float) -> str:
    """UTC, milliseconds, trailing ``Z``. Sortable as a string, which is how it is read."""
    moment = datetime.fromtimestamp(created, UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _redact_value(value: Any) -> Any:
    """Redact addresses anywhere in a field value, however deeply it is nested.

    Applied to every field of every record rather than trusted to call sites. A field is
    the *likeliest* place for an address to appear — a destination, a bounce reason, an
    error string quoted from a provider — and ``json.dumps`` would emit a plain ``str``
    without ever consulting the serialiser hook below.
    """
    if isinstance(value, str):
        return redact_emails(value)
    if isinstance(value, Mapping):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact_value(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    """Serialise what ``json`` will not, and redact whatever comes out as text."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return redact_emails(str(value))


__all__ = [
    "LOG_FORMAT_HUMAN",
    "LOG_FORMAT_JSON",
    "QUIET_LOGGERS",
    "RESERVED_FIELDS",
    "SERVICE_NAME",
    "EventPayload",
    "HumanLogFormatter",
    "JsonLogFormatter",
    "LogFormat",
    "configure_logging",
    "log_event",
]
