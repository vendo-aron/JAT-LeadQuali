"""The rubric as data: everything about *this customer's* policy, in one validated value.

Invariant 1 of ``CLAUDE.md`` is the reason this module exists. The ICP paragraph, the
per-dimension weights, the tier boundaries, the confidence gate and the routing table are
tenant configuration. Onboarding customer #2, or retuning customer #1 after a bad week, is
a write to a ``jsonb`` column — never a Python edit, never a deploy, never a regression
risk to anyone else. The only numbers hardcoded here are the documented defaults a new
tenant inherits until it states its own.

Two things follow from that, and both are load-time concerns:

* **A misconfigured tenant must fail loudly, at load, naming itself.** A gap between the
  ``warm`` and ``cold`` bands or a weight for a dimension the model does not score is a
  silent mis-routing at 3am otherwise. Every constraint below is checked when the config is
  built, and :meth:`TenantConfig.from_dict` puts the tenant's id in the message so the
  operator knows which config to fix.
* **Serialisation is byte-stable.** :meth:`TenantConfig.icp_block` is the head of the
  prompt, and the prompt prefix is cached (#11). A dict that iterates in insertion order,
  a CRLF from a config edited on Windows, or a float that renders differently on two hosts
  would change those bytes and drop the cache hit rate to zero without any visible error.
  Everything in the block is sorted, normalised and fixed-format.

Boundary with #9 (``domain/scoring.py`` / ``domain/routing.py``): this module owns policy
**data** and the pure lookups that read it — :meth:`TenantConfig.tier_for`,
:meth:`TenantConfig.action_for`, :meth:`TenantConfig.destination_for` — plus the scale
constant :attr:`TenantConfig.max_weighted_raw`. It never sees a
:class:`~leadquali.domain.models.LeadAssessment`. Policy **computation** —
``weighted_total`` and ``decide`` — lives in #9 and reads this config.

Pure Pydantic v2 and the standard library. No I/O: loading a config from a file is an
adapter's job (``adapters/tenant_config_json.py``), behind
:class:`~leadquali.app.ports.TenantConfigPort`.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Self

from annotated_types import Le
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from leadquali.domain.models import MAX_TOTAL_SCORE, Action, DimensionScores, Tier


class TenantConfigError(ValueError):
    """A tenant's configuration is unusable, and the message says whose and why.

    Raised at load time only. Nothing downstream should ever have to defend against a
    half-valid config, so there is no partially-applied or "best effort" outcome.
    """


class TenantNotFoundError(TenantConfigError):
    """No configuration exists for the requested tenant."""


def _dimension_maxima() -> dict[str, int]:
    """Read each dimension's upper bound off :class:`DimensionScores`.

    Derived rather than restated so the two can never drift: adding a dimension to the
    assessment schema immediately invalidates every config that does not weight it, which
    is exactly the failure we want — loud, at load, not a dimension silently scoring zero.
    """
    maxima: dict[str, int] = {}
    for name, field in DimensionScores.model_fields.items():
        upper = next((meta.le for meta in field.metadata if isinstance(meta, Le)), None)
        if not isinstance(upper, int):
            raise RuntimeError(
                f"DimensionScores.{name} has no integer upper bound; tenant weights cannot "
                "be normalised against a dimension with an unbounded range."
            )
        maxima[name] = upper
    return maxima


#: Upper bound of each judgment dimension, read from the assessment schema.
DIMENSION_MAXIMA: Final[Mapping[str, int]] = _dimension_maxima()

#: The dimensions a tenant's weights must cover — exactly these, no more, no fewer.
DIMENSION_NAMES: Final[frozenset[str]] = frozenset(DIMENSION_MAXIMA)

#: Neutral default: the rubric's own emphasis (30/25/15/15/15) is already the house view of
#: what matters, so an unopinionated tenant multiplies each dimension by one and gets a
#: total that is simply the raw sum. A tenant selling to procurement raises ``authority``.
DEFAULT_WEIGHTS: Final[Mapping[str, float]] = {name: 1.0 for name in sorted(DIMENSION_NAMES)}

#: Below this, the model's own confidence is too low to route on and the lead goes to a
#: human (#9). Deliberately generous: a needless human look costs minutes, a missed deal
#: costs the deal.
DEFAULT_MIN_CONFIDENCE: Final[float] = 0.6

#: Which rubric revision a tenant is pinned to, recorded on every assessment so "did last
#: Tuesday's prompt change make things worse?" stays an answerable question.
DEFAULT_PROMPT_VERSION: Final[str] = "rubric_v1"

#: Tenant ids double as filenames and as jsonb keys, so they are constrained to a slug.
TENANT_ID_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9_-]{0,62}$"

#: A pinned rubric revision names a file on disk (``rubric_v1.md``) and is interpolated
#: into the system prompt's tenant envelope. Constraining it to a slug does both jobs:
#: it cannot escape a filename, and it cannot forge an XML-ish attribute or tag.
PROMPT_VERSION_PATTERN: Final[str] = r"^[a-z0-9][a-z0-9._-]*$"

#: Substrings a tenant must not be able to place in free text. ``icp_block()`` wraps the
#: tenant's own words in a ``<tenant_profile>`` envelope inside the *system* prompt; text
#: that closes or reopens that envelope would let a tenant's stored config plant
#: instructions in the trusted prefix. Today the config is a repo file, but #8 exists so
#: that in P5.1 it becomes a tenant-editable ``jsonb`` column - so the guard goes in now,
#: while it is a one-line validator rather than an incident.
FORBIDDEN_TEXT_FRAGMENTS: Final[tuple[str, ...]] = ("<tenant_profile", "</tenant_profile")

_ACTIONS_NEEDING_A_DESTINATION: Final[frozenset[Action]] = frozenset(
    {Action.EMAIL_SALES, Action.ESCALATE_HUMAN}
)


class TierThresholds(BaseModel):
    """The lower bound, inclusive, of each tier on the 0-100 scale.

    Bands are expressed as minimums rather than as ``(min, max)`` pairs so that overlaps
    and gaps are not merely rejected but *unrepresentable*: every score at or above
    :attr:`hot` is hot, everything from :attr:`warm` up to it is warm, and everything below
    :attr:`cold` is disqualified. The one way to break that — bounds out of order — is what
    the validator below catches.

    Defaults are the plan's: hot >= 80, warm 55-79, cold 30-54, disqualified < 30.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hot: float = Field(default=80.0, ge=0.0, le=MAX_TOTAL_SCORE, allow_inf_nan=False)
    warm: float = Field(default=55.0, ge=0.0, le=MAX_TOTAL_SCORE, allow_inf_nan=False)
    cold: float = Field(default=30.0, ge=0.0, le=MAX_TOTAL_SCORE, allow_inf_nan=False)

    @model_validator(mode="after")
    def _strictly_ordered(self) -> Self:
        if self.warm <= self.cold:
            raise ValueError(
                f"tier thresholds overlap: warm ({self.warm}) must be strictly above "
                f"cold ({self.cold})"
            )
        if self.hot <= self.warm:
            raise ValueError(
                f"tier thresholds overlap: hot ({self.hot}) must be strictly above "
                f"warm ({self.warm})"
            )
        return self

    def tier_for(self, total: float) -> Tier:
        """The tier a total on the 0-100 scale falls in. Lower bounds are inclusive."""
        if total >= self.hot:
            return Tier.HOT
        if total >= self.warm:
            return Tier.WARM
        if total >= self.cold:
            return Tier.COLD
        return Tier.DISQUALIFIED


