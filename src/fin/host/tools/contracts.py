"""Canonical argument contracts and the P3 T1 inventory for every host tool.

One Pydantic model per registered tool is the single source for both the
model-facing schema and runtime pre-effect validation (spec
``harness-runtime-tool-validation`` §Contract, TV-1). The models mirror the
CURRENT ``@tool`` signatures in ``fin.host.graph`` field for field; they add
no new bound. Nested arguments delegate to the existing domain validators
rather than restating them.

:data:`TOOL_CONTRACTS` is the T1 inventory: surfaces, declared bounds and
their sources, domain checks, effect class, stable operation identity, and
the retry/reconciliation policies that exist. A missing applicable policy is
recorded as ``open`` and the affected slice stays blocked; no default is
invented here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Annotated, Any, Callable, Final, Literal, Mapping

from langchain_core.tools import InjectedToolCallId
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fin.host.tools.compare_facts import MAX_CELLS
from fin.host.tools.issuer_fanout import MAX_ISSUERS
from fin.host.tools.statements import MAX_STATEMENT_CELLS
from fin.retrieve.metadata_filters import (
    InvalidFilterError,
    normalized_metadata_filters,
)
from fin.retrieve.port import InvalidBusinessQueryError, resolve_sql_window


class _StrictArgs(BaseModel):
    """Every contract model forbids fields the tool does not declare."""

    model_config = ConfigDict(extra="forbid")


# ----- read_source locators -------------------------------------------------

# These locators were once spelled as a raw JSON schema in the graph; the
# typed models below are the canonical source now, so the bound names them.
_COMPANY_LOCATOR_SOURCE: Final[str] = (
    "contracts.py::CompanySourceRef (company branch)"
)
_ATTACHMENT_LOCATOR_SOURCE: Final[str] = (
    "contracts.py::AttachmentSourceRef (attachment branch)"
)
_ATTACHMENT_VERSION_PATTERN: Final[str] = r"^(0|[1-9][0-9]*)$"


class CompanySourceRef(_StrictArgs):
    """``{document_id, version, unit}`` — the company leg's locator."""

    document_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    unit: int = Field(ge=1)


class AttachmentSourceRef(_StrictArgs):
    """``{kind: "attachment", attachment_id, version, unit}``."""

    kind: Literal["attachment"]
    attachment_id: str = Field(min_length=1)
    version: (
        Annotated[str, Field(pattern=_ATTACHMENT_VERSION_PATTERN)]
        | Annotated[int, Field(ge=0)]
    )
    unit: int = Field(ge=1)

    @field_validator("version", mode="before")
    @classmethod
    def _version_shape(cls, value: object) -> object:
        # The JSON schema allowed a digit string (no leading zero) OR a
        # non-negative integer. Doing this before the union keeps ``"01"``
        # invalid instead of silently coercing it to ``1``.
        if isinstance(value, bool):
            raise ValueError(
                "version must be a digit string or a non-negative integer"
            )
        if isinstance(value, int):
            if value < 0:
                raise ValueError("version must not be negative")
            return value
        if isinstance(value, str):
            if not re.fullmatch(_ATTACHMENT_VERSION_PATTERN, value):
                raise ValueError(
                    "version must be a digit string without a leading zero"
                )
            return value
        raise ValueError(
            "version must be a digit string or a non-negative integer"
        )


SourceRef = CompanySourceRef | AttachmentSourceRef


# ----- tool argument models -------------------------------------------------


class ListFilesArgs(_StrictArgs):
    company: str | None = None
    period: str | None = None
    cik: str | None = None
    form: str | None = None
    ticker: str | None = None
    filing_date: str | None = None


_FILTERS_HINT: Final[str] = (
    "must be an object of at most the four metadata keys "
    "title/doc_type/company/period with 1-256 byte string values and no "
    "control characters or wildcards"
)
_SQL_WINDOW_HINT: Final[str] = (
    "sql_window_start and sql_window_end must be passed together as ordered "
    "YYYY-MM-DD calendar days, or both omitted"
)


