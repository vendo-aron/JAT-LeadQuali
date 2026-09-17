"""Read, validate and describe ``golden_leads.jsonl`` — the labeled-lead corpus.

**The seed cases in the committed file are synthetic. Eval numbers computed against a
synthetic set measure self-consistency, not correctness.** That sentence is the reason this
module exists in the shape it does, and it is repeated in the data file's own header, in
:func:`describe_golden_set`'s output and in ``docs/labeling-golden-set.md``, because a
number with a caveat attached in only one place is a number that will eventually be quoted
without it.

Why there is a seed at all rather than fifty cases
--------------------------------------------------

``docs/decisions/0001-open-product-questions.md`` records the answer to #2 question 1:
**there is no historical lead data with outcomes.** There is therefore nothing to label.
Writing fifty invented leads and calling the result a golden set would produce confident
accuracy figures that measure only whether the model agrees with whoever wrote the leads —
and a rubric that scores 94% against its own author's imagination is exactly how a team
ships a broken rubric believing it works. So what ships here is the machinery, the process
(``docs/labeling-golden-set.md``) and a deliberately small seed whose every case is marked
``synthetic``. The set becomes real one promoted ``feedback`` row at a time.

The provenance field is the load-bearing part. It is required, it is validated, and
:class:`GoldenSet` counts the two kinds separately so no report can quietly average them
together.

Why the format is JSONL with a header record
--------------------------------------------

Line-oriented JSON is append-only in the way a git history likes: promoting a case is one
added line, and two people promoting cases in the same week do not conflict. It is also
readable by ``jq``, a notebook or another language without importing this package — the
same reasoning ``tests/fixtures/injection_corpus.json`` was written under, and the reason
injection cases are *referenced* here by id instead of copied.

The first record is metadata rather than a case: it carries the schema version and the
thresholds the file holds itself to (:class:`GoldenSetHeader`). Expressing "we expect real
cases by now" as a counter in the data — rather than as a date in the code — means the
check cannot rot, and raising the counter is the deliberate, reviewable act that turns the
seed into a dataset.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from leadquali.api.schemas import LeadForm
from leadquali.domain.models import DimensionScores, ExtractedFacts, Tier
from leadquali.prompts.lead import LeadSubmission
from tests.fixtures import load_injection_corpus

#: The file plan §6 names. The most valuable file in the repository, once it is real.
GOLDEN_LEADS_PATH: Final[Path] = Path(__file__).resolve().parent / "golden_leads.jsonl"

#: Bumped only for a change that older readers would misread. Adding an *optional* case
#: key does not need a bump; changing the meaning of an existing one does.
SCHEMA_VERSION: Final[int] = 1

#: The key that marks the metadata record. ``$``-prefixed so it cannot collide with a case
#: field, following the ``$note`` convention already used by the injection corpus.
HEADER_KEY: Final[str] = "$golden_set"

#: The acceptance numbers from #22, held in the header so the gap between what the set is
#: and what it must become is machine-readable rather than folklore.
ACCEPTANCE_TARGET_TOTAL_CASES: Final[int] = 50
ACCEPTANCE_TARGET_REAL_CASES: Final[int] = 50

#: All four tiers must eventually appear, per #22's acceptance criteria.
REQUIRED_TIERS: Final[frozenset[Tier]] = frozenset(Tier)

#: Hard cases required by #22's acceptance criteria.
ACCEPTANCE_TARGET_HARD_CASES: Final[int] = 10

#: The phrase the header note must contain. Crude, and deliberately so: it makes deleting
#: the caveat a test failure rather than a tidy-up.
REQUIRED_NOTE_PHRASE: Final[str] = "self-consistency"

#: Domains reserved for documentation and testing (RFC 2606, RFC 6761). Every address in
#: the committed file must be at one of these, which is what makes "no real PII" a check
#: rather than a hope: a pasted customer address fails the suite.
RESERVED_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {"example.com", "example.org", "example.net"}
)
RESERVED_EMAIL_SUFFIXES: Final[tuple[str, ...]] = (
    ".example",
    ".invalid",
    ".test",
    ".localhost",
    ".example.com",
    ".example.org",
    ".example.net",
)

#: Consumer mailbox providers, allowed as an explicit exception. "Free provider address
#: with strong buying signals" is a lead shape the rubric has to get right, and it cannot
#: be represented by a reserved domain — the domain *is* the signal under test. The local
#: part of such an address must still be obviously fabricated; that rule lives in the
#: runbook because no regular expression can enforce it.
FREE_EMAIL_PROVIDERS: Final[frozenset[str]] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "yahoo.com",
        "yahoo.co.uk",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "web.de",
        "qq.com",
    }
)

_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]{2,63}\Z")

#: A labeler is an opaque handle, never an address or a display name — the same rule #15
#: puts on ``feedback.rater`` and invariant 5 puts on everything.
_LABELER_RE: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_-]{1,31}\Z")

#: Loose on purpose: this is a *finder*, not a validator. Anything that could be read as an
#: address by a human skimming the diff should be caught and made to justify itself.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)

#: The handle every synthetic seed case is labeled with. Naming it in the data is the
#: point: these labels were written by the same person who invented the leads, which is
#: exactly why numbers computed against them measure self-consistency. A *real* case may
#: never carry this handle — see :func:`_parse_labels`.
SEED_LABELER: Final[str] = "seed_author"

#: Shortest justification accepted. A tier with "warm" as its reason is unreviewable six
#: months later, which is precisely when someone will need to re-read it.
MIN_NOTES_CHARS: Final[int] = 20

_CASE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "case_id",
        "provenance",
        "expected_tier",
        "labels",
        "form",
        "injection_case_id",
        "expected_min_tier",
        "expected_max_tier",
        "expect_escalation",
        "expected_dimension_ranges",
        "expected_extracted",
        "hard_case",
        "promoted_from",
        "tags",
    }
)

_LABEL_KEYS: Final[frozenset[str]] = frozenset({"labeler", "tier", "labeled_at", "notes"})

_HEADER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "note",
        "min_total_cases",
        "min_real_cases",
        "acceptance_target_total_cases",
        "acceptance_target_real_cases",
    }
)


class GoldenSetError(ValueError):
    """The golden set is malformed, unlabelled, or below a threshold it declared.

    Always raised, never warned. A corpus that loads with half its cases dropped is worse
    than one that refuses to load: the first silently moves every number computed from it,
    and nobody re-reads a warning printed during a green test run.
    """


class Provenance(StrEnum):
    """Where a case came from — the difference between evidence and illustration."""

    SYNTHETIC = "synthetic"
    """Written by hand to exercise a lead shape. Proves the pipeline, not the rubric."""

    REAL = "real"
    """A submission that actually arrived, pseudonymised and labeled by a human."""


@dataclass(frozen=True, slots=True)
class HumanLabel:
    """One person's independent judgement about one lead."""

    labeler: str
    """Opaque handle for the person, e.g. ``icp_owner``. Never an email address."""

    tier: Tier
    """The tier this person says the lead *should* have received."""

    labeled_at: date
    """When they said it, so a label can be read against the rubric version of the day."""

    notes: str
    """Why. Free text, and the most useful column in the file when a number looks wrong."""


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One labeled lead: a payload, a human verdict, and how much to trust it."""

    case_id: str
    provenance: Provenance
    expected_tier: Tier
    """The adjudicated answer. Always one of :attr:`labels`' tiers, never a third opinion."""

    labels: tuple[HumanLabel, ...]
    form: Mapping[str, str | None]
    """The raw form payload, as :class:`~leadquali.api.schemas.LeadForm` would receive it."""

    injection_case_id: str | None = None
    """Set when the payload came from ``tests/fixtures/injection_corpus.json`` (#12)."""

    expected_min_tier: Tier | None = None
    expected_max_tier: Tier | None = None
    expect_escalation: bool = False
    """True when a human must see this lead whatever tier it lands in (invariant 3)."""

    expected_dimension_ranges: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    expected_extracted: Mapping[str, str | None] = field(default_factory=dict)
    hard_case: bool = False
    promoted_from: str | None = None
    """For a real case, where it came from — e.g. ``feedback:<lead id prefix>``."""

    tags: tuple[str, ...] = ()

    @property
    def is_synthetic(self) -> bool:
        """Whether this case is illustrative rather than evidential."""
        return self.provenance is Provenance.SYNTHETIC

    @property
    def is_real(self) -> bool:
        """Whether this case is a submission that actually arrived."""
        return self.provenance is Provenance.REAL

    @property
    def labelers(self) -> tuple[str, ...]:
        """The people who labeled this case, in the order they are recorded."""
        return tuple(label.labeler for label in self.labels)

    @property
    def labelers_agree(self) -> bool | None:
        """Whether every labeler chose the same tier; ``None`` with fewer than two.

        Aggregated over the set, this is the realistic ceiling on model accuracy: a model
        cannot beat the rate at which humans agree with each other about the answer.
        """
        if len(self.labels) < 2:
            return None
        return len({label.tier for label in self.labels}) == 1

    @property
    def lower_bound(self) -> Tier:
        """The worst tier this lead may be given without the eval calling it a miss."""
        return self.expected_min_tier or self.expected_tier

    @property
    def upper_bound(self) -> Tier:
        """The best tier this lead may be given without the eval calling it a miss."""
        return self.expected_max_tier or self.expected_tier

    def allows_tier(self, tier: Tier) -> bool:
        """Whether ``tier`` is acceptable for this case.

        Exact-match accuracy is the headline number, but it is the wrong question for a
        sparse lead where a human would accept either ``cold`` or ``warm`` and only
        ``disqualified`` is actually wrong. Recording bounds lets #23 report both without
        pretending to a precision the labels do not have.
        """
        return self.lower_bound.rank <= tier.rank <= self.upper_bound.rank

    def to_lead_form(self) -> LeadForm:
        """The payload as the ingest endpoint (#17) would validate it."""
        return LeadForm.model_validate(dict(self.form))

    def to_submission(self) -> LeadSubmission:
        """The payload as the renderer (#12) consumes it."""
        return self.to_lead_form().to_submission()


