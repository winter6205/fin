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
moment the frozen page text changed. The transcript instead reads the
page it is about to quote out of the committed extracted tree at boot,
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

import logging
import time
import warnings
from pathlib import Path
from typing import Any, Iterator, Mapping

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from pydantic import Field

from fin.corpus.errors import IngestMissingError
from fin.host.scripted_model import ScriptedChatModel


logger = logging.getLogger(__name__)


# ----- public constants -----


SCRIPTED_BOOT_ENV_VAR = "FIN_SCRIPTED_MODEL"
SCRIPTED_DELTA_MS_ENV_VAR = "FIN_SCRIPTED_DELTA_MS"
SCRIPTED_SCENARIO_ENV_VAR = "FIN_SCRIPTED_SCENARIO"

# Which transcript the lane replays.
#
# ``grounded`` is one model step that answers from the page it just
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
SCRIPTED_SCENARIOS = (SCRIPTED_SCENARIO_GROUNDED, SCRIPTED_SCENARIO_BOUNCE)

# The filing the scripted transcript reads and quotes. Present in the
# frozen manifest since v1 ingest; a corpus without it is a fixture
# error, not a silent switch to some other document.
SCRIPTED_DOCUMENT_ID = "AAPL-2022Q3"

# Milliseconds between streamed chunks. Large enough that a browser
# polling once per second sees the answer grow across ticks, small
# enough that the whole lane run stays under a few seconds.
DEFAULT_DELTA_PACING_MS = 200

# Characters of page text quoted verbatim into the answer. Must clear
# the gate's 32-character minimum span with room to spare.
_QUOTED_SPAN_CHARS = 180

# How many chunks the answer is split into on the wire.
_ANSWER_CHUNKS = 6

# The ungrounded draft the ``bounce`` scenario streams first. It quotes
# nothing from the page, so the gate finds no verbatim span and takes
# the bounce path. Long enough that the browser sees real prose in the
# bubble before the retry replaces it — a one-word draft would make the
# no-blank invariant untestable.
_UNGROUNDED_DRAFT = (
    "From memory, Apple's quarterly revenue that period was roughly "
    "eighty-three billion dollars, though I have not opened the filing "
    "to confirm the exact printed figure."
)

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
    :data:`SCRIPTED_DOCUMENT_ID`, or its first page is shorter than the
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
    bounced.
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


def build_scripted_transcript(
    *,
    document_id: str,
    version: str,
    page_text: str,
    scenario: str = SCRIPTED_SCENARIO_GROUNDED,
) -> list[list[AIMessageChunk]]:
    """Return the streamed responses one scripted run replays.

    Response 1 is the research step: reasoning deltas plus a
    ``read_file`` tool call split across chunks (exactly the shape the
    live provider streams). The last response is the answer step: one
    more reasoning delta, then the grounded answer split into
    :data:`_ANSWER_CHUNKS` content chunks so the wire carries deltas
    rather than a dump.

    ``scenario=`` :data:`SCRIPTED_SCENARIO_BOUNCE` inserts an
    ungrounded answer step between the two, which the citation gate
    refuses; the grounded step then runs as the post-bounce retry.
    """
    if scenario not in SCRIPTED_SCENARIOS:
        raise ScriptedBootConfigError(
            f"scenario={scenario!r} names no scripted transcript; use "
            f"one of {list(SCRIPTED_SCENARIOS)}."
        )
    quote = page_text[:_QUOTED_SPAN_CHARS]
    answer = f"Per {document_id} page 1: {quote}"
    args = (
        f'{{"document_id": "{document_id}", "version": "{version}", '
        '"page": 1, "page_limit": 1}'
    )
    draft_step: list[list[AIMessageChunk]] = []
    if scenario == SCRIPTED_SCENARIO_BOUNCE:
        draft_step = [
            [
                AIMessageChunk(content=part)
                for part in _split(_UNGROUNDED_DRAFT, _ANSWER_CHUNKS)
            ]
        ]
    return [
        [
            AIMessageChunk(
                content="",
                additional_kwargs={
                    "reasoning_details": [
                        {"text": f"Reading {document_id} page 1 "}
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
                        "name": "read_file",
                        "args": "",
                        "id": "scripted-read-1",
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
        ],
        *draft_step,
        [
            AIMessageChunk(
                content="",
                additional_kwargs={
                    "reasoning_details": [
                        {"text": " Quoting the page verbatim."}
                    ]
                },
            ),
            *[
                AIMessageChunk(content=part)
                for part in _split(answer, _ANSWER_CHUNKS)
            ],
        ],
    ]


def _load_fixture_page(
    manifest_path: Path, extracted_root: Path
) -> tuple[str, str, set[str]]:
    """Return ``(version, page_text, known_document_ids)`` for the lane."""
    from fin.corpus.extracted import load_extracted_tree

    try:
        tree = load_extracted_tree(manifest_path, extracted_root)
    except (IngestMissingError, OSError) as exc:
        raise ScriptedBootFixtureError(
            "the scripted e2e transcript needs the committed extracted "
            f"tree under {extracted_root}: {exc}"
        ) from exc

    pages = tree.get(SCRIPTED_DOCUMENT_ID)
    if not pages:
        raise ScriptedBootFixtureError(
            f"the frozen corpus has no {SCRIPTED_DOCUMENT_ID}; the "
            "scripted e2e transcript quotes that filing."
        )
    page = pages[0]
    if len(page.text) < _QUOTED_SPAN_CHARS:
        raise ScriptedBootFixtureError(
            f"{SCRIPTED_DOCUMENT_ID} page 1 has {len(page.text)} characters; "
            f"the scripted transcript quotes {_QUOTED_SPAN_CHARS}."
        )
    return page.locator.version, page.text, set(tree)


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
    no ``classify_model`` — the rules tier already routes the lane's
    fully-slotted question to ``research`` and an LLM classify call
    would consume transcript responses meant for the run itself.

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

    from fin.host.graph import RETRIEVE_ADAPTER_FAKE, compile_graph

    delta_seconds = _delta_seconds(env)
    scenario = scripted_scenario(env)
    mpath = Path(manifest_path) if manifest_path else _DEFAULT_MANIFEST_PATH
    rpath = Path(extracted_root) if extracted_root else _DEFAULT_EXTRACTED_ROOT
    version, page_text, known_document_ids = _load_fixture_page(mpath, rpath)

    message = (
        f"{SCRIPTED_BOOT_ENV_VAR} is set: this process answers from a "
        "scripted transcript, NOT from a language model. Every reply is "
        "a fixture for the browser e2e lane. Unset the variable to boot "
        "the configured provider."
    )
    warnings.warn(message, stacklevel=2)
    logger.warning(message)

    model = PacedScriptedChatModel(
        stream_responses=build_scripted_transcript(
            document_id=SCRIPTED_DOCUMENT_ID,
            version=version,
            page_text=page_text,
            scenario=scenario,
        ),
        delta_seconds=delta_seconds,
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        known_document_ids=known_document_ids,
        manifest_path=mpath,
        extracted_root=rpath,
        retrieve_adapter=RETRIEVE_ADAPTER_FAKE,
    )


__all__ = [
    "DEFAULT_DELTA_PACING_MS",
    "PacedScriptedChatModel",
    "SCRIPTED_BOOT_ENV_VAR",
    "SCRIPTED_DELTA_MS_ENV_VAR",
    "SCRIPTED_DOCUMENT_ID",
    "SCRIPTED_SCENARIOS",
    "SCRIPTED_SCENARIO_BOUNCE",
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
