"""Citation gate for the v1 host agent (ADR 0008, ADR 0021).

This module is the **only** module in ``src/fin/host/`` that decides
whether the model's answer is grounded in evidence from this run's
tool calls. It runs on the path to END in the host graph (T6); it does
NOT call a model, the network, or a database.

The public API is the pure function ``verify_citations``. It returns one
of three typed outcomes:

- ``DoneResult`` — the answer contains at least one ≥``min_span_len``
  extractive span that is a contiguous substring of this run's evidence
  (a single returned slice, or two or more ADJACENT returned slices of
  the same frozen unit — see ``_match_spans``). ``citations`` is the
  deduplicated list of ``(identity, quote)`` pairs the user-visible
  citation list contains.

- ``BounceResult`` — the answer did not pass; the bounce path
  increments ``repair_count`` and returns a hidden template that T6
  injects into the model's next-turn system message. The template is
  data: CLI/HTTP rendering (T7, T9) does NOT display it.

- ``RefuseResult`` — terminal stop. Three reasons:
  ``unknown_locator`` (the answer claims a ``document_id`` not in the
  frozen corpus — refuses immediately, no repair),
  ``unsupported_database_claim`` (the answer states a figure no ledger
  row this round returned supports — also immediate, because there is
  nothing here to repair into; ADR 0091), and ``citation_repair_cap``
  (``repair_count`` has already hit the cap).

Evidence kinds are closed and there are now TWO verification rules
behind that one set (ADR 0091). A document passage verifies because a
≥``min_span_len`` span of the answer appears in text this run
received. A ``database-row`` verifies by reference to its
``record_key`` and the ``fields`` this round returned: a ledger value
is a scalar, where a 32-character window around it carries no
information, so the span rule is scoped to document kinds and the
asymmetry is deliberate. The two rules never borrow each other's
evidence.

The repair counter and the step cap are independent fuses.
``repair_count`` lives in fin state, not LangGraph
``recursion_limit``.

Citations come from the gate's matched evidence items, never from the
model's claim. This prevents model-invented ``version`` strings
(ADR 0021).

Evidence kinds are closed (spec ``thread-attachment-source-read``): only
text the ``read_source`` / ``retrieve`` legs returned, and the ledger
row the ``sql`` 档 returned, is quotable.
A generated visual observation is never evidence for a quote, and an
item carrying any other kind is a typed ``MalformedEvidenceError``, not
a silent drop. Attachment evidence additionally re-passes a trust
re-check supplied by the caller (ownership + version currency) before
it can produce a citation; the gate itself holds no store.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from fin.corpus.extracted import Locator
from fin.retrieve.source_read import ATTACHMENT_KIND

# ----- shared hidden-bounce marker (SSOT, ADR 0021) -----
#
# The marker the host graph (T6) injects into the model's next-turn
# system message when the gate bounces. CLI / HTTP rendering (T7, T9)
# must strip this marker before printing. The marker is the single
# source of truth — every consumer (``graph.py``, ``cli.py``, ``http.py``)
# imports it from here. Do NOT redefine the literal anywhere else.
HIDDEN_BOUNCE_MARKER: Final[str] = "[FIN_HIDDEN_BOUNCE]"

# Strip the marker (and the whitespace immediately around it) from any
# string before user-facing output. Used by ``cli.py`` and ``http.py``.
_HIDDEN_BOUNCE_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*" + re.escape(HIDDEN_BOUNCE_MARKER) + r"\s*"
)


def strip_hidden_bounce_marker(text: str) -> str:
    """Remove every occurrence of ``HIDDEN_BOUNCE_MARKER`` from ``text``.

    Whitespace immediately around the marker is folded in (the gate's
    template typically leaves a leading newline). The marker is the
    single source of truth — see ``HIDDEN_BOUNCE_MARKER`` above.
    """
    return _HIDDEN_BOUNCE_RE.sub("", text)


# The closed set of evidence kinds the quote channel accepts: the
# company corpus leg, the thread-attachment leg, and the business-ledger
# row the ``sql`` 档 returned. A generated visual observation — or
# anything else — is not quotable and raises at validation; it must
# never ride this channel into a citation.
COMPANY_KIND: Final[str] = "company"

# A row returned by the ``sql`` 档. It is a different shape of evidence
# from a document passage, not a passage with an unusual locator
# (ADR 0091), so it carries its own record identity and its own time
# instead of a ``Locator`` — and it verifies against those two rather
# than by a text span.
DATABASE_ROW_KIND: Final[str] = "database-row"

# How a ledger row presents: it is a record, never a page and never a
# numbered unit. The document displays stay ``page`` / ``unit`` because
# for them those words are the whole meaning.
DATABASE_ROW_DISPLAY: Final[str] = "record"

# The wire key a ledger citation's retained snapshot rides on. ONE
# spelling owned HERE and imported by every consumer of it — the shared
# run-payload shaper, the workspace projection and the CSV export — so
# the three surfaces cannot disagree about where a row's evidence lives
# (spec 8.1).
DATABASE_ROW_SNAPSHOT_KEY: Final[str] = "database_row"

QUOTABLE_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset(
    {COMPANY_KIND, ATTACHMENT_KIND, DATABASE_ROW_KIND}
)

# A numeric claim no field of any row this round returned supports is
# refused TERMINALLY and on its own reason: it is a different product
# fact from a missing locator (which is a model-invented filing id) and
# from a bounce (which is an answer that simply cited nothing yet). The
# operator copy keyed by this reason says "unsupported figure", not
# "try a more specific question".
UNSUPPORTED_DATABASE_CLAIM: Final[str] = "unsupported_database_claim"

# v1 document-id locator pattern (ADR 0006 / CONTEXT.md vocabulary).
# Matches the manifest ``document_id`` shape (e.g. ``AAPL-2022Q3``):
# 2-5 uppercase letters, a dash, four digits, then ``Q1``/``Q2``/
# ``Q3``/``Q4``. The frozen corpus exposes 12 such ids; this is the
# only shape the gate treats as a locator claim.
_LOCATOR_REGEX: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Z]{2,5}-\d{4}Q[1-4])\b"
)


# Default template the host graph injects into the model's next-turn
# system message on a bounce. Hidden from CLI/HTTP rendering (T7, T9).
# Neutral instruction language — no "error" / "failed" framing.
_HIDDEN_BOUNCE_TEMPLATE: Final[str] = (
    "your answer did not cite evidence; re-answer with at least one "
    "≥32-character span copied verbatim from a passage or unit you "
    "retrieved or read this run."
)


# An ISO date first: a ledger row's ``expiry_date`` is one value, and
# reading it as three separate numbers would let "2026" alone support a
# claim of "2026 contracts" — or refuse a claim about the month because
# its hyphens parsed as minus signs.
_DATE_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?"
)

# A number as the answer writes it. The sign is a sign only at a word
# boundary: ``-15`` is negative fifteen, but the ``-`` inside
# ``C-2291`` or ``ACME-2024Q1`` joins two alphanumerics and is not
# arithmetic.
_NUMBER_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w-])[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w-])\d+(?:\.\d+)?"
)


def _numeric_tokens(text: str) -> frozenset[str]:
    """Every number ``text`` states, normalised to its canonical form.

    Dates are one token each, ``1,000`` and ``1000`` are one value, and
    a leading ``+`` is dropped while ``-`` is kept: a ledger field of
    ``-15`` must not support a claim of ``15``.

    WHY canonicalise at all: the model writes a ledger's grouped figure
    and its punctuation differently from the wire, and a supported
    figure must not be refused over that. The canonical text is what a
    row's field is compared on.
    """
    without_dates = _DATE_TOKEN_RE.sub(" ", text)
    tokens: set[str] = set()
    for match in _DATE_TOKEN_RE.finditer(text):
        tokens.add(f"date:{match.group(0)}")
    for match in _NUMBER_TOKEN_RE.finditer(without_dates):
        raw = match.group(0).replace(",", "").lstrip("+")
        try:
            value = float(raw)
        except ValueError:  # pragma: no cover - the regex admits no such token
            continue
        tokens.add(format(value, "g"))
    return frozenset(tokens)


def _plain_text(value: Any) -> str:
    """The stable text of one ledger value.

    A returned ``30`` and a returned ``"30"`` are the same fact and must
    support the same claim, so both render as ``"30"``. A ``bool`` is an
    ``int`` subclass but is not a number this contract reads as one, so
    it renders as the text the wire actually said.
    """
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return "" if value is None else str(value)
    return repr(value)


def _pairs(value: Any) -> tuple[tuple[str, str], ...]:
    """One mapping rendered as ordered ``(name, text)`` pairs.

    Sorted by name so the row's identity and its ``supports`` check do
    not depend on the JSON object's key order: two rows describing one
    record must produce one identity, and a claim must be checked
    against the same text whichever order the adapter serialized.
    """
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        (str(key), _plain_text(value[key])) for key in sorted(value, key=str)
    )


def _escape_source_id_part(text: str) -> str:
    """Make one id part unambiguous inside the ``key=value,...`` line.

    Record-key values are ledger data and may themselves carry the id's
    delimiters (a counterparty ``A=B, Ltd``). Without escaping, two
    different records spell ONE id — ``k=a=b`` from key ``k`` value
    ``a=b`` and from key ``k=a`` value ``b`` — and the exported trail or
    a clicked chip would open the wrong row (spec 8.0: one id opens one
    record). Backslash is escaped first so a literal ``\\=`` cannot
    imitate an escaped ``=``.
    """
    return text.replace("\\", "\\\\").replace("=", "\\=").replace(",", "\\,")


def format_database_row_source_id(
    source: str, record_key: Mapping[str, Any]
) -> str:
    """The one id that opens a ledger row: the permitted view, then the
    record key that query returned.

    One spelling, read by the gate, the CSV export and the web client, so
    an exported trail and a rendered chip cannot name the same record two
    different ways. Each part carries the escaping of
    :func:`_escape_source_id_part`, so the id is reversible and distinct
    per record even when values contain ``=`` or ``,``.
    """
    return f"{source}#" + ",".join(
        f"{_escape_source_id_part(str(key))}"
        f"={_escape_source_id_part(_plain_text(value))}"
        for key, value in record_key.items()
    )


@dataclass(frozen=True)
class DatabaseRow:
    """One row of a business-ledger result set, as evidence.

    The shape of spec 8: ``source`` names the permitted view that
    answered (which makes the read scope auditable from the citation
    alone), ``record_key`` is the row's own identity, ``queried_at``
    says when the value was true, and ``fields`` are the values THIS
    ROUND returned — the model's account of them is never what verifies
    (ADR 0091, ADR 0021).

    ``summary`` is the one display line, which the model may quote but
    cannot invent: it is minted here from the returned fields.

    Frozen and hashable, because the gate dedups citations by identity
    and a row's identity is its ``(source, record_key, queried_at)``.
    """

    source: str
    record_key: tuple[tuple[str, str], ...]
    queried_at: str
    fields: tuple[tuple[str, str], ...]
    summary: str

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "DatabaseRow":
        """Build one row from its wire mapping, coercing field values to text.

        WHY coerce to text: a claim about a database value is checked as
        a string against the text the answer carries, and ``30`` must
        match ``"30"`` from a JSON body exactly as ``"30"`` does. The
        text is kept VERBATIM — it is this round's own output, not user
        input — and what must not be ambiguous is the ID built from it,
        which :func:`format_database_row_source_id` escapes for exactly
        that reason. Values with no stable text form (nested objects)
        render as their repr rather than being dropped.
        """
        return cls(
            source=_plain_text(raw.get("source")),
            record_key=_pairs(raw.get("record_key")),
            queried_at=_plain_text(raw.get("queried_at")),
            fields=_pairs(raw.get("fields")),
            summary=_plain_text(raw.get("summary")),
        )

    @property
    def source_id(self) -> str:
        """The one id string that opens this row (spec 8.0)."""
        return format_database_row_source_id(self.source, dict(self.record_key))

    @property
    def record_key_values(self) -> tuple[str, ...]:
        """The row's own key values, as the text a claim would quote."""
        return tuple(text for _name, text in self.record_key)

    @property
    def tokens(self) -> frozenset[str]:
        """Every number this row states, from its summary, its key and its fields."""
        out = _numeric_tokens(self.summary)
        for _name, text in self.record_key + self.fields:
            out |= _numeric_tokens(text)
        return out

    def supports(self, claim: str) -> bool:
        """Whether this row's returned values support ``claim``.

        A field's value supports itself, so a scalar like a notice
        period of ``30`` is citable without a 32-code-point window
        around it (ADR 0091). ``record_key`` values count too: the key
        is part of the record this round returned.

        Two ways a claim is supported, and no third. Either a NUMBER the
        claim states is a number this row returned, or the claim quotes
        the row's own ``summary`` — the display line minted here from
        those fields, which the model may repeat but cannot invent.
        A claim that is neither is the model's prose resting on nothing
        this round returned.

        NOTE the gate calls :meth:`supports_numbers`, not this method:
        it first reduces the answer to the numbers it claimed OUTSIDE
        any quoted display line, because a pasted display line is the
        row speaking and an invented figure beside it is the model
        speaking. This method is the whole-claim answer for callers
        that have a claim string rather than canonical tokens.
        """
        if self.summary and self.summary in claim:
            return True
        return self.supports_numbers(_numeric_tokens(claim))

    def supports_numbers(self, claim_tokens: frozenset[str]) -> bool:
        """Whether this row returned at least one of ``claim_tokens``.

        The numeric half of :meth:`supports` at token granularity, which
        is what the gate's per-row test needs: canonical token text
        (``5e+06``, ``date:2026-03-14``) does not round-trip back
        through :func:`_numeric_tokens`, so the gate carries the set.
        """
        return bool(self.tokens & claim_tokens)