@dataclass(frozen=True, slots=True)
class GoldenSetHeader:
    """The thresholds the file holds itself to, declared in the file itself."""

    schema_version: int
    note: str
    min_total_cases: int
    """Floor on total cases. A ratchet against accidental deletion, not an ambition."""

    min_real_cases: int
    """Floor on non-synthetic cases. ``0`` today because none exist; raising it is how the
    owner declares that collection has started, and the loader then fails until it has."""

    acceptance_target_total_cases: int
    acceptance_target_real_cases: int


@dataclass(frozen=True, slots=True)
class GoldenSet:
    """A loaded, validated corpus, plus the counts nobody may collapse into one number."""

    header: GoldenSetHeader
    cases: tuple[GoldenCase, ...]
    path: Path

    @property
    def synthetic_cases(self) -> tuple[GoldenCase, ...]:
        """Every hand-written case."""
        return tuple(case for case in self.cases if case.is_synthetic)

    @property
    def real_cases(self) -> tuple[GoldenCase, ...]:
        """Every case that came from a submission that actually arrived."""
        return tuple(case for case in self.cases if case.is_real)

    @property
    def synthetic_count(self) -> int:
        """How many cases are illustrative."""
        return len(self.synthetic_cases)

    @property
    def real_count(self) -> int:
        """How many cases are evidential. The only number that makes the set worth trusting."""
        return len(self.real_cases)

    @property
    def hard_cases(self) -> tuple[GoldenCase, ...]:
        """Cases a human flagged as genuinely difficult."""
        return tuple(case for case in self.cases if case.hard_case)

    @property
    def injection_cases(self) -> tuple[GoldenCase, ...]:
        """Cases whose payload is an attack from #12's corpus."""
        return tuple(case for case in self.cases if case.injection_case_id is not None)

    @property
    def tiers_covered(self) -> frozenset[Tier]:
        """Which tiers have at least one labeled case."""
        return frozenset(case.expected_tier for case in self.cases)

    @property
    def dual_labeled_cases(self) -> tuple[GoldenCase, ...]:
        """Cases two or more people labeled independently."""
        return tuple(case for case in self.cases if len(case.labels) >= 2)

    @property
    def inter_labeler_agreement(self) -> float | None:
        """Fraction of dual-labeled cases where every labeler chose the same tier.

        ``None`` when nobody has double-labeled anything yet, which is a different and more
        honest answer than ``1.0``.
        """
        overlap = self.dual_labeled_cases
        if not overlap:
            return None
        return sum(1 for case in overlap if case.labelers_agree) / len(overlap)

    @property
    def acceptance_gaps(self) -> tuple[str, ...]:
        """What still stands between this file and #22's acceptance criteria.

        Reported, never enforced: enforcing them would mean the suite is red from the day
        this lands until the day the owner finishes labeling, which trains everyone to
        ignore it.
        """
        gaps: list[str] = []
        if len(self.cases) < self.header.acceptance_target_total_cases:
            gaps.append(
                f"{len(self.cases)}/{self.header.acceptance_target_total_cases} labeled cases"
            )
        if self.real_count < self.header.acceptance_target_real_cases:
            gaps.append(
                f"{self.real_count}/{self.header.acceptance_target_real_cases} real "
                "(non-synthetic) cases"
            )
        missing = REQUIRED_TIERS - self.tiers_covered
        if missing:
            gaps.append("tiers unrepresented: " + ", ".join(sorted(tier.value for tier in missing)))
        if len(self.hard_cases) < ACCEPTANCE_TARGET_HARD_CASES:
            gaps.append(f"{len(self.hard_cases)}/{ACCEPTANCE_TARGET_HARD_CASES} hard cases")
        if self.inter_labeler_agreement is None:
            gaps.append("inter-labeler agreement unmeasured: no case has two labels")
        return tuple(gaps)

    @property
    def meets_acceptance_criteria(self) -> bool:
        """Whether #22 can be called done. False until real leads have been labeled."""
        return not self.acceptance_gaps

    def counts(self) -> Mapping[str, int]:
        """Every count a report should quote, so none has to be recomputed by hand."""
        return {
            "cases": len(self.cases),
            "synthetic": self.synthetic_count,
            "real": self.real_count,
            "hard": len(self.hard_cases),
            "injection": len(self.injection_cases),
            "dual_labeled": len(self.dual_labeled_cases),
        }

    def by_id(self, case_id: str) -> GoldenCase:
        """One case by id, for a test or a notebook that wants a specific shape."""
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise GoldenSetError(f"{self.path}: no case with id {case_id!r}")


