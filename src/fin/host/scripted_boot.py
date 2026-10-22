"""Scripted-provider boot for the browser e2e lane (T7).

`specs/workspace.md` Amendment 2026-08-26 puts one browser lane in
scope on the condition that it runs on the scripted model plus
``InMemorySaver``: no network LLM, no credentials, deterministic. This
module is that boot path — it builds the SAME host graph
``fin.host.http`` serves, with :class:`ScriptedChatModel` in the model
seat and a transcript derived from the frozen corpus.

Explicit opt-in, never a fallback
---------------------------------
``compile_for_runtime_dev`` deliberately refuses to auto-fake the model
("better a loud failure than a hidden scripted substitute masquerading
as a real conversation"). This module keeps that rule: it engages only
when the operator sets ``FIN_SCRIPTED_MODEL`` to a truthy value, and a
value that is neither truthy nor falsy raises
:class:`ScriptedBootConfigError` instead of quietly reading as "not
requested". Nothing about a missing credential, an unreachable proxy,
or an ``InMemorySaver`` checkpointer can select this path.

Why the transcript reads the corpus
-----------------------------------
The citation gate only says ``done`` when the answer carries a
≥32-character span copied verbatim from evidence this run retrieved
(ADR 0021). A hard-coded fixture answer would therefore bounce the
moment the frozen unit text changed. The transcript instead reads the
unit it is about to quote out of the committed extracted tree at boot,
so the lane's completed turn stays grounded by construction.

Pacing
------
Frames are ordered deterministically; only their spacing is timed.
``FIN_SCRIPTED_DELTA_MS`` (default
:data:`DEFAULT_DELTA_PACING_MS`) sleeps between chunks so a browser
observes prose growing token-by-token — and so the run's thinking clock
measures a non-zero duration for the post-run collapse label. Set it to
``0`` for host-speed runs.
"""

from __future__ import annotations

import json
import logging
import time
import warnings
from pathlib import Path
from typing import Any, Iterator, Mapping, NamedTuple

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from pydantic import Field

from fin.corpus.errors import IngestMissingError
from fin.host.grounded.grounded_decision import CANDIDATE_TERMINATOR
from fin.host.scripted_model import ScriptedChatModel
from fin.retrieve import FakeRetrievePort


logger = logging.getLogger(__name__)


# ----- public constants -----


SCRIPTED_BOOT_ENV_VAR = "FIN_SCRIPTED_MODEL"
SCRIPTED_DELTA_MS_ENV_VAR = "FIN_SCRIPTED_DELTA_MS"
SCRIPTED_SCENARIO_ENV_VAR = "FIN_SCRIPTED_SCENARIO"

# Which transcript the lane replays.
#
# ``grounded`` is one model step that answers from the unit it just
# read — the shape T7 already drove.
#
# ``bounce`` prefixes that with an UNGROUNDED answer step, so the
# citation gate refuses it, stamps its hidden bounce marker, and the
# retry streams the grounded prose. That is the only way the browser
# can watch the T3 boundary: the marker arms the generator's pending
# reset, the retry's first delta carries ``reset: true``, and the
# invariant under test is that the bubble never blanks in between.
# Without this scenario the lane's transcript can never bounce, and
# "no retract across a bounce" — an observable T7's acceptance names —
# is asserted nowhere in a browser.
SCRIPTED_SCENARIO_GROUNDED = "grounded"
SCRIPTED_SCENARIO_BOUNCE = "bounce"

# ``document_choice`` pauses the run on a grounded document choice
# (QA-chain T9 / GE-4) so the browser can watch the operator pick a
# candidate and the SAME run answer inside the picked source. It reads
# TWO filings — both already in the frozen manifest, so no second
# document has to be seeded — emits the hidden candidate terminator the
# admission node turns into a ``document_choice`` question, and then, on
# the resume leg, reads and quotes the picked filing only. Without this
# scenario the lane's transcript can never ask a question the browser has
# to answer, and the whole T9 surface — the choice card, the pick, the
# preserved run/receipt — is asserted nowhere in a browser.
#
# WHY two manifest filings and no seeded second document: a candidate is
# admitted only when an admitted hit backs it, so the scenario has to
# READ two pages to offer two candidates — but both pages already exist.
# Seeding a filing the frozen manifest does not carry would have moved
# ``corpus_filing_count`` and every corpus-shaped expectation with it, for
# no coverage this scenario needs.
SCRIPTED_SCENARIO_DOCUMENT_CHOICE = "document_choice"
SCRIPTED_SCENARIOS = (
    SCRIPTED_SCENARIO_GROUNDED,
    SCRIPTED_SCENARIO_BOUNCE,
    SCRIPTED_SCENARIO_DOCUMENT_CHOICE,
)