@dataclass(frozen=True)
class EvidenceItem:
    """One evidence item the gate may match against.

    ``unit_text`` is whatever the upstream tool returned for one frozen
    unit (or, for retrieve chunks, the passage slice — both are slices
    of the committed extracted tree or stored attachment text). The
    gate matches ``≥min_span_len`` substrings of ``answer`` against
    ``unit_text`` only; it does NOT reconstruct whole units on the fly
    (the gate's evidence list already has the right granularity).

    ``kind`` is the closed quotable kind (``"company"``,
    ``ATTACHMENT_KIND``, or ``DATABASE_ROW_KIND``). Attachment items
    carry their identity in ``attachment_id`` (``locator.document_id``
    stays empty); company items carry it in ``locator.document_id``;
    database-row items carry it in ``record_key`` / ``source`` and
    leave the document slots empty. ``version_int`` is the trusted
    record's integer version an attachment item was admitted under —
    what the caller's re-check compares currency against; ``None`` for
    the other kinds. ``filename`` is display-only metadata and never
    part of the citation identity; ``display`` says how the unit is
    presented (``"page"`` for a physical PDF page, ``"unit"``
    otherwise, ``DATABASE_ROW_DISPLAY`` for a ledger row).

    A ledger row is a REQUIRED-``locator`` item: a row is never a
    document passage, so it cannot be given a ``Locator`` to satisfy the
    type, and the row's own fields are its identity. The row therefore
    rides its own ``database_row`` mapping, and ``locator`` is
    ``Locator(document_id="", version="", unit=1)`` — the same
    never-read-empty document slots an attachment item already uses,
    with ``version`` carrying the row's ``queried_at`` so the citation
    states when the value was true and ``unit`` fixed at 1 because a
    record has no ordinal.

    ``position`` / ``next_position`` are the slice's code-point offsets
    within the full frozen unit, taken from the read contract: two
    items join into one matchable text only when one run's
    ``next_position`` equals the next run's ``position``. Retrieve
    chunks keep the defaults (``0`` / ``None``) because a passage slice
    is not a unit-aligned offset and must never bridge. A database row
    keeps both defaults for the same reason plus one more: a row is a
    single record, never a slice of one, so it must never bridge.
    """

    locator: Locator
    unit_text: str
    kind: str = COMPANY_KIND
    attachment_id: str = ""
    version_int: int | None = None
    filename: str = ""
    display: str = "page"
    position: int = 0
    next_position: int | None = None
    # The one ledger row this item carries, or ``None`` for every
    # document kind. A separate field rather than a widened
    # ``locator`` so ``Locator`` keeps meaning a page of a frozen file
    # (spec 8.0) and the row keeps its own identity and its own time.
    database_row: tuple["DatabaseRow", ...] = ()