def describe_golden_set(golden: GoldenSet) -> str:
    """A one-paragraph summary that always states the synthetic/real split.

    Every consumer that reports a number is expected to print this next to it. The split is
    not a footnote: with zero real cases, tier accuracy is a measure of how consistently
    the model reproduces the opinions of whoever wrote the seed.
    """
    counts = golden.counts()
    agreement = golden.inter_labeler_agreement
    agreement_text = "unmeasured" if agreement is None else f"{agreement:.0%}"
    lines = [
        f"{counts['cases']} cases: {counts['synthetic']} synthetic, {counts['real']} real "
        f"({counts['hard']} hard, {counts['injection']} injection).",
        f"Tiers covered: {', '.join(sorted(tier.value for tier in golden.tiers_covered))}.",
        f"Inter-labeler agreement over {counts['dual_labeled']} double-labeled cases: "
        f"{agreement_text}.",
    ]
    if golden.real_count == 0:
        lines.append(
            "Every case is synthetic: eval numbers computed against this set measure "
            "self-consistency, not correctness."
        )
    else:
        lines.append(
            f"{counts['synthetic']} of {counts['cases']} cases are synthetic; to that extent "
            "these numbers measure self-consistency, not correctness."
        )
    if golden.acceptance_gaps:
        lines.append("Outstanding for #22: " + "; ".join(golden.acceptance_gaps) + ".")
    return " ".join(lines)