class RetrieveArgs(_StrictArgs):
    query: str
    document_id: str | None = None
    limit: int = 8
    cik: str | None = None
    form: str | None = None
    ticker: str | None = None
    filing_date: str | None = None
    attachment_ids: list[str] | None = None
    sql_window_start: Annotated[
        str | None,
        Field(
            description=(
                "Inclusive expiry window START as YYYY-MM-DD, copied from a "
                "date the user stated. Pass it together with sql_window_end or "
                "omit both; never substitute today's date, a relative phrase, or "
                "a default."
            )
        ),
    ] = None
    sql_window_end: Annotated[
        str | None,
        Field(
            description=(
                "Inclusive expiry window END as YYYY-MM-DD, copied from a "
                "date the user stated. Pass it together with sql_window_start or "
                "omit both; a single bound or a malformed date is refused."
            )
        ),
    ] = None
    structured_request: Mapping[str, Any] | None = None
    filters: Annotated[
        Mapping[str, Any] | None,
        Field(
            description=(
                "Optional generic metadata filter, a JSON object with at most "
                "the four exact-match keys title, doc_type, company, period. "
                "Values are exact, case-sensitive string comparisons after "
                "normalization; omit the object to search the whole "
                "authorized scope."
            )
        ),
    ] = None
    # Host-injected by the ToolNode; present so the bound tool's bound
    # arguments match this model, and annotated so LangChain keeps them out
    # of the model-facing schema. Never supplied by the model.
    tool_call_id: Annotated[str, InjectedToolCallId] = ""
    state: Annotated[Any, InjectedState()] = None

    @field_validator("filters")
    @classmethod
    def _filters_contract(
        cls, value: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        if value is None:
            return None
        try:
            normalized_metadata_filters(value)
        except InvalidFilterError:
            # The domain exception text names submitted keys/values; only the
            # safe constraint survives into model feedback.
            raise ValueError(_FILTERS_HINT) from None
        return value

    @model_validator(mode="after")
    def _sql_window_contract(self) -> "RetrieveArgs":
        if self.sql_window_start is None and self.sql_window_end is None:
            return self
        try:
            resolve_sql_window(
                "sql", self.sql_window_start, self.sql_window_end
            )
        except InvalidBusinessQueryError:
            # ``resolve_sql_window`` embeds both submitted bounds in its
            # message; replace it with the value-free constraint.
            raise ValueError(_SQL_WINDOW_HINT) from None
        return self


class GetXbrlFactArgs(_StrictArgs):
    concept: str
    period: str
    document_id: str | None = None
    issuer: str | None = None
    tool_call_id: Annotated[str, InjectedToolCallId] = ""


class CompareFactsArgs(_StrictArgs):
    concept: str
    periods: list[str]
    document_ids: list[str] | None = None
    issuers: list[str] | None = None


class ResearchIssuersArgs(_StrictArgs):
    issuers: list[str]
    query: str


class RedlineArgs(_StrictArgs):
    before_document_id: str
    before_version: str
    after_document_id: str
    after_version: str


class ExtractTablesArgs(_StrictArgs):
    document_id: str
    version: str
    unit: int = 1


class ThreeYearStatementArgs(_StrictArgs):
    issuer: str
    periods: list[str]
    concepts: list[str]


class SaveMemoryArgs(_StrictArgs):
    title: str
    body: str


class RecallMemoryArgs(_StrictArgs):
    query: str
    limit: int = 8


class EditUserProfileArgs(_StrictArgs):
    # ``layer`` stays ``str``: ``UserProfileStore.write_layer`` returns its
    # typed ``user_profile_unknown_layer`` dict for an unknown name, and
    # tests pin that contract. The allowlist is a recorded domain check, not
    # a schema enum.
    layer: str
    text: str


class ReadSourceArgs(_StrictArgs):
    source_ref: SourceRef
    position: int = 0
    tool_call_id: Annotated[str, InjectedToolCallId] = ""


_SCOPE_HINT: Final[str] = (
    "must be four normalized numbers [left, top, right, bottom] within 0..1 "
    "with increasing bounds"
)
_DETAIL_PART_HINT: Final[str] = (
    "must be a non-empty name of at most {limit} characters"
)


def _detail_part_limit() -> int:
    """The visual lane's existing plotted-target bound (do not re-spell it)."""
    from fin.host.attachment.attachment_visual_read import (
        MAX_DETAIL_PART_CHARS,
    )

    return MAX_DETAIL_PART_CHARS


def detail_part_hint() -> str:
    """Value-free feedback for the plotted-target bound."""
    return _DETAIL_PART_HINT.format(limit=_detail_part_limit())


class ReadVisualDetailArgs(_StrictArgs):
    attachment_id: str
    page: int
    chart: str
    series: str
    category: str
    scope: list[float] | None = None
    tool_call_id: Annotated[str, InjectedToolCallId] = ""

    @field_validator("chart", "series", "category")
    @classmethod
    def _detail_part(cls, value: str) -> str:
        limit = _detail_part_limit()
        if not 0 < len(value) <= limit:
            raise ValueError(_DETAIL_PART_HINT.format(limit=limit))
        return value

    @field_validator("scope")
    @classmethod
    def _crop_scope(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        if len(value) != 4:
            raise ValueError(_SCOPE_HINT)
        # Lazy: the visual lane reaches the composition root through the
        # vision adapter, and this module is imported by graph wiring.
        from fin.host.attachment.attachment_vision import VisionCrop

        try:
            VisionCrop(
                left=float(value[0]),
                top=float(value[1]),
                right=float(value[2]),
                bottom=float(value[3]),
            )
        except (TypeError, ValueError, IndexError, KeyError):
            raise ValueError(_SCOPE_HINT) from None
        return value


# ----- inventory records ----------------------------------------------------

ToolSurface = Literal["model_bound", "tool_node", "clarify", "issuer"]
EffectClass = Literal[
    "none", "local_read", "network_read", "persistent_write", "external_effect"
]
PolicyStatus = Literal["declared", "open", "not_applicable"]

#: A field note is either fixed text or resolved lazily (a bound that lives
#: in a module this file must not import at module load).
FieldNote = str | Callable[[], str]


@dataclass(frozen=True)
class DeclaredBound:
    """One field bound that already exists in the tool or its domain."""

    field: str
    constraint: str
    source: str


@dataclass(frozen=True)
class DomainCheck:
    """One authorization or semantic precondition owned below the schema."""

    name: str
    source: str
    note: str = ""


@dataclass(frozen=True)
class PolicyRecord:
    status: PolicyStatus
    summary: str
    source: str | None = None


def _declared(summary: str, source: str) -> PolicyRecord:
    return PolicyRecord("declared", summary, source)


def _open(summary: str, source: str | None = None) -> PolicyRecord:
    return PolicyRecord("open", summary, source)


def _not_applicable(summary: str, source: str | None = None) -> PolicyRecord:
    return PolicyRecord("not_applicable", summary, source)


@dataclass(frozen=True)
class ToolContract:
    """The canonical contract plus the T1 policy inventory for one tool."""

    name: str
    args_model: type[BaseModel]
    surfaces: frozenset[ToolSurface]
    schema_source: str
    bounds: tuple[DeclaredBound, ...]
    domain_checks: tuple[DomainCheck, ...]
    effect: EffectClass
    operation_identity: str | None
    retry: PolicyRecord
    reconciliation: PolicyRecord
    blocked_slices: tuple[str, ...] = ()
    host_injected_fields: frozenset[str] = frozenset()
    #: Safe, value-free feedback hints keyed by field path; ``""`` is the
    #: model-level entry used when a cross-field check rejects the object.
    field_notes: Mapping[str, FieldNote] = dc_field(default_factory=dict)


def _explicit(model: str, tool: str) -> str:
    return (
        f"src/fin/host/tools/contracts.py::{model} (explicit Pydantic model "
        f"mirroring graph.py @tool({tool!r}))"
    )


_READ_ONLY_RETRY: Final[str] = (
    "pure local read with no effect to duplicate; no operation-level retry "
    "policy exists and the model may re-call"
)
_READ_ONLY_RECONCILE: Final[str] = "no effect to reconcile"
_SCOPE_GATE: Final[str] = "fin.host.graph::_company_scope_for_run"

TOOL_CONTRACTS: Final[dict[str, ToolContract]] = {
    "list_files": ToolContract(
        name="list_files",
        args_model=ListFilesArgs,
        surfaces=frozenset({"model_bound", "tool_node", "clarify"}),
        schema_source=_explicit("ListFilesArgs", "list_files"),
        bounds=(),
        domain_checks=(
            DomainCheck(
                "company/snapshot source-scope gate",
                _SCOPE_GATE,
                "runs before the catalog is enumerated",
            ),
            DomainCheck(
                "catalog row narrowing",
                "fin.host.graph::_narrow_filings_to_corpus_ids",
            ),
            DomainCheck(
                "catalog filtering semantics",
                "fin.corpus.catalog::list_filings",
                "unknown/conflicting/empty filters answer an empty list",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_not_applicable(_READ_ONLY_RETRY, "fin.host.tools.list_files"),
        reconciliation=_not_applicable(_READ_ONLY_RECONCILE, "fin.host.tools.list_files"),
    ),
    "retrieve": ToolContract(
        name="retrieve",
        args_model=RetrieveArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("RetrieveArgs", "retrieve"),
        bounds=(
            DeclaredBound(
                "limit",
                "default 8; no schema maximum — the adapter clamps <=0 to 8 "
                "and >20 to 20",
                "fin.retrieve.mcp_adapter (ADR 0009); "
                "fin.host.attachment.attachment_retrieval._MAX_LIMIT",
            ),
            DeclaredBound(
                "filters",
                "at most the four allowlist keys; each value 1-256 UTF-8 "
                "bytes, no Cc control character, no '*'+'?' wildcard",
                "fin.retrieve.metadata_filters "
                "(METADATA_FILTER_KEYS, FILTER_VALUE_MAX_BYTES)",
            ),
            DeclaredBound(
                "sql_window_start/sql_window_end",
                "both or neither; strict YYYY-MM-DD calendar days, end >= start",
                "fin.retrieve.port::resolve_sql_window",
            ),
        ),
        domain_checks=(
            DomainCheck(
                "generic metadata filter normalization",
                "fin.retrieve.metadata_filters::normalized_metadata_filters",
            ),
            DomainCheck(
                "sql expiry window",
                "fin.retrieve.port::resolve_sql_window",
            ),
            DomainCheck(
                "query and legacy filter validity",
                "fin.retrieve.input_validation.validators",
                "empty query and provably conflicting filters are typed input failures",
            ),
            DomainCheck(
                "structured-request mode compatibility",
                "fin.retrieve.port::_structured_query_refusal_code",
                "fields remain the structured backend's concern",
            ),
            DomainCheck(
                "attachment source-scope gate",
                "fin.host.graph::_scoped_search_ids",
            ),
        ),
        effect="network_read",
        operation_identity=None,
        retry=_open(
            "No general-failure execution retry is declared. The host's "
            "empty-hit rewrite is bounded at one rewrite (max_retries=1) and "
            "is a read strategy, not an execution-retry policy.",
            "fin.host.graph::retrieve_with_rewrite_retry",
        ),
        reconciliation=_not_applicable(_READ_ONLY_RECONCILE, "fin.retrieve.port"),
        host_injected_fields=frozenset({"tool_call_id", "state"}),
        field_notes={
            "filters": _FILTERS_HINT,
            "structured_request": (
                "must be a JSON object; its query fields and predicates are "
                "owned by the structured backend"
            ),
            "": _SQL_WINDOW_HINT,
        },
    ),
    "get_xbrl_fact": ToolContract(
        name="get_xbrl_fact",
        args_model=GetXbrlFactArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("GetXbrlFactArgs", "get_xbrl_fact"),
        bounds=(),
        domain_checks=(
            DomainCheck("company/snapshot source-scope gate", _SCOPE_GATE),
            DomainCheck(
                "named-document selection check",
                "fin.host.graph::_corpus_document_permitted",
            ),
            DomainCheck(
                "fact lookup",
                "fin.corpus.facts::lookup_fact",
                "a miss answers the typed fact_not_found dict",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_not_applicable(
            _READ_ONLY_RETRY, "fin.host.tools.get_xbrl_fact"
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.tools.get_xbrl_fact"
        ),
        host_injected_fields=frozenset({"tool_call_id"}),
    ),
    "compare_facts": ToolContract(
        name="compare_facts",
        args_model=CompareFactsArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("CompareFactsArgs", "compare_facts"),
        bounds=(
            DeclaredBound(
                "periods x rows",
                f"at most {MAX_CELLS} cells; overflow answers comparison_overflow",
                "fin.host.tools.compare_facts::MAX_CELLS",
            ),
        ),
        domain_checks=(
            DomainCheck("company/snapshot source-scope gate", _SCOPE_GATE),
            DomainCheck(
                "cell-count cap and empty request",
                "fin.host.tools.compare_facts::compare_facts",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_not_applicable(
            _READ_ONLY_RETRY, "fin.host.tools.compare_facts"
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.tools.compare_facts"
        ),
    ),
    "research_issuers": ToolContract(
        name="research_issuers",
        args_model=ResearchIssuersArgs,
        surfaces=frozenset({"model_bound", "issuer"}),
        schema_source=_explicit("ResearchIssuersArgs", "research_issuers"),
        bounds=(
            DeclaredBound(
                "issuers",
                f"at most {MAX_ISSUERS}; a larger roster answers issuer_overflow",
                "fin.host.tools.issuer_fanout::MAX_ISSUERS",
            ),
        ),
        domain_checks=(
            DomainCheck(
                "fan-out roster planning",
                "fin.host.tools.issuer_fanout::plan_issuer_fanout",
            ),
        ),
        effect="network_read",
        operation_identity=None,
        retry=_not_applicable(
            "The bound tool body never executes in the compiled graph; each "
            "issuer branch calls the retrieve port, whose unset general-failure "
            "retry policy is inventoried on 'retrieve'.",
            "fin.host.graph::_make_research_issuers_tool",
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.graph::_make_research_issuers_tool"
        ),
    ),
    "redline": ToolContract(
        name="redline",
        args_model=RedlineArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("RedlineArgs", "redline"),
        bounds=(),
        domain_checks=(
            DomainCheck(
                "company/snapshot source-scope gate",
                _SCOPE_GATE,
                "both sides must sit inside the selection before either opens",
            ),
            DomainCheck(
                "locator resolution",
                "fin.host.tools.redline::redline",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_not_applicable(_READ_ONLY_RETRY, "fin.host.tools.redline"),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.tools.redline"
        ),
    ),
    "extract_tables": ToolContract(
        name="extract_tables",
        args_model=ExtractTablesArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("ExtractTablesArgs", "extract_tables"),
        bounds=(
            DeclaredBound(
                "unit",
                "default 1; a unit < 1 answers the typed unknown_locator dict",
                "fin.host.tools.table_extract::extract_tables",
            ),
        ),
        domain_checks=(
            DomainCheck("company/snapshot source-scope gate", _SCOPE_GATE),
            DomainCheck(
                "locator resolution",
                "fin.host.tools.table_extract::extract_tables",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_not_applicable(
            _READ_ONLY_RETRY, "fin.host.tools.table_extract"
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.tools.table_extract"
        ),
    ),
    "three_year_statement": ToolContract(
        name="three_year_statement",
        args_model=ThreeYearStatementArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("ThreeYearStatementArgs", "three_year_statement"),
        bounds=(
            DeclaredBound(
                "periods",
                "exactly 3 after cleaning; otherwise statement_requires_three_years",
                "fin.host.tools.statements::three_year_statement",
            ),
            DeclaredBound(
                "periods x concepts",
                f"at most {MAX_STATEMENT_CELLS} cells; otherwise statement_overflow",
                "fin.host.tools.statements::MAX_STATEMENT_CELLS",
            ),
        ),
        domain_checks=(
            DomainCheck("company/snapshot source-scope gate", _SCOPE_GATE),
            DomainCheck(
                "three-year and cell-count request checks",
                "fin.host.tools.statements::three_year_statement",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_not_applicable(
            _READ_ONLY_RETRY, "fin.host.tools.statements"
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.tools.statements"
        ),
    ),
    "save_memory": ToolContract(
        name="save_memory",
        args_model=SaveMemoryArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("SaveMemoryArgs", "save_memory"),
        bounds=(),
        domain_checks=(
            DomainCheck(
                "workspace root resolution",
                "fin.host.graph::_resolve_workspace_root",
                "missing workspace_root raises instead of defaulting to $HOME",
            ),
            DomainCheck(
                "extract admission",
                "fin.host.memory_buffer::LocalMemoryBuffer.extract",
                "empty text answers {'status': 'empty'}",
            ),
        ),
        effect="persistent_write",
        operation_identity=None,
        retry=_open(
            "No declared retry. A repeated extract mints a new row id, so a "
            "retry can duplicate the same intent.",
            "fin.host.memory_buffer::LocalMemoryBuffer.extract",
        ),
        reconciliation=_open(
            "No declared reconciliation-by-operation-identity policy; a write "
            "may have committed before an uncertain outcome.",
            "fin.host.memory_port::MemoryPort.extract",
        ),
        blocked_slices=("execution", "retry", "reconciliation"),
    ),
    "recall_memory": ToolContract(
        name="recall_memory",
        args_model=RecallMemoryArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("RecallMemoryArgs", "recall_memory"),
        bounds=(
            DeclaredBound(
                "limit",
                "default 8; the backend resolves the cap at runtime, no schema maximum",
                "fin.host.memory_port::DEFAULT_MEMORY_LIMIT / MEMORY_LIMIT_CAP",
            ),
        ),
        domain_checks=(
            DomainCheck(
                "workspace root resolution",
                "fin.host.graph::_resolve_workspace_root",
            ),
            DomainCheck(
                "recall limit and workspace scope",
                "fin.host.memory_buffer::LocalMemoryBuffer.recall",
            ),
        ),
        effect="local_read",
        operation_identity=None,
        retry=_open(
            "The transport-neutral memory port declares no failure retry "
            "policy; the backend may be local or remote.",
            "fin.host.memory_port::MemoryPort.recall",
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.memory_port::MemoryPort.recall"
        ),
        blocked_slices=("retry",),
    ),
    "edit_user_profile": ToolContract(
        name="edit_user_profile",
        args_model=EditUserProfileArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit("EditUserProfileArgs", "edit_user_profile"),
        bounds=(),
        domain_checks=(
            DomainCheck(
                "layer allowlist",
                "fin.host.user_profile::UserProfileStore.write_layer",
                "unknown layer answers the typed user_profile_unknown_layer dict",
            ),
        ),
        effect="persistent_write",
        operation_identity=None,
        retry=_open(
            "No declared retry policy. Rewriting the same text is idempotent "
            "in content, but no policy states when a retry is allowed.",
            "fin.host.user_profile::UserProfileStore.write_layer",
        ),
        reconciliation=_open(
            "No declared reconciliation-by-operation-identity policy for a "
            "profile write.",
            "fin.host.user_profile::UserProfileStore.write_layer",
        ),
        blocked_slices=("execution", "retry", "reconciliation"),
    ),
    "read_source": ToolContract(
        name="read_source",
        args_model=ReadSourceArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=(
            "src/fin/host/tools/contracts.py::CompanySourceRef / "
            "AttachmentSourceRef (the canonical locator models the graph "
            "used to spell as a raw schema)"
        ),
        bounds=(
            DeclaredBound(
                "source_ref.document_id / attachment_id / version",
                "minLength 1 on every identifier string",
                _COMPANY_LOCATOR_SOURCE + "; " + _ATTACHMENT_LOCATOR_SOURCE,
            ),
            DeclaredBound(
                "source_ref.unit",
                "integer >= 1",
                _COMPANY_LOCATOR_SOURCE + "; " + _ATTACHMENT_LOCATOR_SOURCE,
            ),
            DeclaredBound(
                "source_ref.version (attachment)",
                "digit string matching ^(0|[1-9][0-9]*)$ or integer >= 0",
                _ATTACHMENT_LOCATOR_SOURCE,
            ),
        ),
        domain_checks=(
            DomainCheck(
                "cursor validity",
                "fin.retrieve.source_read::position_error",
                "non-integer or negative position answers invalid_position",
            ),
            DomainCheck(
                "attachment source-scope gate",
                "fin.host.graph::_read_source_scope_refusal",
            ),
            DomainCheck(
                "locator/version/position leg checks",
                "fin.retrieve.source_read",
            ),
        ),
        effect="network_read",
        operation_identity=None,
        retry=_open(
            "No declared retry policy. The read is side-effect free, so a "
            "retry cannot duplicate an effect, but no policy names failure "
            "handling.",
            "fin.host.tools.read_source",
        ),
        reconciliation=_not_applicable(
            _READ_ONLY_RECONCILE, "fin.host.tools.read_source"
        ),
        host_injected_fields=frozenset({"tool_call_id"}),
        field_notes={
            "source_ref": (
                "must be one of the two declared locator shapes: "
                "{document_id, version, unit} or "
                '{kind: "attachment", attachment_id, version, unit}'
            ),
        },
    ),
    "read_visual_detail": ToolContract(
        name="read_visual_detail",
        args_model=ReadVisualDetailArgs,
        surfaces=frozenset({"model_bound", "tool_node"}),
        schema_source=_explicit(
            "ReadVisualDetailArgs", "read_visual_detail"
        ),
        bounds=(
            DeclaredBound(
                "chart / series / category",
                "1-120 characters (existing plotted-target bound promoted to "
                "the schema)",
                "fin.host.attachment.attachment_visual_read::"
                "MAX_DETAIL_PART_CHARS",
            ),
            DeclaredBound(
                "scope",
                "four normalized numbers in 0..1 with right > left and "
                "bottom > top",
                "fin.host.attachment.attachment_vision::VisionCrop",
            ),
            DeclaredBound(
                "page",
                "1-based; no schema bound — the lane checks page >= 1 and "
                "page <= the attachment's stored unit count",
                "fin.host.attachment.attachment_visual_read",
            ),
        ),
        domain_checks=(
            DomainCheck(
                "attachment source-scope gate",
                "fin.host.graph::_visual_scope_refusal",
                "runs before a page is drawn or a vision call is charged",
            ),
            DomainCheck(
                "page and observation availability",
                "fin.host.attachment.attachment_visual_read",
            ),
            DomainCheck(
                "run vision budget",
                "fin.host.attachment.attachment_budgets::VisionCallLedger",
            ),
        ),
        effect="external_effect",
        operation_identity=(
            "DetailKey(attachment_id, attachment version, unit, page, chart, "
            "series, category, scope) with observation_version — the stable "
            "key a stored observation is reused against"
        ),
        retry=_declared(
            "One reserved full-page transport retry per run, limited to "
            "adapter-classified retryable transport failures.",
            "fin.host.attachment.attachment_vision / "
            "attachment_vision_inspect",
        ),
        reconciliation=_open(
            "No declared reconciliation-by-operation-identity policy when a "
            "provider call's outcome is unknown. Observation reuse is a cache, "
            "not proof of the charged call's state.",
            "fin.host.attachment.attachment_visual_read",
        ),
        blocked_slices=("reconciliation",),
        host_injected_fields=frozenset({"tool_call_id"}),
        field_notes={
            "chart": detail_part_hint,
            "series": detail_part_hint,
            "category": detail_part_hint,
            "scope": _SCOPE_HINT,
        },
    ),
}


class UnknownToolContract(LookupError):
    """No canonical contract is registered for the requested tool name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no tool contract registered for {name!r}")
        self.name = name


def contract_for(name: str) -> ToolContract:
    """Return the canonical contract for ``name`` or raise."""
    try:
        return TOOL_CONTRACTS[name]
    except KeyError:
        raise UnknownToolContract(name) from None


__all__ = [
    "AttachmentSourceRef",
    "CompanySourceRef",
    "CompareFactsArgs",
    "DeclaredBound",
    "DomainCheck",
    "EditUserProfileArgs",
    "EffectClass",
    "ExtractTablesArgs",
    "GetXbrlFactArgs",
    "ListFilesArgs",
    "PolicyRecord",
    "ReadSourceArgs",
    "ReadVisualDetailArgs",
    "RecallMemoryArgs",
    "RedlineArgs",
    "ResearchIssuersArgs",
    "RetrieveArgs",
    "SaveMemoryArgs",
    "SourceRef",
    "TOOL_CONTRACTS",
    "ThreeYearStatementArgs",
    "ToolContract",
    "ToolSurface",
    "UnknownToolContract",
    "contract_for",
    "detail_part_hint",
]