@dataclass(frozen=True)
class Citation:
    """A passed citation.

    ``kind`` / ``document_id`` / ``attachment_id`` / ``version`` /
    ``unit`` / ``display`` / ``filename`` always come from the matched
    evidence item — never from the model's claim. This prevents
    model-invented ``version`` strings (ADR 0021).

    Exactly one source id is set: ``document_id`` for a company
    citation (``attachment_id`` empty), ``attachment_id`` for an
    attachment citation (``document_id`` empty, ``filename`` the safe
    display name). ``display`` is ``"page"`` when the cited unit is
    presented as a physical page, ``"unit"`` otherwise, and
    ``DATABASE_ROW_DISPLAY`` for a ledger row — which sets NEITHER id
    slot and puts its own id on ``database_source_id``, because a row
    has no document and no attachment to name. ``version`` is the
    row's ``queried_at``, so the citation always states when the value
    was true.

    ``quote`` is the ≥``min_span_len`` substring of the answer that
    matched; the gate keeps the verbatim span so downstream rendering
    can echo it as a quoted excerpt. For a ledger row it is instead the
    claim's own text: a scalar value needs no verbatim window, and the
    span rule does not apply to this kind.
    """

    kind: str
    document_id: str
    attachment_id: str
    version: str
    unit: int
    display: str
    filename: str
    quote: str
    database_source_id: str = ""
    database_source: str = ""
    database_record_key: tuple[tuple[str, str], ...] = ()
    database_fields: tuple[tuple[str, str], ...] = ()