def parse_golden_set(text: str, *, path: Path | None = None) -> GoldenSet:
    """Parse and validate a golden-set document. Raises :class:`GoldenSetError`.

    Separate from :func:`load_golden_set` so the validator can be exercised against
    hand-built documents without writing files, and so a promoted case can be checked
    before it is appended.
    """
    where = path or Path("<string>")
    header: GoldenSetHeader | None = None
    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoldenSetError(f"{where}: line {number} is not valid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise GoldenSetError(
                f"{where}: line {number} must be a JSON object, got {type(record).__name__}"
            )
        if HEADER_KEY in record:
            if header is not None:
                raise GoldenSetError(
                    f"{where}: line {number} is a second {HEADER_KEY} record; there is exactly one"
                )
            if cases:
                raise GoldenSetError(
                    f"{where}: line {number} has the {HEADER_KEY} record after the first case; "
                    "it must be the first record in the file"
                )
            header = _parse_header(record[HEADER_KEY], where, number)
            continue
        if header is None:
            raise GoldenSetError(
                f"{where}: line {number} is a case but the file has no {HEADER_KEY} header record; "
                "the header declares the schema version and the thresholds the set must meet"
            )
        case = _parse_case(record, where, number)
        if case.case_id in seen_ids:
            raise GoldenSetError(
                f"{where}: line {number} repeats case_id {case.case_id!r}; ids are how a case is "
                "referred to in a review and must be unique"
            )
        seen_ids.add(case.case_id)
        cases.append(case)

    if header is None:
        raise GoldenSetError(
            f"{where}: no {HEADER_KEY} header record found; an empty or headerless golden set "
            "cannot state what it is"
        )

    golden = GoldenSet(header=header, cases=tuple(cases), path=where)
    _check_thresholds(golden)
    return golden


def load_golden_set(path: Path | None = None) -> GoldenSet:
    """Load and validate the golden set, from ``path`` or the committed file.

    The committed file's result is cached — every consumer wants the same immutable value,
    and one referenced injection case expands to ~1.5 MB. An explicit ``path`` is *not*
    cached, so a caller validating a file it is in the middle of editing sees its edits.
    """
    if path is None:
        return _load_committed_golden_set()
    return _read_and_parse(path)


@cache
def _load_committed_golden_set() -> GoldenSet:
    return _read_and_parse(GOLDEN_LEADS_PATH)


def _read_and_parse(target: Path) -> GoldenSet:
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise GoldenSetError(f"cannot read the golden set at {target}: {exc}") from exc
    return parse_golden_set(text, path=target)


# --------------------------------------------------------------------------------------
# Parsing helpers. Every one of them raises rather than defaults.
# --------------------------------------------------------------------------------------