# The filing the scripted transcript reads and quotes. Present in the
# frozen manifest since v1 ingest; a corpus without it is a fixture
# error, not a silent switch to some other document.
SCRIPTED_DOCUMENT_ID = "AAPL-2022Q3"

# The SECOND filing the ``document_choice`` scenario offers as an
# alternative candidate. A choice needs two hit-backed candidates — one
# document can never produce a pick — and this is the one the transcript
# names so both candidates are backed by pages the run actually read.
# Also a v1 manifest filing, so the lane needs no second seeded document
# and no existing check's corpus view shifts.
SCRIPTED_ALTERNATE_DOCUMENT_ID = "AAPL-2023Q3"

# Milliseconds between streamed chunks. Large enough that a browser
# polling once per second sees the answer grow across ticks, small
# enough that the whole lane run stays under a few seconds.
DEFAULT_DELTA_PACING_MS = 200

# Characters of frozen-unit text quoted verbatim into the answer. Must
# clear the gate's 32-character minimum span with room to spare.
_QUOTED_SPAN_CHARS = 180

# How many chunks the answer is split into on the wire.
_ANSWER_CHUNKS = 6

# How many chunks the candidate request is split into. More than the
# answer's, deliberately: the terminator is a hidden marker the transport
# holds back across deltas, and a request that arrives in few pieces
# would leave that hold-back untested.
_CHOICE_CHUNKS = 8

# The ungrounded draft the ``bounce`` scenario streams first. It quotes
# nothing from the unit, so the gate finds no verbatim span and takes
# the bounce path. Long enough that the browser sees real prose in the
# bubble before the retry replaces it — a one-word draft would make the
# no-blank invariant untestable.
_UNGROUNDED_DRAFT = (
    "From memory, Apple's quarterly revenue that period was roughly "
    "eighty-three billion dollars, though I have not opened the filing "
    "to confirm the exact printed figure."
)

# The prose the ``document_choice`` scenario emits in front of the
# candidate terminator. Ordinary-person Chinese with no ids or tool
# names (the readable-projection spec), and it is withheld from the wire
# by the transport's own terminator hold-back, so the browser never sees
# it: the pause publishes the choice card instead.
_CHOICE_LEAD_IN = "这个问题可能指不同的资料。"

# The two read_source call ids of this scenario. The FIRST two reads
# build the candidate evidence (one page per filing, so both candidates
# are hit-backed); the THIRD re-reads the picked filing on the resume
# leg, because the pause drops the pre-pick evidence and the resumed leaf
# re-retrieves inside the narrowed scope.
_CHOICE_READ_ID_PREFIX = "scripted-read"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})

_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "corpus"
_DEFAULT_MANIFEST_PATH = _DEFAULT_DATA_DIR / "manifest.json"
_DEFAULT_EXTRACTED_ROOT = _DEFAULT_DATA_DIR / "extracted"


# ----- typed errors -----


class ScriptedBootError(Exception):
    """Base class for scripted-boot typed errors."""


class ScriptedBootConfigError(ScriptedBootError):
    """An environment value controlling the scripted boot is unreadable.

    Raised for an opt-in value that is neither truthy nor falsy and for
    a pacing value that is not a finite, non-negative number. Both stop
    the boot: an operator who spelled the switch wrong must see it
    rather than get the production provider back by accident.
    """


class ScriptedBootFixtureError(ScriptedBootError):
    """The frozen corpus cannot supply the transcript's grounded quote.

    Raised when the extracted tree is missing, does not contain
    :data:`SCRIPTED_DOCUMENT_ID`, or its first unit is shorter than the
    span the answer must quote. Booting anyway would serve a lane whose
    every run bounces the citation gate for reasons that have nothing
    to do with the code under test.
    """


# ----- env reads -----


def _lookup(env: Mapping[str, str] | None, name: str) -> str | None:
    import os

    source: Mapping[str, str] = os.environ if env is None else env
    return source.get(name)