# ----- result types -----


@dataclass(frozen=True)
class DoneResult:
    """Gate accepted: ≥1 ≥``min_span_len`` span is grounded in evidence.

    ``citations`` is the deduplicated list of ``(identity, quote)``
    pairs (a unique evidence identity plus quote is at most one
    ``Citation``). The host graph forwards ``answer`` and ``citations``
    to the CLI/HTTP surface.
    """

    outcome: str  # always "done"
    citations: tuple[Citation, ...]
    answer: str


@dataclass(frozen=True)
class BounceResult:
    """Gate rejected: recoverable. The caller loops back to the model.

    ``repair_count`` is the **post-bounce** count — what the caller
    should pass on the next ``verify_citations`` call. ``hidden_template``
    is the fixed instruction the host graph injects into the model's
    next-turn system message; it is data, not user-facing output.
    """

    outcome: str  # always "bounce"
    repair_count: int
    hidden_template: str


@dataclass(frozen=True)
class RefuseResult:
    """Gate rejected: terminal. Run status is ``refuse``.

    ``reason`` is one of ``"unknown_locator"`` or
    ``"citation_repair_cap"``. No other values are produced (ADR 0008).
    """

    outcome: str  # always "refuse"
    reason: str


GateResult = DoneResult | BounceResult | RefuseResult


# ----- typed errors -----


class CitationGateError(Exception):
    """Base class for citation gate typed errors.

    The gate never silently swallows malformed input; it raises a
    subclass of this for each invalid call shape (defensive-contract
    axis: malformed evidence → typed error).
    """


class MalformedEvidenceError(CitationGateError):
    """An evidence item has an empty ``unit_text``, an invalid locator
    (``unit < 1`` / missing source id / empty ``version``), or a kind
    outside the closed quotable set.

    The ``index`` attribute carries the position of the offending item
    in the caller's evidence iterable (useful for run logs; matches the
    host graph's evidence-list shape).
    """

    def __init__(self, message: str, *, index: int | None = None) -> None:
        super().__init__(message)
        self.index = index


# ----- core gate -----