def _parse_header(raw: object, where: Path, number: int) -> GoldenSetHeader:
    at = f"{where}: line {number}"
    if not isinstance(raw, dict):
        raise GoldenSetError(f"{at}: {HEADER_KEY} must be an object")
    body: dict[str, Any] = raw
    _reject_unknown_keys(body, _HEADER_KEYS, at, HEADER_KEY)

    version = _require_int(body, "schema_version", at, minimum=1)
    if version != SCHEMA_VERSION:
        raise GoldenSetError(
            f"{at}: schema_version {version} is not the version this loader understands "
            f"({SCHEMA_VERSION})"
        )
    note = _require_str(body, "note", at, minimum=1)
    if REQUIRED_NOTE_PHRASE not in note:
        raise GoldenSetError(
            f"{at}: the {HEADER_KEY} note must contain {REQUIRED_NOTE_PHRASE!r}. Anyone reading "
            "this file has to be told, in the file, that numbers computed against synthetic "
            "cases measure self-consistency, not correctness"
        )

    header = GoldenSetHeader(
        schema_version=version,
        note=note,
        min_total_cases=_require_int(body, "min_total_cases", at, minimum=0),
        min_real_cases=_require_int(body, "min_real_cases", at, minimum=0),
        acceptance_target_total_cases=_require_int(
            body, "acceptance_target_total_cases", at, minimum=0
        ),
        acceptance_target_real_cases=_require_int(
            body, "acceptance_target_real_cases", at, minimum=0
        ),
    )
    if header.acceptance_target_total_cases < ACCEPTANCE_TARGET_TOTAL_CASES:
        raise GoldenSetError(
            f"{at}: acceptance_target_total_cases is {header.acceptance_target_total_cases}, "
            f"below the {ACCEPTANCE_TARGET_TOTAL_CASES} #22 asks for. Lowering the target is a "
            "product decision and belongs in a decision record, not in this file"
        )
    if header.acceptance_target_real_cases < ACCEPTANCE_TARGET_REAL_CASES:
        raise GoldenSetError(
            f"{at}: acceptance_target_real_cases is {header.acceptance_target_real_cases}, below "
            f"the {ACCEPTANCE_TARGET_REAL_CASES} #22 asks for"
        )
    return header


def _parse_case(record: Mapping[str, Any], where: Path, number: int) -> GoldenCase:
    at = f"{where}: line {number}"
    _reject_unknown_keys(record, _CASE_KEYS, at, "case")

    case_id = _require_str(record, "case_id", at, minimum=3)
    if not _SLUG_RE.match(case_id):
        raise GoldenSetError(
            f"{at}: case_id {case_id!r} must be a lowercase slug (letters, digits, underscores); "
            "it is quoted in reviews and used as a test id"
        )
    at = f"{where}: line {number} (case {case_id})"

    provenance = _require_provenance(record, "provenance", at)
    promoted_from = _optional_str(record, "promoted_from", at)
    if provenance is Provenance.REAL and promoted_from is None:
        raise GoldenSetError(
            f"{at}: a case marked 'real' must record promoted_from — which feedback row or export "
            "it came from. An unattributable 'real' case is an unfalsifiable claim about the data"
        )
    if provenance is Provenance.SYNTHETIC and promoted_from is not None:
        raise GoldenSetError(
            f"{at}: promoted_from is set on a case marked 'synthetic'. A hand-written case has no "
            "origin; if this lead really did arrive, mark it 'real'"
        )

    labels = _parse_labels(record, at)
    if provenance is Provenance.REAL and all(label.labeler == SEED_LABELER for label in labels):
        raise GoldenSetError(
            f"{at}: a case marked 'real' is labeled only by {SEED_LABELER!r}, the handle the "
            "synthetic seed was written under. A promoted lead needs a human's own judgement "
            "recorded against their own handle, or the set has grown in size without growing "
            "in evidence"
        )
    expected_tier = _require_tier(record, "expected_tier", at)
    chosen = {label.tier for label in labels}
    if expected_tier not in chosen:
        raise GoldenSetError(
            f"{at}: expected_tier {expected_tier.value!r} is not a tier any labeler chose "
            f"({', '.join(sorted(tier.value for tier in chosen))}). Adjudicating a disagreement "
            "means picking one of the human answers, not inventing a third"
        )

    form, injection_case_id = _parse_payload(record, at)
    min_tier = _optional_tier(record, "expected_min_tier", at)
    max_tier = _optional_tier(record, "expected_max_tier", at)
    if injection_case_id is not None:
        max_tier = _tighten_against_corpus(injection_case_id, max_tier, expected_tier, at)
    if min_tier is not None and max_tier is not None and min_tier.rank > max_tier.rank:
        raise GoldenSetError(
            f"{at}: expected_min_tier {min_tier.value!r} ranks above expected_max_tier "
            f"{max_tier.value!r}, so no tier could ever satisfy this case"
        )
    if min_tier is not None and expected_tier.rank < min_tier.rank:
        raise GoldenSetError(
            f"{at}: expected_min_tier {min_tier.value!r} excludes the expected_tier "
            f"{expected_tier.value!r}"
        )
    if max_tier is not None and expected_tier.rank > max_tier.rank:
        raise GoldenSetError(
            f"{at}: expected_max_tier {max_tier.value!r} excludes the expected_tier "
            f"{expected_tier.value!r}"
        )

    return GoldenCase(
        case_id=case_id,
        provenance=provenance,
        expected_tier=expected_tier,
        labels=labels,
        form=form,
        injection_case_id=injection_case_id,
        expected_min_tier=min_tier,
        expected_max_tier=max_tier,
        expect_escalation=_optional_bool(record, "expect_escalation", at),
        expected_dimension_ranges=_parse_dimension_ranges(record, at),
        expected_extracted=_parse_extracted(record, at),
        hard_case=_optional_bool(record, "hard_case", at),
        promoted_from=promoted_from,
        tags=_parse_tags(record, at),
    )


