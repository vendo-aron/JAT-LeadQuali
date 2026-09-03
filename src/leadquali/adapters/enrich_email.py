"""Email enrichment: what an address alone can tell you, bought without spending tokens.

The external system here is DNS. Given one submitted address this adapter establishes the
domain, whether that domain exists and can receive mail, whether it belongs to a consumer
mailbox provider or a throwaway one, whether the address is a role account rather than a
person, and whether the company the submitter typed plausibly owns the domain they wrote
from. Those are cheap, deterministic and verifiable, which is exactly what a language model
is worst at and most likely to guess about — so they are computed here and handed to it as
facts (see :mod:`leadquali.app.enrichment`) rather than left for it to infer.

**Free-mail and disposable are two different signals and are never merged.** A lead at
``gmail.com`` is a real person with a real inbox who simply has no corporate mail — a
solo founder, a consultant, half the SMB market — and scoring that as fraud loses
business. A lead at ``mailinator.com`` has chosen an address they intend to abandon,
which is a statement about their interest in being contacted. The two live in two
different data files and appear as two different values of ``email_domain_type``; a single
"suspicious address" boolean would destroy the distinction that makes either useful.

**Failure is expected, cheap and bounded.** Enrichment is an optimisation on scoring, never
a gate on it: #14's pipeline treats a missing enrichment as a normal outcome, and this
adapter's contract is that it *never raises* — :meth:`EmailEnricher.enrich` catches
everything and returns an :class:`~leadquali.app.enrichment.Enrichment` marked unavailable
with a PII-free reason. Three mechanisms keep a bad DNS day from becoming a bad revenue
day:

* **One bounded lookup per lead.** A single MX query answers both questions worth asking
  (``NXDOMAIN`` means the domain does not exist; an empty answer means it exists but
  cannot receive mail), and ``lifetime`` caps the whole query including any retry across
  nameservers. There is no retry loop here on purpose: a lead is not worth a second
  attempt, and a retry storm during an outage is how one slow dependency takes a worker
  fleet down.
* **A small in-process TTL cache**, keyed on domain. Leads arrive in bursts from one
  company — a team filling in a form after a webinar, a campaign landing page — and the
  answer for a domain does not change between two of them. 15 minutes and 1,024 domains
  are deliberately modest: the cache exists to collapse a burst, not to be a resolver.
  15 minutes is short enough that a domain fixing its MX records is picked up within one
  worker's lifetime, and long enough to cover the arrival pattern; 1,024 short strings is
  tens of kilobytes, and the bound is what stops a flood of unique junk domains from
  turning the cache into a memory leak. Both are constructor arguments.
* **A circuit breaker.** When DNS is down, per-domain caching does not help — every lead
  carries a new domain and pays the full timeout. After
  :data:`DEFAULT_FAILURE_THRESHOLD` consecutive failures the adapter stops calling the
  resolver for :data:`DEFAULT_BREAKER_COOLDOWN_SECONDS` and degrades immediately, so an
  outage costs a handful of timeouts per minute rather than one per lead. Any success
  closes it again.

**Deployment constraint for Phase 4 (#27) — a Lambda in private VPC subnets has no DNS
resolution unless the VPC provides it.** The qualification worker runs in private subnets
to reach Postgres; a Lambda placed in a VPC uses the VPC's resolver (``.2`` in the VPC
CIDR, the Route 53 Resolver) and reaches it only if ``enableDnsSupport`` and
``enableDnsHostnames`` are on and the subnet's routing and NACLs permit it. Public
resolution additionally needs a NAT gateway or a Route 53 Resolver outbound endpoint. Get
that wrong and **every** lookup times out: the breaker opens, every lead is silently
enriched with nothing, and nothing is broken enough to page anyone. The visible symptom is
``enrichment_available=False`` on every :class:`~leadquali.app.qualify.QualificationResult`
and a stream of the warning logged below — #21 should alarm on that ratio.

**Invariant 5.** The domain is logged; the local part never is, and never enters the
prompt facts either. An address is PII, a domain is a company.

The two domain lists are data, not code: ``data/disposable_email_domains.txt`` and
``data/free_mail_domains.txt``, both documenting their source and update procedure in
their own headers, both overridable per instance so a deployment can mount a fresher list
without a release.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from email.utils import parseaddr
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import dns.resolver

from leadquali.app.enrichment import Enrichment
from leadquali.prompts.lead import LeadSubmission

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- tunables

#: Hard cap on one MX query, in seconds, retries included. Two seconds is generous for a
#: resolver in the same VPC and short enough that the breaker's threshold of them is still
#: a fraction of one lead's model call.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 2.0

#: How long a domain's MX answer is reused. See the module docstring for why 15 minutes.
DEFAULT_CACHE_TTL_SECONDS: Final[float] = 900.0

#: How many domains the cache holds before evicting the least recently used one.
DEFAULT_CACHE_MAX_DOMAINS: Final[int] = 1024

#: Consecutive resolver failures before the adapter stops trying for a cooldown.
DEFAULT_FAILURE_THRESHOLD: Final[int] = 5

#: How long the breaker stays open. One minute: long enough to stop the bleeding, short
#: enough that a blip does not cost a quarter of an hour of enrichment.
DEFAULT_BREAKER_COOLDOWN_SECONDS: Final[float] = 60.0

# ------------------------------------------------------------------------- fact keys

#: The registered domain of the address, e.g. ``acme.com``. Never the whole address.
FACT_DOMAIN: Final[str] = "email_domain"

#: One of :class:`DomainType`.
FACT_DOMAIN_TYPE: Final[str] = "email_domain_type"

#: One of :class:`AddressType`.
FACT_ADDRESS_TYPE: Final[str] = "email_address_type"

#: :data:`YES` / :data:`NO` / :data:`UNKNOWN` — whether the domain exists in DNS at all.
FACT_DOMAIN_RESOLVES: Final[str] = "email_domain_resolves"

#: :data:`YES` / :data:`NO` / :data:`UNKNOWN` — whether it can actually receive mail.
FACT_DOMAIN_HAS_MX: Final[str] = "email_domain_has_mx"

#: :data:`YES` / :data:`NO` / :data:`NOT_CHECKED` / :data:`NOT_APPLICABLE`.
FACT_COMPANY_MATCH: Final[str] = "company_matches_email_domain"

YES: Final[str] = "yes"
NO: Final[str] = "no"

#: The check was attempted and could not be completed. Distinct from :data:`NOT_CHECKED`.
UNKNOWN: Final[str] = "unknown"

#: Nothing to check — the submission gave no company name.
NOT_CHECKED: Final[str] = "not_checked"

#: The check is meaningless here: nobody's company owns ``gmail.com``.
NOT_APPLICABLE: Final[str] = "not_applicable"

#: Reasons, all operator-facing and free of PII (invariant 5).
NO_ADDRESS_REASON: Final[str] = "no email address in the submission"
MALFORMED_REASON: Final[str] = "the submitted email address is not a usable address"
BREAKER_OPEN_REASON: Final[str] = "dns lookups paused after repeated failures"


class DomainType(StrEnum):
    """What kind of mail domain the address is on.

    :attr:`CORPORATE` is a claim about the lists, not about DNS: it means "not a known
    consumer or throwaway provider", i.e. an address on somebody's own domain. Whether
    that domain actually exists is a separate fact (:data:`FACT_DOMAIN_RESOLVES`), kept
    separate so the model can tell "own domain, receives mail" from "own domain, does not
    exist" — which are opposite signals.
    """

    CORPORATE = "corporate"
    FREE_MAIL = "free_mail"
    DISPOSABLE = "disposable"


class AddressType(StrEnum):
    """Whether the address reaches a person or a shared inbox.

    A role account is not a bad lead — ``info@`` is how a great many small companies make
    first contact — but it is weak evidence for the ``authority`` dimension, because
    nobody has told you who they are.
    """

    ROLE_ACCOUNT = "role_account"
    PERSONAL = "personal"


class MxResult(StrEnum):
    """The three answers one MX query can give that are *facts* rather than failures.

    Anything else — a timeout, SERVFAIL, an unreachable resolver — is a failure and is
    raised by an :class:`MxLookup`, because reporting "no MX records" for a domain nobody
    managed to ask about would be a false statement in a block the model is told to trust.
    """

    HAS_MX = "has_mx"
    NO_MX_RECORDS = "no_mx_records"
    NO_SUCH_DOMAIN = "no_such_domain"


@runtime_checkable
class MxLookup(Protocol):
    """The DNS seam, narrow enough that a test can implement it in four lines.

    It is deliberately *not* a port in :mod:`leadquali.app.ports`: the application layer
    has no business knowing that enrichment involves MX records at all. It exists so the
    suite runs offline, and so a deployment that resolves through an internal service
    rather than DNS can substitute one class.
    """

    def lookup_mx(self, domain: str) -> MxResult:
        """Return what DNS says about ``domain``'s mail exchangers.

        Raises:
            Exception: any failure to *obtain* an answer — timeout, SERVFAIL, no reachable
                nameserver. The caller degrades; it never reports a guess as a fact.
        """
        ...


class DomainListError(RuntimeError):
    """A configured domain list could not be read.

    Raised at construction, not per lead, and deliberately not degraded: an empty
    disposable list silently classifies every throwaway address as corporate, which is a
    scoring bug nobody would ever notice. Failing here fails the deploy, in front of
    whoever is doing the deploying.
    """


def default_data_dir() -> Path:
    """Directory holding the bundled domain lists, next to this module."""
    return Path(__file__).resolve().parent / "data"


def default_disposable_domains_path() -> Path:
    """The bundled disposable/throwaway domain list."""
    return default_data_dir() / "disposable_email_domains.txt"


def default_free_mail_domains_path() -> Path:
    """The bundled consumer ("free-mail") provider list."""
    return default_data_dir() / "free_mail_domains.txt"


def load_domain_list(path: Path | str) -> frozenset[str]:
    """Read one domain-per-line list, ignoring blank lines and ``#`` comments.

    Entries are lowercased and stripped of a leading ``@`` and a trailing dot, so a list
    pasted from another tool works without editing.
    """
    file = Path(path)
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise DomainListError(f"cannot read the domain list at {file}: {exc}") from exc
    domains = {
        entry
        for line in raw.splitlines()
        if (entry := line.split("#", 1)[0].strip().lower().lstrip("@").rstrip("."))
    }
    if not domains:
        raise DomainListError(f"the domain list at {file} is empty")
    return frozenset(domains)


class DnsPythonMxLookup:
    """The real :class:`MxLookup`, backed by ``dnspython``.

    ``dns`` is imported here and nowhere else in the package, for the same reason
    ``anthropic`` lives only in the LLM adapter.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        resolver: dns.resolver.Resolver | None = None,
    ) -> None:
        """Build a lookup.

        Args:
            timeout_seconds: hard cap on one query, passed as ``lifetime`` so it bounds
                the whole attempt rather than each individual UDP send.
            resolver: an already-configured resolver. Defaults to the system one, read
                from ``/etc/resolv.conf`` at construction — which in a VPC Lambda is the
                VPC resolver. See the module docstring's Phase 4 note.
        """
        self._timeout_seconds = timeout_seconds
        self._resolver = resolver if resolver is not None else dns.resolver.Resolver()
        self._resolver.timeout = timeout_seconds
        self._resolver.lifetime = timeout_seconds

    def lookup_mx(self, domain: str) -> MxResult:
        """Ask for ``domain``'s MX records once, within the configured lifetime."""
        try:
            answer = self._resolver.resolve(domain, "MX", lifetime=self._timeout_seconds)
        except dns.resolver.NXDOMAIN:
            return MxResult.NO_SUCH_DOMAIN
        except dns.resolver.NoAnswer:
            return MxResult.NO_MX_RECORDS
        return MxResult.HAS_MX if len(answer) > 0 else MxResult.NO_MX_RECORDS