def verify_citations(
    answer: str,
    evidence: Iterable[EvidenceItem],
    repair_count: int,
    *,
    known_document_ids: set[str],
    repair_cap: int = 3,
    min_span_len: int = 32,
    attachment_identity_current: Callable[[EvidenceItem], bool] | None = None,
) -> GateResult:
    """Verify the model's answer is grounded in this run's evidence.

    Parameters
    ----------
    answer
        The model's final answer prose. Treated as an opaque UTF-8
        string; the gate never decodes / re-encodes it.
    evidence
        The union of this run's ``read_source`` returned slices and
        ``retrieve`` chunk texts, each carrying its locator and offsets.
        Empty evidence is allowed; the gate then falls through to the
        bounce path (or refuse, if ``repair_count`` already hit
        ``repair_cap``).
    repair_count
        The number of recoverable citation failures already
        accumulated on this run. Negative values are treated as 0
        (defensive). ``unknown_locator`` does **not** increment it;
        the bounce path does.
    known_document_ids
        The corpus manifest's document_id set. Any regex-shaped
        ``document_id`` claim in ``answer`` that is not in this set
        refuses immediately on the first failure (ADR 0008).
    repair_cap
        Hard cap on consecutive bounce-able failures. Default 3
        (ADR 0008). When ``repair_count >= repair_cap``, the gate
        returns ``RefuseResult("citation_repair_cap")`` instead of
        a bounce.
    min_span_len
        Minimum length of an extractive span, in characters. Default
        32 (ADR 0021).
    attachment_identity_current
        Trust re-check for attachment evidence, supplied by the caller
        which alone can see the trusted store: it must return True only
        when the item's ``attachment_id`` is still owned by the current
        thread and ``version_int`` still matches the record. An
        attachment item the check rejects — or any attachment item
        when no check is supplied — never yields a citation, even when
        its text matches. Company items are unaffected.

    Returns
    -------
    GateResult
        One of ``DoneResult``, ``BounceResult``, ``RefuseResult``.
        Discriminate on ``outcome`` (always ``"done"`` / ``"bounce"``
        / ``"refuse"``).

    Raises
    ------
    MalformedEvidenceError
        If any item in ``evidence`` has ``unit_text == ""``,
        ``locator.unit < 1``, an empty source id / ``version``, or a
        ``kind`` outside ``QUOTABLE_EVIDENCE_KINDS``. The gate does NOT
        silently skip malformed items — ADR 0021 forbids hidden drops.
        A ``database-row`` item is held to its own shape here instead:
        it must carry at least one row, and every row must name a
        ``source``, a ``record_key`` and a ``queried_at``.
    """
    # Repair counter is clamped to 0 (defensive: a negative value from
    # a caller bug should not accidentally cap the bounce path).
    if repair_count < 0:
        repair_count = 0

    # Validate evidence eagerly so the span-match path below never
    # sees a half-broken input. Materialize first so validation can
    # report each offending item's index in caller order.
    evidence_tuple: tuple[EvidenceItem, ...] = tuple(evidence)
    for idx, item in enumerate(evidence_tuple):
        _validate_evidence(item, index=idx)

    # Attachment trust re-check: refuse (silently, per spec: this is
    # not a malformed-input case, it is an untrusted-identity case)
    # any attachment item whose identity is stale, foreign, or
    # unverifiable before it can carry a quote.
    eligible = tuple(
        item
        for item in evidence_tuple
        if item.kind != ATTACHMENT_KIND
        or (
            attachment_identity_current is not None
            and attachment_identity_current(item)
        )
    )

    # Step 1: unknown-locator heuristic. Refuses immediately on the
    # first failure (no repair) — ADR 0008 + ADR 0021. The regex is
    # intentionally narrow (matches the manifest document_id shape
    # only); loose phrase matches would falsely flag English text
    # like "AAPL revenue" as locator claims.
    unknown_locator = _check_unknown_locator(
        answer, evidence_tuple, known_document_ids
    )
    if unknown_locator is not None:
        return RefuseResult(outcome="refuse", reason=unknown_locator)

    # Step 2: the two verification rules behind one closed set
    # (ADR 0091). A ledger row is checked against its record key and
    # the fields THIS ROUND returned; a document passage is checked by a
    # ≥``min_span_len`` span. They are deliberately different, so an
    # answer may cite either or both and neither rule borrows the
    # other's evidence: a document span can never stand in for an
    # unsupported ledger number, and a ledger number can never be
    # published without the row that returned it.
    database_rows = tuple(
        row for item in eligible for row in item.database_row
    )
    document_items = tuple(
        item for item in eligible if not item.database_row
    )

    # Step 3: EVERY number the answer states (outside the display lines
    # it quoted) must be a number some returned row states — a figure
    # no returned field supports is an unsupported claim, refused on
    # its own reason, and BEFORE any citation is published (spec §8).
    #
    # The check is per claim token, not per answer: a supported figure
    # beside a fabricated one cannot launder it. Publishing that mix
    # would attach a real citation to a false number — the exact
    # failure this rule exists to catch.
    #
    # It is not a bounce: a bounce asks the model to cite something, and
    # there is nothing here to cite, so a repair loop would only produce
    # another invented figure. It is not a missing locator: nothing
    # about a filing identity is wrong, and that copy would send the
    # operator to the corpus instead of the ledger. And it is checked
    # before the document spans so an ungrounded number can never buy
    # itself a citation with any unrelated document text.
    #
    # An answer that states no figure and quotes no row cited nothing
    # this round, which is the ordinary bounce and must keep counting
    # toward the repair cap. A run that asked no ledger at all has no
    # rows to check against, so the check cannot fire.
    #
    # The numbers are taken from the answer LESS whatever the answer
    # quotes off a row. A model that pastes the display line and then
    # invents a figure beside it has still invented the figure, and
    # counting the pasted line's own numbers as the claim's would let a
    # correct quote launder any number.
    claim_tokens = _unquoted_claim_tokens(answer, database_rows)
    unsupported_claims = _check_numeric_claims(claim_tokens, database_rows)
    if unsupported_claims:
        return RefuseResult(
            outcome="refuse", reason=UNSUPPORTED_DATABASE_CLAIM
        )

    # Step 4: the supported rows cite. A row is evidence when the answer
    # repeats the row's own display line, or states a value this round's
    # row returned — never on a document span, and never because some
    # other row happened to answer.
    database_citations = _database_citations(
        database_rows, answer, claim_tokens
    )
    span_citations = _match_spans(
        answer, document_items, min_span_len=min_span_len
    )
    citations = database_citations + span_citations

    if citations:
        return DoneResult(
            outcome="done",
            citations=citations,
            answer=answer,
        )

    # Step 5: no spans passed → bounce or refuse.
    if repair_count >= repair_cap:
        return RefuseResult(outcome="refuse", reason="citation_repair_cap")

    return BounceResult(
        outcome="bounce",
        repair_count=repair_count + 1,
        hidden_template=_HIDDEN_BOUNCE_TEMPLATE,
    )