def _parse_labels(record: Mapping[str, Any], at: str) -> tuple[HumanLabel, ...]:
    raw = record.get("labels")
    if not isinstance(raw, list) or not raw:
        raise GoldenSetError(
            f"{at}: labels must be a non-empty list. A case with no labels is not a golden case — "
            "it is a lead payload waiting for a human"
        )
    labels: list[HumanLabel] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise GoldenSetError(f"{at}: labels[{index}] must be an object")
        body: dict[str, Any] = entry
        where = f"{at}: labels[{index}]"
        _reject_unknown_keys(body, _LABEL_KEYS, where, "label")
        labeler = _require_str(body, "labeler", where, minimum=2)
        if not _LABELER_RE.match(labeler):
            raise GoldenSetError(
                f"{where}: labeler {labeler!r} must be an opaque lowercase handle, not an email "
                "address or a person's name (invariant 5, and #15's rule for feedback.rater)"
            )
        if labeler in seen:
            raise GoldenSetError(
                f"{where}: {labeler!r} already labeled this case. Two labels from one person is a "
                "revision, not independent agreement — edit the existing label instead"
            )
        seen.add(labeler)
        notes = _require_str(body, "notes", where, minimum=MIN_NOTES_CHARS)
        labels.append(
            HumanLabel(
                labeler=labeler,
                tier=_require_tier(body, "tier", where),
                labeled_at=_require_date(body, "labeled_at", where),
                notes=notes,
            )
        )
    return tuple(labels)


def _parse_payload(
    record: Mapping[str, Any], at: str
) -> tuple[Mapping[str, str | None], str | None]:
    """Resolve a case's payload from exactly one source: inline, or #12's corpus."""
    inline = record.get("form")
    injection_case_id = _optional_str(record, "injection_case_id", at)
    if (inline is None) == (injection_case_id is None):
        raise GoldenSetError(
            f"{at}: give exactly one of form (an inline payload) or injection_case_id (a "
            "reference into tests/fixtures/injection_corpus.json). Two sources for one payload "
            "means they will disagree; none means there is no lead here"
        )

    if injection_case_id is not None:
        corpus = {case.id: case for case in load_injection_corpus()}
        found = corpus.get(injection_case_id)
        if found is None:
            raise GoldenSetError(
                f"{at}: injection_case_id {injection_case_id!r} is not in the injection corpus "
                f"({len(corpus)} cases). Injection payloads are owned by #12 and referenced here, "
                "never copied — add the attack there first"
            )
        payload: dict[str, str | None] = dict(found.fields)
        payload.update(found.extra)
        return payload, injection_case_id

    if not isinstance(inline, dict) or not inline:
        raise GoldenSetError(f"{at}: form must be a non-empty object of form fields")
    form: dict[str, str | None] = {}
    for key, value in inline.items():
        if not isinstance(key, str):  # pragma: no cover — JSON keys are always strings
            raise GoldenSetError(f"{at}: form has a non-string field name")
        if value is not None and not isinstance(value, str | int | float | bool):
            raise GoldenSetError(
                f"{at}: form field {key!r} must be a single scalar value, not "
                f"{type(value).__name__} — #17's LeadForm rejects nested structure"
            )
        form[key] = None if value is None else str(value)
    try:
        LeadForm.model_validate(dict(form))
    except ValidationError as exc:
        raise GoldenSetError(
            f"{at}: form is not a payload the ingest endpoint would accept: "
            f"{exc.errors()[0].get('msg', 'invalid')}"
        ) from exc
    _reject_real_contact_details(form, at)
    return form, None


def _reject_real_contact_details(form: Mapping[str, str | None], at: str) -> None:
    """Refuse any address or website outside the reserved/allowlisted domains.

    This file lives in a git repository that may be handed to a customer, so the PII rule
    has to be mechanical rather than remembered. It catches the realistic mistake — pasting
    a genuine submission in while promoting it — and it is why promoting a feedback row is
    a *rewrite* of the payload, not a copy.
    """
    for field_name, value in form.items():
        if not value:
            continue
        for domain in _EMAIL_RE.findall(value):
            if not _is_allowed_domain(str(domain)):
                raise GoldenSetError(
                    f"{at}: form field {field_name!r} contains an email address at "
                    f"{domain!r}, which is not a reserved documentation domain. Pseudonymise it "
                    "consistently (example.com, .invalid, .test) before committing it"
                )
        if field_name == "website":
            host = _url_host(value)
            if host and not _is_allowed_domain(host):
                raise GoldenSetError(
                    f"{at}: website {value!r} points at {host!r}, which is not a reserved "
                    "documentation domain. Replace it with a pseudonym"
                )


def _is_allowed_domain(domain: str) -> bool:
    lowered = domain.lower().rstrip(".")
    if lowered in RESERVED_EMAIL_DOMAINS or lowered in FREE_EMAIL_PROVIDERS:
        return True
    return lowered.endswith(RESERVED_EMAIL_SUFFIXES)