def scripted_boot_requested(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the operator asked for the scripted provider.

    Reads ``FIN_SCRIPTED_MODEL`` from ``env`` (``os.environ`` when
    ``None``). Absent or falsy is ``False``; a truthy value is ``True``;
    anything else raises :class:`ScriptedBootConfigError`.
    """
    raw = _lookup(env, SCRIPTED_BOOT_ENV_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ScriptedBootConfigError(
        f"{SCRIPTED_BOOT_ENV_VAR}={raw!r} is not readable as a switch; "
        f"use one of {sorted(_TRUTHY)} to boot the scripted e2e provider "
        f"or one of {sorted(_FALSY - {''})} to boot the configured provider."
    )


def scripted_scenario(env: Mapping[str, str] | None = None) -> str:
    """Return the transcript scenario the operator asked for.

    Absent or empty is :data:`SCRIPTED_SCENARIO_GROUNDED`. Any other
    unrecognised value raises :class:`ScriptedBootConfigError` rather
    than falling back: a misspelled scenario that silently ran the
    grounded transcript would report a green bounce check that never
    bounced, and a green document-choice check that never paused.
    """
    raw = _lookup(env, SCRIPTED_SCENARIO_ENV_VAR)
    if raw is None or raw.strip() == "":
        return SCRIPTED_SCENARIO_GROUNDED
    value = raw.strip().lower()
    if value in SCRIPTED_SCENARIOS:
        return value
    raise ScriptedBootConfigError(
        f"{SCRIPTED_SCENARIO_ENV_VAR}={raw!r} names no scripted "
        f"transcript; use one of {list(SCRIPTED_SCENARIOS)}."
    )


def _delta_seconds(env: Mapping[str, str] | None) -> float:
    raw = _lookup(env, SCRIPTED_DELTA_MS_ENV_VAR)
    if raw is None or raw.strip() == "":
        return DEFAULT_DELTA_PACING_MS / 1000.0
    try:
        millis = float(raw)
    except ValueError as exc:
        raise ScriptedBootConfigError(
            f"{SCRIPTED_DELTA_MS_ENV_VAR}={raw!r} is not a number of "
            "milliseconds."
        ) from exc
    if millis < 0 or millis == float("inf"):
        raise ScriptedBootConfigError(
            f"{SCRIPTED_DELTA_MS_ENV_VAR}={raw!r} must be a finite, "
            "non-negative number of milliseconds."
        )
    return millis / 1000.0


# ----- the model -----


class PacedScriptedChatModel(ScriptedChatModel):
    """A :class:`ScriptedChatModel` that cycles and paces its chunks.

    Two differences from the test-surface base class, both required by
    a long-lived server process rather than a one-shot test:

    * **Cycling.** The base class pins the LAST response once the
      transcript is exhausted (the step-cap loop contract). A server
      compiles the graph once and then answers every send with the same
      model object, so the second send would replay an answer-only tail
      against a freshly reset (empty) evidence list and bounce. Cycling
      back to the top makes send N behave exactly like send 1.
    * **Pacing.** Real deltas arrive over time. ``delta_seconds``
      spaces the chunks so the browser can observe the answer growing
      and so the run's thinking clock reports a non-zero duration.
    """

    delta_seconds: float = Field(default=0.0)

    def _stream(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if self.stream_responses and self._stream_index >= len(
            self.stream_responses
        ):
            self._stream_index = 0
        for chunk in super()._stream(
            messages, stop=stop, run_manager=run_manager, **kwargs
        ):
            if self.delta_seconds > 0:
                time.sleep(self.delta_seconds)
            yield chunk


# ----- the transcript -----


def _split(text: str, parts: int) -> list[str]:
    """Split ``text`` into ``parts`` non-empty chunks, in order."""
    size = max(1, -(-len(text) // parts))
    return [text[i : i + size] for i in range(0, len(text), size)]


def _read_source_response(
    document_id: str, version: str, unit: int, call_id: str
) -> list[AIMessageChunk]:
    """One research step: reasoning deltas plus a ``read_source`` call.

    The exact chunk shape a live provider streams (the tool call split
    across three chunks — reasoning, name+id, then args), factored out so
    every scenario reads one way. ``call_id`` must be distinct per call:
    the tool node matches each ``ToolMessage`` to its own call, and two
    calls sharing one id would collapse into a single unreadable hop.
    """
    args = (
        '{"source_ref": '
        f'{{"document_id": "{document_id}", "version": "{version}", '
        f'"unit": {unit}}}, "position": 0}}'
    )
    return [
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [
                    {"text": f"Reading {document_id} page {unit} "}
                ]
            },
        ),
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [{"text": "before answering."}]
            },
            tool_call_chunks=[
                {
                    "name": "read_source",
                    "args": "",
                    "id": call_id,
                    "index": 0,
                }
            ],
        ),
        AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": None, "args": args, "id": None, "index": 0}
            ],
        ),
    ]


def _grounded_answer_response(answer: str) -> list[AIMessageChunk]:
    """The answer step: one reasoning delta, then the prose in chunks."""
    return [
        AIMessageChunk(
            content="",
            additional_kwargs={
                "reasoning_details": [{"text": " Quoting the page verbatim."}]
            },
        ),
        *[
            AIMessageChunk(content=part)
            for part in _split(answer, _ANSWER_CHUNKS)
        ],
    ]


def _candidate_request_response(
    document_ids: tuple[str, ...],
) -> list[AIMessageChunk]:
    """The candidate-request step: prose, the terminator, then the JSON.

    Split across several content chunks on purpose. The terminator is a
    hidden wire marker that the transport withholds from a streaming
    answer by holding back any suffix that could still grow into it, so a
    terminator arriving whole in one delta is a legal but untested case;
    splitting across deltas drives exactly the hold-back the production
    path relies on, and is what makes this scenario a real exercise of
    the admission seam rather than of the script's own chunking.
    """
    payload = json.dumps(
        {"candidates": [{"document_id": doc} for doc in document_ids]},
        ensure_ascii=False,
    )
    text = f"{_CHOICE_LEAD_IN}{CANDIDATE_TERMINATOR}{payload}"
    return [AIMessageChunk(content=part) for part in _split(text, _CHOICE_CHUNKS)]


def build_scripted_transcript(
    *,
    document_id: str,
    version: str,
    unit: int,
    unit_text: str,
    scenario: str = SCRIPTED_SCENARIO_GROUNDED,
    alternate_document_id: str = SCRIPTED_ALTERNATE_DOCUMENT_ID,
    alternate_version: str = "",
    alternate_unit: int = 1,
    alternate_unit_text: str = "",
) -> list[list[AIMessageChunk]]:
    """Return the streamed responses one scripted run replays.

    Response 1 is the research step: reasoning deltas plus a
    ``read_source`` tool call split across chunks (exactly the shape the
    live provider streams). The last response is the answer step: one
    more reasoning delta, then the grounded answer split into
    :data:`_ANSWER_CHUNKS` content chunks so the wire carries deltas
    rather than a dump.

    ``scenario=`` :data:`SCRIPTED_SCENARIO_BOUNCE` inserts an
    ungrounded answer step between the two, which the citation gate
    refuses; the grounded step then runs as the post-bounce retry.

    ``scenario=`` :data:`SCRIPTED_SCENARIO_DOCUMENT_CHOICE` replaces that
    shape with a PAUSING one, and is the only transcript that reads two
    filings:

    1. read the first filing;
    2. read the alternate filing;
    3. emit the candidate request naming both, which the admission node
       turns into a ``document_choice`` question and the graph pauses on;
    4. re-read the FIRST filing;
    5. answer from the first filing alone.

    Steps 4 and 5 are what the browser drives AFTER the operator picks:
    the pause clears the pre-pick evidence and commits the picked
    source's scope, so the resumed leaf re-reads inside that narrowed
    scope and quotes only the picked page. The run id never changes
    across the pause — that is the receipt identity the browser check
    asserts — and the answer deliberately names only the picked filing,
    so an answer that leaked the unpicked one would be visible as text.
    """
    if scenario not in SCRIPTED_SCENARIOS:
        raise ScriptedBootConfigError(
            f"scenario={scenario!r} names no scripted transcript; use "
            f"one of {list(SCRIPTED_SCENARIOS)}."
        )
    # Prefer a relevant, source-backed statement over the filing cover or
    # report header. The chosen line remains an exact substring of the
    # admitted unit, so the citation gate still verifies the same evidence.
    quote = next(
        (
            line.strip()
            for line in unit_text.splitlines()
            if line.strip().startswith("Total net sales ")
        ),
        unit_text[:_QUOTED_SPAN_CHARS],
    )
    # A company unit presents as a physical page, which is the label the
    # browser lane's transcript and its assertions use.
    answer = f"Per {document_id} page {unit}: {quote}"
    if scenario == SCRIPTED_SCENARIO_DOCUMENT_CHOICE:
        # Two filings, both quoted from their own pages, so the browser
        # check can tell the picked answer from the unpicked one by text
        # alone rather than by a state field.
        return [
            _read_source_response(
                document_id, version, unit, f"{_CHOICE_READ_ID_PREFIX}-1"
            ),
            _read_source_response(
                alternate_document_id,
                alternate_version,
                alternate_unit,
                f"{_CHOICE_READ_ID_PREFIX}-2",
            ),
            _candidate_request_response(
                (document_id, alternate_document_id)
            ),
            _read_source_response(
                document_id, version, unit, f"{_CHOICE_READ_ID_PREFIX}-3"
            ),
            _grounded_answer_response(answer),
        ]
    # The transcript quotes ``unit_text``, so it must name that unit: an
    # identity that did not resolve would return no text and the gate
    # would bounce every lane run for a reason unrelated to the code
    # under test.
    draft_step: list[list[AIMessageChunk]] = []
    if scenario == SCRIPTED_SCENARIO_BOUNCE:
        draft_step = [
            [
                AIMessageChunk(content=part)
                for part in _split(_UNGROUNDED_DRAFT, _ANSWER_CHUNKS)
            ]
        ]
    return [
        _read_source_response(
            document_id, version, unit, f"{_CHOICE_READ_ID_PREFIX}-1"
        ),
        *draft_step,
        _grounded_answer_response(answer),
    ]


class _FixtureUnit(NamedTuple):
    """One filing's selected unit: identity and text for ``read_source``.

    A named tuple rather than a bare tuple because the ``document_choice``
    scenario carries TWO of these through the transcript builder, and four
    positional strings that must line up are exactly the shape a reader
    gets wrong silently.
    """

    version: str
    unit: int
    text: str


def _load_fixture_unit(
    manifest_path: Path,
    extracted_root: Path,
    *,
    document_id: str = SCRIPTED_DOCUMENT_ID,
    read_span: bool = True,
) -> tuple[_FixtureUnit, dict[str, Any]]:
    """Return ``(unit, pages_by_doc)`` for the lane.

    The tree is returned alongside the one unit because the lane's
    company read leg is the fake retrieve port: it serves
    ``read_source`` only from the bodies it is handed at construction,
    and the extracted tree is that body table here.

    ``read_span`` is False for the unpicked candidate in the
    ``document_choice`` scenario: it only has to exist for the read to
    return text, because no answer ever quotes it. The selected answer
    unit prefers the page containing the relevant net-sales statement,
    rather than an unrelated filing cover.
    """
    from fin.corpus.extracted import load_extracted_tree

    try:
        tree = load_extracted_tree(manifest_path, extracted_root)
    except (IngestMissingError, OSError) as exc:
        raise ScriptedBootFixtureError(
            "the scripted e2e transcript needs the committed extracted "
            f"tree under {extracted_root}: {exc}"
        ) from exc

    pages = tree.get(document_id)
    if not pages:
        raise ScriptedBootFixtureError(
            f"the frozen corpus has no {document_id}; the "
            "scripted e2e transcript quotes that filing."
        )
    unit_page = next(
        (page for page in pages if "Total net sales " in page.text), pages[0]
    )
    if read_span and len(unit_page.text) < _QUOTED_SPAN_CHARS:
        raise ScriptedBootFixtureError(
            f"{document_id} unit {unit_page.locator.unit} has "
            f"{len(unit_page.text)} characters; the scripted transcript "
            f"quotes {_QUOTED_SPAN_CHARS}."
        )
    return (
        _FixtureUnit(
            version=unit_page.locator.version,
            unit=unit_page.locator.unit,
            text=unit_page.text,
        ),
        dict(tree),
    )


# ----- the boot path -----


def compile_for_scripted_e2e(
    *,
    env: Mapping[str, str] | None = None,
    manifest_path: Path | str | None = None,
    extracted_root: Path | str | None = None,
) -> Any:
    """Build the host graph on the scripted provider for the e2e lane.

    ``InMemorySaver`` for state, ``FakeRetrievePort`` for retrieve (no
    MCP subprocess), :class:`PacedScriptedChatModel` for the model, and
    no ``jev_client`` — the rules tier already routes
    the lane's fully-slotted question to ``grounded`` and a gray-zone
    judge call would consume transcript responses meant for the run
    itself.

    ``FIN_SCRIPTED_SCENARIO`` picks the transcript; see
    :func:`scripted_scenario`.

    Raises
    ------
    ScriptedBootConfigError
        ``FIN_SCRIPTED_DELTA_MS`` is not a finite, non-negative number,
        or ``FIN_SCRIPTED_SCENARIO`` names no transcript.
    ScriptedBootFixtureError
        The frozen corpus cannot supply the grounded quote.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from fin.host.graph import compile_graph

    delta_seconds = _delta_seconds(env)
    scenario = scripted_scenario(env)
    mpath = Path(manifest_path) if manifest_path else _DEFAULT_MANIFEST_PATH
    rpath = Path(extracted_root) if extracted_root else _DEFAULT_EXTRACTED_ROOT
    fixture, pages_by_doc = _load_fixture_unit(mpath, rpath)
    known_document_ids = set(pages_by_doc)

    message = (
        f"{SCRIPTED_BOOT_ENV_VAR} is set: this process answers from a "
        "scripted transcript, NOT from a language model. Every reply is "
        "a fixture for the browser e2e lane. Unset the variable to boot "
        "the configured provider."
    )
    warnings.warn(message, stacklevel=2)
    logger.warning(message)

    alternate = _FixtureUnit(version="", unit=1, text="")
    if scenario == SCRIPTED_SCENARIO_DOCUMENT_CHOICE:
        # The second candidate is a SECOND FILING, so the choice can only
        # be admitted from pages this run actually read — a candidate no
        # hit backs is dropped by admission, and a scenario that paused
        # with no candidate would be a lane that reports green for a card
        # that never rendered.
        alternate, _pages = _load_fixture_unit(
            mpath,
            rpath,
            document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
            read_span=False,
        )

    model = PacedScriptedChatModel(
        stream_responses=build_scripted_transcript(
            document_id=SCRIPTED_DOCUMENT_ID,
            version=fixture.version,
            unit=fixture.unit,
            unit_text=fixture.text,
            scenario=scenario,
            alternate_document_id=SCRIPTED_ALTERNATE_DOCUMENT_ID,
            alternate_version=alternate.version,
            alternate_unit=alternate.unit,
            alternate_unit_text=alternate.text,
        ),
        delta_seconds=delta_seconds,
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=known_document_ids,
        manifest_path=mpath,
        extracted_root=rpath,
        # The transcript's read_source call is served by the run's own
        # company leg, so the hermetic fake port is built WITH the frozen
        # tree as its readable bodies: a port without them would answer
        # every scripted read ``unknown_locator`` and bounce the gate for
        # a reason unrelated to the code under test. An explicit fixture
        # choice, typed-error at boot when the tree is absent, never a
        # serve-path SSOT (ADR 0046/0047).
        adapter=FakeRetrievePort(
            known_document_ids=known_document_ids,
            pages_by_doc=pages_by_doc,
        ),
    )


__all__ = [
    "DEFAULT_DELTA_PACING_MS",
    "PacedScriptedChatModel",
    "SCRIPTED_ALTERNATE_DOCUMENT_ID",
    "SCRIPTED_BOOT_ENV_VAR",
    "SCRIPTED_DELTA_MS_ENV_VAR",
    "SCRIPTED_DOCUMENT_ID",
    "SCRIPTED_SCENARIOS",
    "SCRIPTED_SCENARIO_BOUNCE",
    "SCRIPTED_SCENARIO_DOCUMENT_CHOICE",
    "SCRIPTED_SCENARIO_ENV_VAR",
    "SCRIPTED_SCENARIO_GROUNDED",
    "ScriptedBootConfigError",
    "ScriptedBootError",
    "ScriptedBootFixtureError",
    "build_scripted_transcript",
    "compile_for_scripted_e2e",
    "scripted_boot_requested",
    "scripted_scenario",
]