# ----- internals -----


def _read_wire_field(source: Any, name: str) -> Any:
    """One field of a citation, read the same way off a wire dict or a
    ``Citation``/``DatabaseRow``-carrying object."""
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def database_row_snapshot(source: Any) -> dict[str, Any] | None:
    """The one assembly of the retained snapshot a ledger citation opens.

    Both readings live here so no surface rebuilds them: a row already
    stored as a wire mapping carries the spec's
    ``{source, record_key, queried_at, fields, summary}`` shape and is
    read back untouched, while a gate ``Citation`` holds its identity in
    ordered pairs and separate slots and is rebuilt into that one shape.
    Either way the snapshot names a record read THIS round, and opening
    it issues no query (spec 8.1). ``None`` means the row states no
    record — a source without a record key names a VIEW, not a row, and
    a citation that opens nothing must not be published.
    """
    raw = _read_wire_field(source, DATABASE_ROW_SNAPSHOT_KEY)
    if raw is not None:
        # A stored row is read, not rebuilt — but still held to the same
        # facts a gate-built row is.
        if not isinstance(raw, Mapping):
            return None
        if not str(raw.get("source") or "") or not raw.get("record_key"):
            return None
        return dict(raw)
    source_name = str(_read_wire_field(source, "database_source") or "")
    record_key = _read_wire_field(source, "database_record_key")
    fields = _read_wire_field(source, "database_fields")
    version = str(_read_wire_field(source, "version") or "")
    if not source_name or not record_key or not version:
        return None
    return {
        "source": source_name,
        "record_key": dict(record_key),
        "queried_at": version,
        "fields": dict(fields or ()),
        "summary": str(_read_wire_field(source, "quote") or ""),
    }


def _database_citation(row: DatabaseRow) -> Citation:
    """Render one ledger row as a ``Citation``.

    The identity comes off the row, never off the model's claim, and
    the quote is the row's OWN ``summary`` rather than a slice of the
    answer: a document citation echoes the span it matched, and for a
    row there is no such span — the retained evidence this citation
    opens is the snapshot, and the answer's wording of it is the
    model's, not the ledger's (spec 8.1).
    """
    return Citation(
        kind=DATABASE_ROW_KIND,
        # A row has no document and no attachment: naming either would
        # point the citation at a filing the ledger never read.
        document_id="",
        attachment_id="",
        version=row.queried_at,
        unit=1,
        display=DATABASE_ROW_DISPLAY,
        filename="",
        quote=row.summary,
        database_source_id=row.source_id,
        database_source=row.source,
        database_record_key=row.record_key,
        database_fields=row.fields,
    )


def _check_unknown_locator(
    answer: str,
    evidence: tuple[EvidenceItem, ...],
    known_document_ids: set[str],
) -> str | None:
    """Step 1's rule: the refuse reason for a forged filing id, or ``None``.

    A token that IS a value this round's rows returned is not a forged
    filing: a counterparty reference like ``ACME-2024Q1`` is an
    ordinary ledger value that happens to wear the filing-id shape,
    and refusing the run over one is the exact failure ADR 0091
    records. A run that asked no ledger has no such exemption, so the
    document path is byte-identical to before.
    """
    returned_text = _returned_row_text(evidence)
    for match in _LOCATOR_REGEX.finditer(answer):
        candidate = match.group(1)
        if candidate in known_document_ids or candidate in returned_text:
            continue
        return "unknown_locator"
    return None


def _unquoted_claim_tokens(
    answer: str, database_rows: tuple[DatabaseRow, ...]
) -> frozenset[str]:
    """The numbers the answer states OUTSIDE any row display line it quoted.

    A pasted display line is the row speaking; the numbers beside it are
    the model speaking, and only those are the answer's own claims.
    """
    quoted = " ".join(
        row.summary
        for row in database_rows
        if row.summary and row.summary in answer
    )
    return _numeric_tokens(answer) - _numeric_tokens(quoted)


def _check_numeric_claims(
    claim_tokens: frozenset[str],
    database_rows: tuple[DatabaseRow, ...],
) -> frozenset[str]:
    """Step 3's rule: every claimed number NO returned row states (spec §8).

    The test is per token against each row via ``DatabaseRow``'s own
    numeric half — one supported figure never covers an unsupported one,
    which is the laundering the gate exists to catch. An empty result
    on a run with no rows (or no claimed numbers) is by construction:
    there is nothing to check against, or nothing was claimed.
    """
    if not claim_tokens or not database_rows:
        return frozenset()
    return frozenset(
        token
        for token in claim_tokens
        if not any(
            row.supports_numbers(frozenset([token]))
            for row in database_rows
        )
    )