def _url_host(value: str) -> str | None:
    remainder = value.split("://", 1)[-1].strip()
    host = remainder.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":", 1)[0]
    host = host.removeprefix("www.")
    return host or None


def _tighten_against_corpus(
    injection_case_id: str, declared: Tier | None, expected: Tier, at: str
) -> Tier:
    """Hold an injection case to the ceiling #12's corpus recorded for it.

    The corpus carries an advisory ``expected_max_tier`` per attack. Honouring it here is
    what keeps the two files from drifting: an attack whose ceiling is raised in one place
    and not the other is an attack that quietly stops being tested.
    """
    corpus = {case.id: case for case in load_injection_corpus()}
    advisory_raw = corpus[injection_case_id].expected_max_tier
    try:
        advisory = Tier(advisory_raw)
    except ValueError as exc:  # pragma: no cover — guards a corpus edit, not a golden-set one
        raise GoldenSetError(
            f"{at}: injection case {injection_case_id!r} declares expected_max_tier "
            f"{advisory_raw!r}, which is not a tier"
        ) from exc
    ceiling = advisory if declared is None else min(declared, advisory, key=lambda t: t.rank)
    if expected.rank > ceiling.rank:
        raise GoldenSetError(
            f"{at}: expected_tier {expected.value!r} exceeds the expected_max_tier "
            f"{ceiling.value!r} that the injection corpus records for {injection_case_id!r}. "
            "If the attack really should score higher, change the advisory in "
            "tests/fixtures/injection_corpus.json and say why"
        )
    return ceiling


def _parse_dimension_ranges(record: Mapping[str, Any], at: str) -> Mapping[str, tuple[int, int]]:
    raw = record.get("expected_dimension_ranges")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise GoldenSetError(f"{at}: expected_dimension_ranges must be an object")
    body: dict[str, Any] = raw
    ranges: dict[str, tuple[int, int]] = {}
    for name, bounds in body.items():
        spec = DimensionScores.model_fields.get(str(name))
        if spec is None:
            raise GoldenSetError(
                f"{at}: expected_dimension_ranges names {name!r}, which is not a rubric dimension "
                f"({', '.join(sorted(DimensionScores.model_fields))})"
            )
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise GoldenSetError(
                f"{at}: expected_dimension_ranges[{name!r}] must be a [low, high] pair"
            )
        low, high = bounds
        if not isinstance(low, int) or not isinstance(high, int) or isinstance(low, bool):
            raise GoldenSetError(
                f"{at}: expected_dimension_ranges[{name!r}] bounds must be integers"
            )
        if low > high:
            raise GoldenSetError(
                f"{at}: expected_dimension_ranges[{name!r}] is inverted: [{low}, {high}]"
            )
        cap = _dimension_cap(str(name))
        if low < 0 or high > cap:
            raise GoldenSetError(
                f"{at}: expected_dimension_ranges[{name!r}] is [{low}, {high}], outside the "
                f"0-{cap} the schema allows for that dimension — the model could never satisfy it"
            )
        ranges[str(name)] = (low, high)
    return ranges


def _dimension_cap(name: str) -> int:
    """Upper bound the domain schema puts on one dimension."""
    for metadata in DimensionScores.model_fields[name].metadata:
        upper = getattr(metadata, "le", None)
        if upper is not None:
            return int(upper)
    raise GoldenSetError(  # pragma: no cover — every dimension is bounded in #7
        f"dimension {name!r} has no upper bound in DimensionScores"
    )


def _parse_extracted(record: Mapping[str, Any], at: str) -> Mapping[str, str | None]:
    raw = record.get("expected_extracted")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise GoldenSetError(f"{at}: expected_extracted must be an object")
    body: dict[str, Any] = raw
    expected: dict[str, str | None] = {}
    for name, value in body.items():
        if str(name) not in ExtractedFacts.model_fields:
            raise GoldenSetError(
                f"{at}: expected_extracted names {name!r}, which is not a field of ExtractedFacts "
                f"({', '.join(sorted(ExtractedFacts.model_fields))})"
            )
        if value is not None and not isinstance(value, str):
            raise GoldenSetError(f"{at}: expected_extracted[{name!r}] must be a string or null")
        expected[str(name)] = value
    return expected


def _parse_tags(record: Mapping[str, Any], at: str) -> tuple[str, ...]:
    raw = record.get("tags")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise GoldenSetError(f"{at}: tags must be a list of slugs")
    tags: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not re.match(r"\A[a-z][a-z0-9_]{1,31}\Z", entry):
            raise GoldenSetError(f"{at}: tag {entry!r} must be a short lowercase slug")
        tags.append(entry)
    return tuple(tags)