#: The tier boundaries a tenant inherits until it sets its own.
DEFAULT_THRESHOLDS: Final[TierThresholds] = TierThresholds()


class RoutingRule(BaseModel):
    """What happens to a lead in one tier, and where it goes.

    ``destination`` is deliberately an opaque string rather than an email address: the
    dispatcher for a tenant on the HubSpot adapter reads it as a pipeline id, and the port
    boundary is the only place that should know the difference.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Action = Field(description="What the system does with a lead in this tier.")
    destination: str | None = Field(
        default=None,
        description="Where the lead goes: a sales inbox, a queue, a CRM pipeline id.",
    )

    @model_validator(mode="after")
    def _destination_matches_the_action(self) -> Self:
        """A delivering action needs somewhere to deliver; suppression needs nowhere.

        A tenant that configures ``email_sales`` with no address has silently switched a
        whole tier off, which is invariant 3 broken by typo rather than by design.
        """
        blank = self.destination is None or not self.destination.strip()
        if self.action in _ACTIONS_NEEDING_A_DESTINATION and blank:
            raise ValueError(f"action '{self.action.value}' requires a non-empty destination")
        if self.action is Action.SUPPRESS and not blank:
            raise ValueError(
                f"action '{Action.SUPPRESS.value}' delivers nothing and must not carry a "
                f"destination (got {self.destination!r})"
            )
        return self


class TenantConfig(BaseModel):
    """One customer's complete qualification policy, as data.

    Everything a tenant can tune lives here and nowhere else. Only the fields that identify
    the tenant and describe its customers are required; the rubric numerics default to the
    documented house policy, so onboarding is three fields plus a routing table.

    ``routing_rules`` has no default on purpose: destinations are the one part of the
    policy that cannot be guessed, and a placeholder address would mean a tenant's leads
    going nowhere with no error at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(
        pattern=TENANT_ID_PATTERN,
        description="Stable slug identifying the tenant; also its config filename.",
    )
    name: str = Field(min_length=1, max_length=200, description="Human-readable tenant name.")
    icp_description: str = Field(
        min_length=1,
        max_length=8000,
        description="Free-text ideal customer profile, injected into the prompt.",
    )
    weights: Mapping[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_WEIGHTS),
        # Without this the default skips every field validator, so a config that omits
        # `weights` would carry a plain, mutable dict while an explicit one is frozen.
        validate_default=True,
        description="Per-dimension multiplier; must cover exactly the assessment's dimensions.",
    )
    thresholds: TierThresholds = Field(
        default_factory=lambda: DEFAULT_THRESHOLDS,
        description="Inclusive lower bound of each tier on the 0-100 scale.",
    )
    min_confidence: float = Field(
        default=DEFAULT_MIN_CONFIDENCE,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="Below this model confidence the lead escalates to a human.",
    )
    routing_rules: Mapping[Tier, RoutingRule] = Field(
        description="Action and destination for every tier; all four are required."
    )
    prompt_version: str = Field(
        default=DEFAULT_PROMPT_VERSION,
        pattern=PROMPT_VERSION_PATTERN,
        min_length=1,
        max_length=64,
        description="Rubric revision this tenant is pinned to; recorded per assessment.",
    )

    # ------------------------------------------------------------------ validation

    @field_serializer("weights")
    def _serialise_weights(self, value: Mapping[str, float]) -> dict[str, float]:
        """Emit a plain dict: the stored mapping is a ``mappingproxy``, which pydantic
        cannot serialise, and ``icp_config`` is written to a ``jsonb`` column."""
        return dict(value)

    @field_serializer("routing_rules")
    def _serialise_routing_rules(
        self, value: Mapping[Tier, RoutingRule]
    ) -> dict[Tier, RoutingRule]:
        """Plain dict, for the same reason as :meth:`_serialise_weights`."""
        return dict(value)

    @field_validator("weights", mode="after")
    @classmethod
    def _freeze_weights(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        """Normalise negative zero, then make the mapping immutable.

        Two separate traps. ``-0.0`` passes a ``weight < 0.0`` check, survives a JSON
        round-trip, and renders as ``-0.00`` in :meth:`icp_block` - so two configs that
        compare equal would produce different prompt bytes and silently halve the cache
        hit rate. Adding ``0.0`` maps it to ``+0.0`` (IEEE-754) and leaves every other
        value alone.

        And ``frozen=True`` only blocks attribute *reassignment*: without this,
        ``cfg.weights["icp_fit"] = 99.0`` succeeds on a validated config, changing both
        the prompt and the scoring scale after every validator has passed.
        """
        return MappingProxyType({name: weight + 0.0 for name, weight in value.items()})

    @field_validator("routing_rules", mode="after")
    @classmethod
    def _freeze_routing_rules(cls, value: Mapping[Tier, RoutingRule]) -> Mapping[Tier, RoutingRule]:
        """Same immutability argument as :meth:`_freeze_weights`; ``RoutingRule`` is frozen."""
        return MappingProxyType(dict(value))

    @field_validator("name", "icp_description", mode="after")
    @classmethod
    def _reject_envelope_forgery(cls, value: str) -> str:
        """Refuse text that could break out of the prompt's tenant envelope.

        Only the literal delimiter is rejected, not ``<`` in general: an ICP genuinely
        says things like "companies with <200 employees", and refusing that would make
        the validator the tenant's enemy.
        """
        lowered = value.lower()
        for fragment in FORBIDDEN_TEXT_FRAGMENTS:
            if fragment in lowered:
                raise ValueError(
                    f"text may not contain {fragment!r}: it would break out of the "
                    "tenant envelope in the system prompt"
                )
        return value

    @field_validator("name", "icp_description", mode="after")
    @classmethod
    def _canonical_text(cls, value: str) -> str:
        """Canonicalise free text once, at the boundary, instead of on every prompt build.

        Line endings become ``\n`` and trailing whitespace goes, so the same config saved
        from a Windows editor and from a Unix one is the same value — and therefore the
        same cacheable prompt prefix. Text that is nothing but whitespace is rejected: an
        empty ICP block would leave the model qualifying against no profile at all.
        """
        canonical = _normalise_text(value)
        if not canonical:
            raise ValueError("must contain more than whitespace")
        return canonical

    @model_validator(mode="after")
    def _weights_cover_exactly_the_scored_dimensions(self) -> Self:
        supplied = set(self.weights)
        missing = sorted(DIMENSION_NAMES - supplied)
        unknown = sorted(supplied - DIMENSION_NAMES)
        if missing or unknown:
            parts = []
            if missing:
                parts.append(f"missing weight for {', '.join(missing)}")
            if unknown:
                parts.append(f"unknown dimension {', '.join(unknown)}")
            raise ValueError(f"tenant '{self.tenant_id}': weights are wrong — {'; '.join(parts)}")
        for dimension, weight in sorted(self.weights.items()):
            if not math.isfinite(weight) or weight < 0.0:
                raise ValueError(
                    f"tenant '{self.tenant_id}': weight for {dimension} must be a finite "
                    f"non-negative number, got {weight}"
                )
        return self

    @model_validator(mode="after")
    def _normalisation_is_well_defined(self) -> Self:
        """The weighted scale must have a positive top, or no lead could ever be hot.

        Totals are normalised onto 0-100 by dividing by :attr:`max_weighted_raw`, so a
        config whose weights all zero out has no scale at all — and, less obviously, every
        config that survives this check *can* reach its hot threshold, because a maximal
        assessment normalises to exactly ``MAX_TOTAL_SCORE`` and ``hot <= MAX_TOTAL_SCORE``.
        """
        if self.max_weighted_raw <= 0.0:
            raise ValueError(
                f"tenant '{self.tenant_id}': every dimension weight is zero, so no lead "
                "could reach any tier above disqualified"
            )
        return self

    @model_validator(mode="after")
    def _every_tier_is_routed(self) -> Self:
        missing = sorted(tier.value for tier in Tier if tier not in self.routing_rules)
        if missing:
            raise ValueError(
                f"tenant '{self.tenant_id}': no routing rule for {', '.join(missing)} — "
                "every tier must say what happens to a lead that lands in it"
            )
        return self

    @classmethod
    def from_dict(cls, document: object) -> Self:
        """Validate a config document, failing with the tenant's name in the message.

        Use this at every load boundary — the JSON file today, the ``jsonb`` column in
        P5.1. ``model_validate`` raises a perfectly good ``ValidationError``, but it does
        not know which of a hundred tenants produced it, and that is the first thing an
        operator needs at 3am.
        """
        if not isinstance(document, Mapping):
            raise TenantConfigError(
                f"tenant config must be a JSON object, got {type(document).__name__}"
            )
        raw_id = document.get("tenant_id")
        label = raw_id if isinstance(raw_id, str) and raw_id else "unknown"
        try:
            return cls.model_validate(dict(document))
        except ValidationError as exc:
            raise TenantConfigError(f"tenant '{label}': invalid configuration — {exc}") from exc

    # ------------------------------------------------------------------- accessors

    @property
    def max_weighted_raw(self) -> float:
        """The largest weighted raw score this tenant's weights can produce.

        The denominator #9's ``weighted_total`` normalises by:
        ``MAX_TOTAL_SCORE * raw / cfg.max_weighted_raw``. Exposed as data rather than
        applied here so that all scoring arithmetic — and its rounding decision — stays in
        one module.
        """
        return sum(weight * DIMENSION_MAXIMA[name] for name, weight in self.weights.items())

    def weight_for(self, dimension: str) -> float:
        """This tenant's multiplier for one dimension."""
        try:
            return self.weights[dimension]
        except KeyError:
            raise KeyError(
                f"tenant '{self.tenant_id}': no weight for dimension '{dimension}'"
            ) from None

    def tier_for(self, total: float) -> Tier:
        """The tier a normalised total falls in. Pure; lower bounds are inclusive."""
        if not math.isfinite(total) or not 0.0 <= total <= MAX_TOTAL_SCORE:
            raise ValueError(
                f"tenant '{self.tenant_id}': total score {total} is off the "
                f"0-{MAX_TOTAL_SCORE:g} scale; normalise before asking for a tier"
            )
        return self.thresholds.tier_for(total)

    def rule_for(self, tier: Tier) -> RoutingRule:
        """The routing rule for a tier. Total, because validation proved every tier has one."""
        return self.routing_rules[tier]

    def action_for(self, tier: Tier) -> Action:
        """What this tenant does with a lead in the given tier."""
        return self.routing_rules[tier].action

    def destination_for(self, tier: Tier) -> str | None:
        """Where this tenant sends a lead in the given tier; ``None`` for suppression."""
        return self.routing_rules[tier].destination

    # ----------------------------------------------------------------- prompt block

    def icp_block(self) -> str:
        """The tenant block for the system prompt — byte-stable for a given config.

        This string is the tail of the cacheable prompt prefix (#11), so it must be a pure
        function of the config's *values*: dimensions are emitted in sorted order, weights
        in a fixed two-decimal format, and the tenant text is normalised to ``\\n`` line
        endings with trailing whitespace stripped. Nothing here is derived from clock,
        environment or dict insertion order.

        It carries only what the model needs to *judge*: who the customer sells to and
        which dimensions that customer cares about most. Thresholds, ``min_confidence`` and
        the routing table are deliberately absent — invariant 2 says the model assesses and
        code routes, and a model that can see the hot threshold will start aiming for it.
        """
        weight_lines = [f"- {name}: {self.weights[name]:.2f}" for name in sorted(self.weights)]
        parts = [
            f'<tenant_profile version="{self.prompt_version}">',
            f"You are qualifying inbound leads for {_normalise_text(self.name)}.",
            "",
            "Ideal customer profile:",
            _normalise_text(self.icp_description),
            "",
            "Relative emphasis for this customer (higher means it matters more):",
            *weight_lines,
            "</tenant_profile>",
        ]
        return "\n".join(parts)


def _normalise_text(text: str) -> str:
    """Collapse a free-text field to a stable byte sequence.

    CRLF becomes LF, trailing whitespace goes from every line, and leading and trailing
    blank lines go entirely. Without this, saving ``tenants/default.json`` from a Windows
    editor would change the prompt prefix and silently halve the cache hit rate.
    """
    # NFC first: the same visible text in decomposed form is a different byte string,
    # which is an operator-invisible way to lose the prompt cache.
    normalised = unicodedata.normalize("NFC", text)
    lines = normalised.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_PROMPT_VERSION",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_WEIGHTS",
    "DIMENSION_MAXIMA",
    "DIMENSION_NAMES",
    "FORBIDDEN_TEXT_FRAGMENTS",
    "PROMPT_VERSION_PATTERN",
    "TENANT_ID_PATTERN",
    "RoutingRule",
    "TenantConfig",
    "TenantConfigError",
    "TenantNotFoundError",
    "TierThresholds",
]