def _database_citations(
    database_rows: tuple[DatabaseRow, ...],
    answer: str,
    claim_tokens: frozenset[str],
) -> tuple[Citation, ...]:
    """Step 4: the rows that speak for themselves cite.

    A row is evidence when the answer repeats the row's own display
    line, or states a value THIS row returned — never on a document
    span, and never because some other row happened to answer.
    """
    return tuple(
        _database_citation(row)
        for row in database_rows
        if (row.summary and row.summary in answer)
        or row.supports_numbers(claim_tokens)
    )


def _returned_row_text(evidence: tuple[EvidenceItem, ...]) -> frozenset[str]:
    """Every locator-shaped string this round's ledger rows returned.

    Used only to keep the unknown-locator heuristic off the ledger's own
    values: a token the rows returned is something the run READ, so
    treating it as a model-invented filing id would refuse the run over a
    correct answer.
    """
    return frozenset(
        text
        for item in evidence
        for row in item.database_row
        for text in row.record_key_values
        if _LOCATOR_REGEX.fullmatch(text)
    )


def _validate_evidence(item: EvidenceItem, *, index: int) -> None:
    """Validate a single ``EvidenceItem``.

    Raises ``MalformedEvidenceError`` rather than silently skipping.
    The error carries the offending item's index so the host graph's
    run logs can point at the specific evidence entry that broke the
    contract.
    """
    if item.kind not in QUOTABLE_EVIDENCE_KINDS:
        raise MalformedEvidenceError(
            f"evidence[{index}].kind {item.kind!r} is not quotable; only "
            f"{sorted(QUOTABLE_EVIDENCE_KINDS)} text may enter the quote "
            f"channel",
            index=index,
        )
    if not isinstance(item.unit_text, str):
        raise MalformedEvidenceError(
            f"evidence[{index}].unit_text must be str, "
            f"got {type(item.unit_text).__name__}",
            index=index,
        )
    if item.unit_text == "":
        raise MalformedEvidenceError(
            f"evidence[{index}].unit_text is empty",
            index=index,
        )
    locator = item.locator
    if not isinstance(locator, Locator):
        raise MalformedEvidenceError(
            f"evidence[{index}].locator must be Locator, "
            f"got {type(locator).__name__}",
            index=index,
        )
    if (
        not isinstance(locator.unit, int)
        or isinstance(locator.unit, bool)
        or locator.unit < 1
    ):
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.unit must be int ≥ 1, "
            f"got {locator.unit!r}",
            index=index,
        )
    if not locator.version and item.kind != DATABASE_ROW_KIND:
        # WHY the row is exempt: a row has no frozen file, so it has no
        # manifest version — its currency is its ``queried_at``, which
        # the row's own validation below requires. Holding a row to a
        # document version would mean fabricating one, which is the
        # invented identity the whole third kind exists to avoid.
        raise MalformedEvidenceError(
            f"evidence[{index}].locator.version is empty",
            index=index,
        )
    if item.kind == ATTACHMENT_KIND:
        if not item.attachment_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].attachment_id is empty for an "
                f"attachment item",
                index=index,
            )
        if locator.document_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].locator.document_id must be empty "
                f"for an attachment item, got {locator.document_id!r}",
                index=index,
            )
    elif item.kind == DATABASE_ROW_KIND:
        # A row is held to its OWN contract, not the document one: it
        # must carry at least one row, and a row with no source, no key
        # or no time is a claim about a record the answer cannot point
        # at, so it raises here rather than verifying against nothing.
        if not item.database_row:
            raise MalformedEvidenceError(
                f"evidence[{index}].database_row is empty for a "
                f"database-row item",
                index=index,
            )
        for row_index, row in enumerate(item.database_row):
            if not row.source:
                raise MalformedEvidenceError(
                    f"evidence[{index}].database_row[{row_index}].source "
                    f"is empty",
                    index=index,
                )
            if not row.record_key:
                raise MalformedEvidenceError(
                    f"evidence[{index}].database_row[{row_index}]."
                    f"record_key is empty",
                    index=index,
                )
            if not row.queried_at:
                raise MalformedEvidenceError(
                    f"evidence[{index}].database_row[{row_index}]."
                    f"queried_at is empty; a citation about a value that "
                    f"moves needs the time it was read",
                    index=index,
                )
        if locator.document_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].locator.document_id must be empty "
                f"for a database-row item, got {locator.document_id!r}",
                index=index,
            )
    else:
        if not locator.document_id:
            raise MalformedEvidenceError(
                f"evidence[{index}].locator.document_id is empty",
                index=index,
            )


def _unit_identity(
    item: EvidenceItem,
) -> tuple[str, str, str, int]:
    """The identity two evidence items share exactly when they are
    slices of the same frozen unit: kind, source id, version, ordinal.
    """
    source_id = (
        item.attachment_id
        if item.kind == ATTACHMENT_KIND
        else item.locator.document_id
    )
    return (item.kind, source_id, item.locator.version, item.locator.unit)


def _citation_from(item: EvidenceItem, quote: str) -> Citation:
    """Render one matched evidence item as a ``Citation``."""
    return Citation(
        kind=item.kind,
        document_id=(
            ""
            if item.kind in (ATTACHMENT_KIND, DATABASE_ROW_KIND)
            else item.locator.document_id
        ),
        attachment_id=item.attachment_id,
        version=item.locator.version,
        unit=item.locator.unit,
        display=item.display,
        filename=item.filename,
        quote=quote,
    )