class EmailEnricher:
    """An :class:`~leadquali.app.ports.EnricherPort` built on one email address.

    Swap it for :class:`~leadquali.adapters.enrich_null.NullEnricher` at the entrypoint
    and the pipeline is unchanged; swap it in and every lead carries six more facts that
    the model would otherwise have had to guess.
    """

    def __init__(
        self,
        *,
        lookup: MxLookup | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        cache_max_domains: int = DEFAULT_CACHE_MAX_DOMAINS,
        disposable_domains_path: Path | str | None = None,
        free_mail_domains_path: Path | str | None = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build an enricher. Every knob has a working default; tests replace three.

        Args:
            lookup: the DNS seam. Defaults to :class:`DnsPythonMxLookup` built with
                ``timeout_seconds``; tests and offline deployments inject their own.
            timeout_seconds: hard cap on one lookup. Ignored when ``lookup`` is given,
                which owns its own timeout.
            cache_ttl_seconds: how long one domain's answer is reused.
            cache_max_domains: LRU bound on the cache.
            disposable_domains_path: override for the bundled throwaway list — a mounted
                file, refreshed by operations without a release.
            free_mail_domains_path: override for the bundled consumer-provider list.
            failure_threshold: consecutive failures that open the breaker. Must be >= 1.
            breaker_cooldown_seconds: how long it stays open.
            monotonic: the time source for the cache and the breaker. Monotonic, not wall
                time, so an NTP correction cannot make a cache entry immortal — and
                injectable so tests exercise expiry without sleeping.

        Raises:
            ValueError: a nonsensical bound (a non-positive cache size or threshold).
            DomainListError: a domain list is missing, unreadable or empty.
        """
        if cache_max_domains < 1:
            raise ValueError("cache_max_domains must be at least 1")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")

        self._lookup: MxLookup = (
            lookup if lookup is not None else DnsPythonMxLookup(timeout_seconds=timeout_seconds)
        )
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_max_domains = cache_max_domains
        self._failure_threshold = failure_threshold
        self._breaker_cooldown_seconds = breaker_cooldown_seconds
        self._monotonic = monotonic

        self.disposable_domains = load_domain_list(
            disposable_domains_path
            if disposable_domains_path is not None
            else default_disposable_domains_path()
        )
        self.free_mail_domains = load_domain_list(
            free_mail_domains_path
            if free_mail_domains_path is not None
            else default_free_mail_domains_path()
        )

        # One lock over the cache and the breaker. A worker may run several leads at once,
        # and it is never held across the lookup itself: blocking every lead behind one
        # slow query is the failure this adapter exists to avoid.
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, MxResult]] = OrderedDict()
        self._failures = 0
        self._opened_at: float | None = None

    # ------------------------------------------------------------------ introspection

    @property
    def cached_domains(self) -> int:
        """How many domains the cache currently holds. For tests and metrics."""
        with self._lock:
            return len(self._cache)

    @property
    def consecutive_failures(self) -> int:
        """Resolver failures since the last success. At the threshold, the breaker is open."""
        with self._lock:
            return self._failures

    # ------------------------------------------------------------------------ the port

    def enrich(self, *, tenant_id: str, submission: LeadSubmission) -> Enrichment:
        """Establish what the address says about this lead. Never raises.

        The outer catch is not defensive clutter: the alternative to a bug here being an
        unavailable enrichment is a bug here being a lost lead, and the two are not close.
        """
        del tenant_id  # No per-tenant behaviour yet; the port carries it for adapters that do.
        try:
            return self._enrich(submission)
        except Exception as exc:  # An enricher that raises is a bug; a lost lead is worse.
            logger.exception("email enrichment failed unexpectedly")
            return Enrichment.unavailable(f"enrichment error ({type(exc).__name__})")

    def _enrich(self, submission: LeadSubmission) -> Enrichment:
        parsed = _parse_address(submission.email or "")
        if parsed is None:
            if not (submission.email or "").strip():
                return Enrichment.unavailable(NO_ADDRESS_REASON)
            logger.debug("email enrichment skipped: the address is not parseable")
            return Enrichment.unavailable(MALFORMED_REASON)

        local, domain = parsed
        domain_type = self._classify_domain(domain)
        facts = {
            FACT_DOMAIN: domain,
            FACT_DOMAIN_TYPE: domain_type.value,
            FACT_ADDRESS_TYPE: _classify_address(local).value,
            FACT_COMPANY_MATCH: _company_match(submission.company, domain, domain_type),
        }

        result, reason = self._resolve(domain)
        if result is None:
            # The classification above needed no network and is still true, so it stays:
            # #14's Enrichment models exactly this partial state, and throwing away a
            # disposable-domain hit because DNS was slow would be the worst of both.
            facts[FACT_DOMAIN_RESOLVES] = UNKNOWN
            facts[FACT_DOMAIN_HAS_MX] = UNKNOWN
            return Enrichment(facts=facts, available=False, unavailable_reason=reason)

        facts[FACT_DOMAIN_RESOLVES] = NO if result is MxResult.NO_SUCH_DOMAIN else YES
        facts[FACT_DOMAIN_HAS_MX] = YES if result is MxResult.HAS_MX else NO
        return Enrichment(facts=facts)

    # --------------------------------------------------------------- lookup + caching

    def _resolve(self, domain: str) -> tuple[MxResult | None, str]:
        """One bounded MX lookup, or the reason there wasn't one.

        Returns ``(result, "")`` on success and ``(None, reason)`` on any failure. The
        reason is rendered into the prompt, so it names the exception's class and nothing
        else — never a message that might quote an address or an internal hostname.
        """
        now = self._monotonic()
        with self._lock:
            cached = self._cache_get(domain, now)
            if cached is not None:
                return cached, ""
            if self._breaker_is_open(now):
                return None, BREAKER_OPEN_REASON

        try:
            result = self._lookup.lookup_mx(domain)
        except Exception as exc:  # Every failure mode of DNS, plus the ones we forgot.
            with self._lock:
                self._record_failure(now)
            logger.debug("mx lookup failed for domain=%s (%s)", domain, type(exc).__name__)
            return None, f"dns lookup failed ({type(exc).__name__})"

        with self._lock:
            self._record_success()
            self._cache_put(domain, result, now)
        return result, ""

    def _cache_get(self, domain: str, now: float) -> MxResult | None:
        entry = self._cache.get(domain)
        if entry is None:
            return None
        expires_at, result = entry
        if now >= expires_at:
            del self._cache[domain]
            return None
        self._cache.move_to_end(domain)
        return result

    def _cache_put(self, domain: str, result: MxResult, now: float) -> None:
        # Negative answers are cached like positive ones: a burst of leads from a domain
        # that does not exist is exactly the burst worth not re-asking about.
        self._cache[domain] = (now + self._cache_ttl_seconds, result)
        self._cache.move_to_end(domain)
        while len(self._cache) > self._cache_max_domains:
            self._cache.popitem(last=False)

    def _breaker_is_open(self, now: float) -> bool:
        return (
            self._opened_at is not None and now - self._opened_at < self._breaker_cooldown_seconds
        )

    def _record_failure(self, now: float) -> None:
        self._failures += 1
        if self._failures < self._failure_threshold:
            return
        if self._opened_at is None:
            logger.warning(
                "email enrichment disabled for %.0fs after %d consecutive dns failures; "
                "leads will be assessed without enrichment. In a VPC deployment this is "
                "usually missing DNS resolution in the private subnets, not a dns outage.",
                self._breaker_cooldown_seconds,
                self._failures,
            )
        self._opened_at = now

    def _record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("email enrichment resumed: dns is answering again")
        self._failures = 0
        self._opened_at = None

    # ------------------------------------------------------------------ classification

    def _classify_domain(self, domain: str) -> DomainType:
        """Disposable wins over free-mail, which wins over "somebody's own domain".

        The precedence matters if a domain ever lands on both lists: a throwaway service
        that also offers permanent mailboxes is a throwaway service for our purposes.
        """
        if domain in self.disposable_domains:
            return DomainType.DISPOSABLE
        if domain in self.free_mail_domains:
            return DomainType.FREE_MAIL
        return DomainType.CORPORATE


# ---------------------------------------------------------------------- address parsing

#: Local parts we treat as shared inboxes rather than people. Kept in code, unlike the
#: domain lists, because it is a closed linguistic set that has not changed in twenty
#: years — whereas throwaway domains appear weekly, which is what makes those a data file.
ROLE_LOCAL_PARTS: Final[frozenset[str]] = frozenset(
    {
        "abuse",
        "accounting",
        "accounts",
        "admin",
        "administrator",
        "ask",
        "billing",
        "careers",
        "contact",
        "contactus",
        "customerservice",
        "enquiries",
        "enquiry",
        "feedback",
        "finance",
        "general",
        "hello",
        "help",
        "helpdesk",
        "hi",
        "hr",
        "info",
        "inquiries",
        "inquiry",
        "invoices",
        "it",
        "jobs",
        "legal",
        "mail",
        "marketing",
        "media",
        "newsletter",
        "noreply",
        "office",
        "orders",
        "partners",
        "partnerships",
        "postmaster",
        "press",
        "privacy",
        "purchasing",
        "recruitment",
        "sales",
        "security",
        "service",
        "support",
        "team",
        "webmaster",
        "welcome",
    }
)

#: Dropped when comparing a company name to a domain: they carry no identity.
_COMPANY_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "ab",
        "ag",
        "and",
        "as",
        "bv",
        "co",
        "company",
        "corp",
        "corporation",
        "gbr",
        "gmbh",
        "group",
        "holding",
        "holdings",
        "inc",
        "incorporated",
        "kft",
        "kk",
        "limited",
        "llc",
        "llp",
        "lp",
        "ltd",
        "nv",
        "of",
        "oy",
        "pc",
        "plc",
        "pllc",
        "pty",
        "sa",
        "sarl",
        "sas",
        "spa",
        "srl",
        "the",
        "ug",
    }
)

#: Second-level labels that are part of a public suffix rather than a name, so that
#: ``acme.co.uk`` reduces to ``acme``. A deliberate approximation: the real answer is the
#: Public Suffix List, which is a dependency and a periodic data refresh that this one
#: heuristic does not justify. Getting it wrong costs a false ``no`` on one soft signal.
_PUBLIC_SECOND_LEVEL_LABELS: Final[frozenset[str]] = frozenset(
    {"ac", "co", "com", "edu", "gov", "ltd", "me", "mil", "ne", "net", "or", "org", "plc", "sch"}
)

_LOCAL_PART_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_NON_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

_MAX_LOCAL_PART_CHARS: Final[int] = 64
_MAX_DOMAIN_CHARS: Final[int] = 253
_MIN_MATCHABLE_CHARS: Final[int] = 3


def _parse_address(raw: str) -> tuple[str, str] | None:
    """Split a submitted address into ``(local_part, domain)``, or ``None`` if it is junk.

    Tolerant where a web form is messy — surrounding whitespace, a ``Name <addr>`` form
    pasted out of a mail client, mixed case, a trailing root dot, an internationalised
    domain — and strict where a mistake would matter: exactly one ``@``, a domain of at
    least two syntactically valid labels. ``None`` means "not usable", and the caller says
    so rather than resolving something it made up.
    """
    _, address = parseaddr(raw.strip())
    if address.count("@") != 1:
        return None
    local, _, domain = address.partition("@")
    local = local.strip()
    if not local or len(local) > _MAX_LOCAL_PART_CHARS or not _LOCAL_PART_RE.match(local):
        return None

    domain = unicodedata.normalize("NFKC", domain.strip()).rstrip(".").lower()
    if not domain or len(domain) > _MAX_DOMAIN_CHARS:
        return None
    if not domain.isascii():
        try:  # An IDN is a real domain; resolve it as the punycode DNS actually holds.
            domain = domain.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None

    labels = domain.split(".")
    if len(labels) < 2 or len(labels[-1]) < 2 or labels[-1].isdigit():
        return None
    if not all(_LABEL_RE.match(label) for label in labels):
        return None
    return local, domain


def _classify_address(local: str) -> AddressType:
    """Role account or person, judged on the local part with any ``+tag`` removed."""
    base = _NON_ALNUM_RE.sub("", local.split("+", 1)[0].lower())
    return AddressType.ROLE_ACCOUNT if base in ROLE_LOCAL_PARTS else AddressType.PERSONAL


def _company_match(company: str | None, domain: str, domain_type: DomainType) -> str:
    """Whether the typed company plausibly owns the domain the lead wrote from.

    A genuine ICP-fit signal in both directions: agreement corroborates everything else
    the submission claims about itself, and disagreement is worth a human's attention. It
    is reported as a separate fact rather than folded into the domain type so the model
    can weigh it — the heuristic is a heuristic, and rebrands, acquisitions and holding
    companies all produce honest mismatches.

    On a consumer or throwaway domain the question has no content — nobody's employer owns
    ``gmail.com`` — so it is reported as not applicable rather than as a mismatch, which
    would otherwise put a false negative signal on every sole trader.
    """
    if domain_type is not DomainType.CORPORATE:
        return NOT_APPLICABLE
    tokens = _company_tokens(company or "")
    if not tokens:
        return NOT_CHECKED

    core = _domain_core(domain)
    squashed = "".join(tokens)
    if len(core) < _MIN_MATCHABLE_CHARS or len(squashed) < _MIN_MATCHABLE_CHARS:
        return NO
    if core in squashed or squashed in core:
        return YES
    initials = "".join(token[0] for token in tokens if len(token) >= 2)
    return YES if len(initials) >= 2 and initials == core else NO


def _company_tokens(company: str) -> list[str]:
    """The identity-carrying words of a company name, accents folded and suffixes dropped."""
    folded = unicodedata.normalize("NFKD", company.lower())
    ascii_only = "".join(character for character in folded if not unicodedata.combining(character))
    return [
        token
        for token in _NON_ALNUM_RE.split(ascii_only)
        if token and token not in _COMPANY_STOPWORDS
    ]


def _domain_core(domain: str) -> str:
    """The name-bearing label of a domain: ``mail.acme.co.uk`` → ``acme``."""
    labels = domain.split(".")[:-1]
    if len(labels) >= 2 and labels[-1] in _PUBLIC_SECOND_LEVEL_LABELS:
        labels = labels[:-1]
    return labels[-1] if labels else ""


__all__ = [
    "BREAKER_OPEN_REASON",
    "DEFAULT_BREAKER_COOLDOWN_SECONDS",
    "DEFAULT_CACHE_MAX_DOMAINS",
    "DEFAULT_CACHE_TTL_SECONDS",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_TIMEOUT_SECONDS",
    "FACT_ADDRESS_TYPE",
    "FACT_COMPANY_MATCH",
    "FACT_DOMAIN",
    "FACT_DOMAIN_HAS_MX",
    "FACT_DOMAIN_RESOLVES",
    "FACT_DOMAIN_TYPE",
    "MALFORMED_REASON",
    "NO",
    "NOT_APPLICABLE",
    "NOT_CHECKED",
    "NO_ADDRESS_REASON",
    "ROLE_LOCAL_PARTS",
    "UNKNOWN",
    "YES",
    "AddressType",
    "DnsPythonMxLookup",
    "DomainListError",
    "DomainType",
    "EmailEnricher",
    "MxLookup",
    "MxResult",
    "default_data_dir",
    "default_disposable_domains_path",
    "default_free_mail_domains_path",
    "load_domain_list",
]