def _check_thresholds(golden: GoldenSet) -> None:
    """Enforce the floors the file declared for itself.

    ``min_real_cases`` is the mechanism the brief asks for: a counter rather than a date.
    While it is ``0``, a wholly synthetic set loads and the suite is green — which is
    correct, because today no real lead exists to label. The moment the owner raises it,
    this check fails until real cases arrive, and it cannot silently expire.
    """
    header = golden.header
    if len(golden.cases) < header.min_total_cases:
        raise GoldenSetError(
            f"{golden.path}: has {len(golden.cases)} cases but declares "
            f"min_total_cases={header.min_total_cases}. Either cases were deleted, or the floor "
            "was raised before they were added"
        )
    if golden.real_count < header.min_real_cases:
        raise GoldenSetError(
            f"{golden.path}: declares min_real_cases={header.min_real_cases} but holds "
            f"{golden.real_count} real and {golden.synthetic_count} synthetic cases. A synthetic "
            "set measures self-consistency, not correctness: promote real leads from the feedback "
            "table (docs/labeling-golden-set.md) or lower the floor and say why"
        )


# --------------------------------------------------------------------------------------
# Small typed accessors. Each one raises GoldenSetError naming the field, so a validation
# failure tells a labeler which key to fix rather than showing them a traceback.
# --------------------------------------------------------------------------------------


def _reject_unknown_keys(
    body: Mapping[str, Any], allowed: Iterable[str], at: str, what: str
) -> None:
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise GoldenSetError(
            f"{at}: unknown {what} key(s) {', '.join(repr(key) for key in unknown)}. A misspelled "
            f"key would silently drop the expectation it carried; allowed: "
            f"{', '.join(sorted(allowed))}"
        )


def _require_str(body: Mapping[str, Any], key: str, at: str, *, minimum: int) -> str:
    value = body.get(key)
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise GoldenSetError(
            f"{at}: {key} must be a string of at least {minimum} characters, got {value!r}"
        )
    return value.strip()


def _optional_str(body: Mapping[str, Any], key: str, at: str) -> str | None:
    if body.get(key) is None:
        return None
    return _require_str(body, key, at, minimum=1)


def _require_int(body: Mapping[str, Any], key: str, at: str, *, minimum: int) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise GoldenSetError(f"{at}: {key} must be an integer >= {minimum}, got {value!r}")
    return value


def _optional_bool(body: Mapping[str, Any], key: str, at: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise GoldenSetError(f"{at}: {key} must be true or false, got {value!r}")
    return value


def _require_date(body: Mapping[str, Any], key: str, at: str) -> date:
    value = body.get(key)
    if not isinstance(value, str):
        raise GoldenSetError(f"{at}: {key} must be an ISO date string (YYYY-MM-DD), got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GoldenSetError(
            f"{at}: {key} must be an ISO date (YYYY-MM-DD), got {value!r}"
        ) from exc


def _require_tier(body: Mapping[str, Any], key: str, at: str) -> Tier:
    value = body.get(key)
    if not isinstance(value, str):
        raise GoldenSetError(f"{at}: {key} is required and must be a string, got {value!r}")
    try:
        return Tier(value)
    except ValueError as exc:
        allowed = ", ".join(tier.value for tier in Tier)
        raise GoldenSetError(f"{at}: {key} {value!r} is not one of: {allowed}") from exc


def _optional_tier(body: Mapping[str, Any], key: str, at: str) -> Tier | None:
    if body.get(key) is None:
        return None
    return _require_tier(body, key, at)


def _require_provenance(body: Mapping[str, Any], key: str, at: str) -> Provenance:
    value = body.get(key)
    if not isinstance(value, str):
        raise GoldenSetError(
            f"{at}: {key} is required and must be a string, got {value!r}. Every case says "
            "whether it is synthetic or real; a set that cannot tell them apart reports "
            "self-consistency as if it were correctness"
        )
    try:
        return Provenance(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in Provenance)
        raise GoldenSetError(f"{at}: {key} {value!r} is not one of: {allowed}") from exc


def golden_case_ids(cases: Sequence[GoldenCase]) -> list[str]:
    """Case ids, for use as ``pytest.mark.parametrize`` ids."""
    return [case.case_id for case in cases]


__all__ = [
    "ACCEPTANCE_TARGET_HARD_CASES",
    "ACCEPTANCE_TARGET_REAL_CASES",
    "ACCEPTANCE_TARGET_TOTAL_CASES",
    "FREE_EMAIL_PROVIDERS",
    "GOLDEN_LEADS_PATH",
    "HEADER_KEY",
    "MIN_NOTES_CHARS",
    "REQUIRED_TIERS",
    "RESERVED_EMAIL_DOMAINS",
    "RESERVED_EMAIL_SUFFIXES",
    "SCHEMA_VERSION",
    "SEED_LABELER",
    "GoldenCase",
    "GoldenSet",
    "GoldenSetError",
    "GoldenSetHeader",
    "HumanLabel",
    "Provenance",
    "describe_golden_set",
    "golden_case_ids",
    "load_golden_set",
    "parse_golden_set",
]