def _contiguous_runs(
    evidence: tuple[EvidenceItem, ...],
) -> list[list[EvidenceItem]]:
    """Maximal ADJACENT slice runs (length ≥ 2) of the same unit.

    Items are grouped by unit identity, ordered by ``position`` inside
    each group, and split wherever continuity breaks — an item with no
    ``next_position`` (a retrieve chunk, or an unread tail slice) or a
    nonzero gap between ``prev.next_position`` and ``next.position``.
    A run never bridges a gap: quote text spanning an unseen hole stays
    unmatched (spec thread-attachment-source-read SC10). Group order
    follows first-seen caller order; deterministic, no set iteration.
    """
    groups: dict[tuple[str, str, str, int], list[EvidenceItem]] = {}
    for item in evidence:
        groups.setdefault(_unit_identity(item), []).append(item)

    runs: list[list[EvidenceItem]] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda it: it.position)
        current = [ordered[0]]
        for nxt in ordered[1:]:
            prev = current[-1]
            if prev.next_position is not None and prev.next_position == nxt.position:
                current.append(nxt)
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = [nxt]
        if len(current) >= 2:
            runs.append(current)
    return runs


def _harvest_spans(
    answer: str,
    ev_text: str,
    anchor: EvidenceItem,
    *,
    min_span_len: int,
    seen: set[tuple[tuple[str, str, str, int], str]],
    citations: list[Citation],
) -> None:
    """Record every maximal grounded span of ``answer`` inside
    ``ev_text`` onto ``citations`` (dedup via ``seen``).

    Algorithm: walk ``answer`` left-to-right. At each starting
    position ``i``, greedily extend the match (``answer[i:j] in
    ev_text``) until the next character would fail. The longest match
    starting at ``i`` is recorded (if it meets ``min_span_len`` and
    contains an alphanumeric token) and we skip past it; otherwise we
    advance one position. The greedy rule guarantees the longest match
    per starting position; the "skip past the match" rule prevents
    nested-match explosions without losing non-overlapping spans.
    """
    n = len(answer)
    ev_len = len(ev_text)
    i = 0
    while i <= n - min_span_len:
        # Greedy longest match starting at i. Cap the extension by
        # both the answer length and the evidence length:
        # ``answer[i:j]`` cannot be a substring of ``ev_text`` if its
        # length exceeds ``ev_len``.
        j = i + min_span_len
        max_j = min(n + 1, i + ev_len + 1)
        while j < max_j and answer[i:j] in ev_text:
            j += 1
        match_len = j - 1 - i

        if match_len >= min_span_len:
            quote = answer[i : i + match_len]
            # A span must contain at least one alphanumeric
            # token — whitespace-only / punctuation-only spans
            # are ignored (ADR 0021).
            if any(ch.isalnum() for ch in quote):
                key = (_unit_identity(anchor), quote)
                if key not in seen:
                    seen.add(key)
                    citations.append(_citation_from(anchor, quote))
            # Skip past this match (whether or not we recorded
            # it — any shorter substring at the same start is
            # either no-alnum or non-matching, by definition of
            # the greedy longest-match).
            i += match_len
        else:
            i += 1


def _match_spans(
    answer: str,
    evidence: tuple[EvidenceItem, ...],
    *,
    min_span_len: int,
) -> tuple[Citation, ...]:
    """Find all maximal ≥``min_span_len`` substrings of ``answer``
    that are grounded in this run's evidence.

    Grounded means: a contiguous substring of ONE returned slice, or
    of two or more ADJACENT returned slices of the same frozen unit —
    joined only where the offsets are contiguous. A quote that spans a
    gap no slice covered stays unmatched.

    Returns ``Citation`` objects deduplicated by
    ``(unit identity, quote)``, in deterministic order: every single
    item first in caller order (left-to-right within the answer), then
    every maximal contiguous run in first-item order. Repeats are
    dropped silently.
    """
    n = len(answer)
    if n < min_span_len:
        return ()

    seen: set[tuple[tuple[str, str, str, int], str]] = set()
    citations: list[Citation] = []

    for ev in evidence:
        _harvest_spans(
            answer, ev.unit_text, ev, min_span_len=min_span_len, seen=seen, citations=citations
        )

    for run in _contiguous_runs(evidence):
        joined = "".join(ev.unit_text for ev in run)
        _harvest_spans(
            answer,
            joined,
            run[0],
            min_span_len=min_span_len,
            seen=seen,
            citations=citations,
        )

    return tuple(citations)


__all__ = [
    "COMPANY_KIND",
    "BounceResult",
    "Citation",
    "CitationGateError",
    "DATABASE_ROW_DISPLAY",
    "DATABASE_ROW_KIND",
    "DATABASE_ROW_SNAPSHOT_KEY",
    "DatabaseRow",
    "DoneResult",
    "EvidenceItem",
    "GateResult",
    "MalformedEvidenceError",
    "QUOTABLE_EVIDENCE_KINDS",
    "RefuseResult",
    "UNSUPPORTED_DATABASE_CLAIM",
    "database_row_snapshot",
    "format_database_row_source_id",
    "verify_citations",
]
