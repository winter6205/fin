"""No unadmitted model text on the wire, and one projection on every surface.

GE-3 (specs/qa-chain-grounded-experience.md:95): "Capture SSE to prove no
unadmitted text leaked before fallback." The leak this pins is a STREAM
leak, not a terminal one: the ``model`` node authors
``prose[[FIN_DOCUMENT_CHOICES]]{...}`` and its ``AIMessageChunk`` deltas
reach the browser immediately, while admission (``parse_decision_output``
then ``admit_decision``) only runs afterwards in
``document_choice_admit_node``. On the choice path the run pauses and no
terminal frame ever arrives, so the leaked deltas are the entire answer the
client sees. On the malformed-payload path the fallback publishes the
verifier's text of the ORIGINAL output — terminator and all — which the
terminal frame then streams verbatim.

Both tests assert on the RAW SSE body rather than on the parsed events, so
a leak cannot hide in a field the parser skips.

GE-5 (spec line 97): "Browser/SSE/history/export use the same admitted
projection." The projection is not an answer rewriter — it only filters
representations — so admitting the same bytes to SSE, history and export is
what makes the three surfaces agree. The tests below pin that the terminal
SSE answer, the persisted history row and the exported answer are the SAME
projected string, and that all three are clean.

A terminator cut across two transport chunks must still be caught, which is
why the streamed path holds a suffix back rather than testing each delta.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from fin.corpus.extracted import Locator
from fin.host import ScriptedChatModel, compile_graph
from fin.host.grounded.grounded_decision import CANDIDATE_TERMINATOR
from fin.host.grounded.readable_projection import (
    HIDDEN_MARK,
    POLICY_REFUSAL_COPY,
    leaked_fragments,
    project_admitted_answer,
)
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort

GROUNDED = "What was AAPL net sales in 2023 Q3?"
DOC_A = "AAPL-2023Q3"
DOC_B = "AAPL-2022Q3"
KNOWN_IDS = {DOC_A, DOC_B, "MSFT-2023Q1"}

DOC_A_TEXT = (
    "Section 1. Apple reported net sales of 89,493 million dollars for the "
    "fiscal 2023 fourth quarter."
)
DOC_B_TEXT = (
    "Section 1. Apple reported net sales of 82,959 million dollars for the "
    "fiscal 2022 fourth quarter."
)

# The grounded span the verifier can admit. Deliberately carries no
# internal label, so it survives the projection byte-for-byte and the
# parity assertions below stay about WIRE equality, not about filtering.
ADMITTED_ANSWER = "Apple reported net sales of 89,493 million dollars."


def _chunk(document_id: str, text: str, unit: int) -> Chunk:
    return Chunk(
        text=text,
        source_ref=Locator(document_id=document_id, version="v" * 8, unit=unit),
        score=1.0,
    )


def _choice_output(*document_ids: str) -> str:
    payload = {"candidates": [{"document_id": doc} for doc in document_ids]}
    return f"这个问题有两种理解。{CANDIDATE_TERMINATOR}{json.dumps(payload)}"


def _malformed_output() -> str:
    """A terminator whose payload does not parse.

    The admission lane takes the ORIGINAL output — terminator and all —
    through the verifier on this path, so the verifier's answer is the
    only thing standing between the payload and the terminal frame.
    """
    return f"{ADMITTED_ANSWER} {CANDIDATE_TERMINATOR}not json"


def _compile(answer: str, *, answer_chunks: list[str] | None = None) -> Any:
    """A two-call scripted run, STREAMED.

    The workspace reads ``stream()``, so a script of whole ``AIMessage``s
    never exercises the token-by-token path where the leak lives. Every
    test here streams; ``answer_chunks`` controls where the transport
    cuts the model's output.
    """
    from langchain_core.messages import AIMessageChunk

    pieces = answer_chunks if answer_chunks is not None else [answer]
    model = ScriptedChatModel(
        script=[AIMessage(content=""), AIMessage(content=answer)],
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "retrieve",
                            "args": json.dumps({"query": GROUNDED}),
                            "id": "c-retrieve-1",
                            "index": 0,
                        }
                    ],
                ),
            ],
            [AIMessageChunk(content=piece) for piece in pieces],
        ],
    )
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(
            hits_by_query={
                GROUNDED: [
                    _chunk(DOC_A, DOC_A_TEXT, 1),
                    _chunk(DOC_B, DOC_B_TEXT, 1),
                ]
            },
            known_document_ids=set(KNOWN_IDS),
        ),
        known_document_ids=set(KNOWN_IDS),
    )


@pytest.fixture
def http_app() -> Any:
    from fin.host.http import app

    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(http_app: Any) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(http_app)


def _install_graph(http_app: Any, answer: str, **kwargs: Any) -> Any:
    from fin.host.http import get_graph

    g = _compile(answer, **kwargs)
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _install_typed_refusal_graph(http_app: Any, provider_prose: str) -> Any:
    from fin.host.http import get_graph

    model = ScriptedChatModel(
        stream_script=[
            AIMessageChunk(
                content=provider_prose,
                additional_kwargs={"refusal": "PRIVATE REFUSAL METADATA"},
            )
        ]
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(known_document_ids=set(KNOWN_IDS)),
        known_document_ids=set(KNOWN_IDS),
    )
    http_app.dependency_overrides[get_graph] = lambda: g
    return g


def _raw_body(client: Any, thread_id: str, question: str) -> str:
    with client.stream(
        "POST",
        f"/api/threads/{thread_id}/messages",
        json={"question": question},
    ) as response:
        assert response.status_code == 200, response.status_code
        return "".join(response.iter_text())


def _new_thread(client: Any) -> str:
    return client.post("/api/threads", json={"first_message": "研究"}).json()["id"]


def _sse_frame_texts(body: str) -> list[str]:
    """Every string a client could render out of this body.

    ``data:`` lines are JSON, so the terminator inside a JSON string value
    shows up escaped as ``\\u3010``-free plain text for our ASCII
    terminator; scanning the raw body is the honest check and parsing the
    answer frames is the precise one. Both are done by the callers.
    """
    return [line for line in body.splitlines() if line.startswith("data:")]


def _event_frames(body: str) -> list[tuple[str, dict[str, Any]]]:
    """``(event, data)`` for every frame, in wire order."""
    frames: list[tuple[str, dict[str, Any]]] = []
    event = ""
    for line in body.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            try:
                parsed = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                frames.append((event, parsed))
    return frames


def _answer_frame_texts(body: str) -> list[str]:
    """Every ``answer`` frame's text — and ONLY answer frames.

    Named events, not "any frame with a ``text`` key": a ``thinking``
    frame carries a ``text`` too, so a positional helper would scan the
    scratchpad region as if the operator were reading it, and a leak
    assertion over it would either fail for the wrong reason or (worse)
    pass while the answer stream leaks.
    """
    return [
        data["text"]
        for event, data in _event_frames(body)
        if event == "answer" and isinstance(data.get("text"), str)
    ]


def _streamed_answer_texts(body: str) -> list[str]:
    """The ``answer`` frames that arrived BEFORE ``status``.

    The terminal answer frame is the authoritative projection; the frames
    before it are the ones a reader watched paint. Asserting leak-freedom
    per frame has to mean per frame the live bubble actually painted, so
    the terminal frame is excluded and pinned separately.
    """
    texts: list[str] = []
    for event, data in _event_frames(body):
        if event == "status":
            break
        if event == "answer" and isinstance(data.get("text"), str):
            texts.append(data["text"])
    return texts


def _assert_no_frame_leaks(frames: list[str]) -> None:
    """Per frame AND across the join — both, because they fail differently.

    The per-frame scan catches an unfiltered live view: the reader saw that
    exact frame, and naming it names the frame they saw it on.

    The join catches what per-frame cannot: a hold-back so eager or so
    lazy that the fragment the seam held back lands SPLIT across two
    released frames, where neither half matches any rule. A suite with
    only the per-frame scan passes that, because both halves are
    individually clean — the join is what makes the fragment whole again,
    which is the same reason the projection joins before it scans.

    Both assertions, so neither defect class can pass.
    """
    for index, text in enumerate(frames):
        assert not leaked_fragments(text), (
            f"answer frame {index} leaked {leaked_fragments(text)!r}: {text!r}"
        )
    joined = "".join(frames)
    assert not leaked_fragments(joined), (
        f"the streamed frames join to a leak {leaked_fragments(joined)!r}: "
        f"{joined!r}"
    )


# ----- GE-3: the choice path -----

CHOICE_PROSE = "这个问题有两种理解。"


def test_the_choice_path_streams_no_terminator_or_payload(
    client: Any, http_app: Any
) -> None:
    """Nothing the model wrote past the choice point reaches the browser.

    The raw body is the evidence: neither the terminator nor the JSON
    payload it introduces may appear on ANY frame — not the streamed
    delta, not a terminal frame. The operator reads the choice card
    instead, which carries the candidate ids it needs by design.
    """
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    assert CANDIDATE_TERMINATOR not in body
    assert '{"candidates"' not in body
    # The decision PROSE is admitted — only the payload is withheld, so
    # a projection that simply emptied the answer would not pass here.
    assert CHOICE_PROSE in "".join(_answer_frame_texts(body))


def test_provider_typed_refusal_never_reaches_sse_or_history(
    client: Any, http_app: Any
) -> None:
    """A typed refusal closes chat safely and cannot leak its model delta."""
    provider_prose = "PRIVATE REFUSAL BODY MUST NOT BE SHOWN"
    _install_typed_refusal_graph(http_app, provider_prose)
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, "Hello!")

    status = next(
        data for event, data in _event_frames(body) if event == "status"
    )
    assert status["status"] == "refuse"
    assert status["reason"] == "policy_refuse"
    assert status["assistant_message"] == POLICY_REFUSAL_COPY
    assert _answer_frame_texts(body) == [""]
    assert provider_prose not in body
    assert "PRIVATE REFUSAL METADATA" not in body

    detail = client.get(f"/api/threads/{thread_id}").json()
    turns = [row for row in detail["turns"] if row.get("status") == "refuse"]
    assert len(turns) == 1
    assert turns[0]["answer"] == ""
    assert turns[0]["assistant_message"] == POLICY_REFUSAL_COPY


def test_the_choice_path_frames_carry_no_forbidden_label(
    client: Any, http_app: Any
) -> None:
    """The streamed prose on the choice path is itself leak-free.

    ``document_id`` is a forbidden label in any corpus, and the payload it
    rides in is exactly where it would otherwise appear. The scan runs on
    the ANSWER frames rather than the whole body because the
    ``document_choice`` frame legitimately names candidate ids by
    contract (frozen wire shape, specs/question-task-runtime-contract.md).
    """
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    _assert_no_frame_leaks(_answer_frame_texts(body))


def test_the_choice_path_frame_is_present_and_uncontracted(
    client: Any, http_app: Any
) -> None:
    """The pause withholds the payload, not the feature.

    The ``document_choice`` frame keeps its frozen shape and its candidate
    choices, so a client that renders the choice card is unaffected.
    """
    _install_graph(http_app, _choice_output(DOC_A, DOC_B))
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    assert "event: document_choice" in body
    marker = "event: document_choice\ndata: "
    payload = json.loads(body.split(marker, 1)[1].splitlines()[0])
    assert payload["kind"] == "document_choice"
    assert payload["purpose"] == "document_choice"
    assert payload["choices"]


# ----- GE-3: the malformed-output fallback path -----

FALLBACK_STATUS = "done"


def _fallback_status(body: str) -> str:
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            parsed = json.loads(line[len("data:"):].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "run_id" in parsed and "status" in parsed:
            return str(parsed["status"])
    return ""


def test_the_malformed_fallback_path_streams_no_terminator_or_payload(
    client: Any, http_app: Any
) -> None:
    """The admitted fallback answer is the verifier's text, not the raw
    payload.

    On this path the verifier is handed the ORIGINAL model output, so its
    verdict's answer text is the one place the unadmitted bytes can still
    escape — via the terminal answer frame. The scan is over the whole
    body so neither the streamed delta nor the terminal frame can carry
    them.
    """
    _install_graph(http_app, _malformed_output())
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    assert _fallback_status(body) == FALLBACK_STATUS
    assert CANDIDATE_TERMINATOR not in body
    assert "not json" not in body


def test_the_malformed_fallback_publishes_the_admitted_answer(
    client: Any, http_app: Any
) -> None:
    """Refusing to show the payload must not cost the answer.

    The fallback still merges, still cites, and still shows the operator
    the verified prose — with the unadmitted suffix removed. A fix that
    blanked the answer would satisfy the leak assertions above and fail
    this one.
    """
    _install_graph(http_app, _malformed_output())
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    terminal = _answer_frame_texts(body)[-1]
    assert ADMITTED_ANSWER in terminal
    _assert_no_frame_leaks(_answer_frame_texts(body))


# ----- the hold-back, not the per-chunk filter -----

SPLIT_CHUNKS = (
    "这个问题有两种理解。[[FIN_DOCUMENT",
    "_CHOICES]]{\"candidates\": [{\"document_id\": \"AAPL-2023Q3\"}]}",
)


def test_the_terminator_split_across_two_deltas_is_still_withheld(
    client: Any, http_app: Any
) -> None:
    """A transport may cut the terminator anywhere.

    A filter applied to each delta would see ``[[FIN_DOCUMENT`` — which
    matches nothing — and let the payload through. The streamed path
    therefore holds back a possible partial terminator suffix and only
    releases text once it can no longer become one, exactly like the
    hold-back the inline-tag filter already does.
    """
    answer = "".join(SPLIT_CHUNKS)
    _install_graph(http_app, answer, answer_chunks=list(SPLIT_CHUNKS))
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    assert CANDIDATE_TERMINATOR not in body
    assert '{"candidates"' not in body
    _assert_no_frame_leaks(_answer_frame_texts(body))


def test_the_withheld_prose_is_still_streamed_when_no_payload_follows(
    client: Any, http_app: Any
) -> None:
    """Hold-back must not swallow the answer on an ordinary run.

    A buffer that only flushes on a terminator would blank every normal
    answer. The flushed prefix is checked here: the ordinary grounded
    answer reaches the client unchanged.
    """
    _install_graph(http_app, ADMITTED_ANSWER)
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    assert ADMITTED_ANSWER in "".join(_answer_frame_texts(body))


# ----- GE-5: one projection on SSE, history and export -----

LEAKY_PROSE = (
    "Apple reported net sales of 89,493 million dollars per document_id "
    "AAPL-2023Q3."
)


def _citation_frame(body: str) -> list[dict[str, Any]]:
    """The terminal ``citations`` frame's rows.

    Parsed by NAMED event rather than through the shared ``_event_frames``
    helper, which only keeps dict payloads — and the citation frame's data
    is a JSON ARRAY. A helper that silently skipped it would make any
    comparison against this surface vacuous, which is the defect this
    file's parity test previously had.
    """
    rows: list[dict[str, Any]] = []
    event = ""
    for line in body.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:") and event == "citations":
            parsed = json.loads(line[len("data:") :].strip())
            if isinstance(parsed, list):
                rows.extend(row for row in parsed if isinstance(row, dict))
    return rows


def test_sse_history_and_export_agree_on_one_projected_answer(
    client: Any, http_app: Any
) -> None:
    """The three surfaces project once, so they cannot disagree.

    GE-5 requires browser, SSE, history and export to use the SAME
    admitted projection. Equality is the assertion that matters: if any
    surface filtered while another passed bytes through, the operator
    would read a different answer in the bubble than in the history row
    than in the export. The BROWSER surface is the streamed join, so
    equality is asserted against it too — a stream that projected only
    on its terminal frame would make the bubble and the history row
    disagree while this test still passed.

    The export half asserts the contract the export ACTUALLY has, which
    is why this is not an answer comparison. The export is a citation or
    comparison bundle, not a transcript: its document is
    ``{thread_id, run_id, kind, citations|comparisons,
    retrieval_window}`` (``fin.host.export._json_body``), and
    specs/enterprise-kb-agent.md:51 scopes it as 「Export of a comparison
    or citation bundle as a file download from the thread」. Nothing there
    authorizes an answer field, so adding one would be inventing a
    requirement rather than testing one. What GE-5 DOES put on the export
    is 「the same admitted presentation」, and the export's share of the
    answer's presentation is its citation rows — so those are what must
    match, byte for byte, against the rows the answer they support was
    admitted with.

    The previous version of this assertion was
    ``run.get("answer", terminal) == terminal`` over a payload with no
    ``answer`` key, so it compared the default against itself and could
    not fail for any implementation. These three assertions can: the
    export is required to carry no leak and no unprojected answer text
    (its own projection), and its rows are required to equal the admitted
    rows the streamed answer cites.
    """
    _install_graph(http_app, LEAKY_PROSE)
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)
    terminal = _answer_frame_texts(body)[-1]

    detail = client.get(f"/api/threads/{thread_id}").json()
    turns = [row for row in detail["turns"] if row.get("answer")]
    assert len(turns) == 1
    persisted = turns[0]["answer"]
    admitted_citations = turns[0]["citations"]

    # Browser/SSE/history: the same projected answer on both wire and
    # replay. This half is unchanged; it is the half that WAS real.
    assert terminal == persisted
    # The streamed citations frame is what the answer's inline markers
    # resolve against, and it is the admitted row set the export must
    # match below. ``section`` is added by the citation wire projection
    # for the readable source tail, so it is compared off — it is a
    # presentation label the export does not carry, not an identity.
    streamed = [
        {k: v for k, v in row.items() if k != "section"}
        for row in _citation_frame(body)
    ]
    assert streamed == admitted_citations

    exported = client.get(
        f"/api/threads/{thread_id}/export",
        params={"format": "json", "kind": "citations"},
    )
    assert exported.status_code == 200, exported.text
    export_payload = json.loads(exported.text)

    # (1) The export's own body is projected: neither the raw leaky prose
    # nor the unprojected label survives on it. Without this, an export
    # that rendered the run's raw record would satisfy every other
    # assertion here while shipping the leak to a file the operator
    # downloads and forwards.
    assert LEAKY_PROSE not in exported.text
    assert "per document_id" not in exported.text
    # (2) The export's citation rows are the ADMITTED rows, not a
    # re-derivation: same order, same evidence bytes, same identities.
    assert export_payload["citations"] == admitted_citations
    # (3) The export carries no answer, so a future addition cannot
    # quietly introduce a second copy of the answer that bypassed
    # ``_admitted_answer_text``. The export's answer is its citations.
    assert "answer" not in export_payload


def test_live_sse_replaces_admitted_source_id_and_keeps_navigation(
    client: Any, http_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every live answer frame and replay uses the trusted source label.

    The raw model prose contains a document ID split across model deltas.
    Projection happens only after admission joins those deltas; citation
    identity remains unchanged for verified source navigation.
    """
    from fin.host import run_payload

    monkeypatch.setattr(run_payload, "corpus_is_finance", lambda: True)
    monkeypatch.setattr(
        run_payload,
        "list_filings",
        lambda **_kwargs: {
            "filings": [
                {
                    "document_id": DOC_A,
                    "company": "Apple Inc.",
                    "period": "2023 Q3",
                    "version": "v" * 8,
                    "page_count": 4,
                    "form": "10-Q",
                }
            ]
        },
    )
    answer_chunks = ["Per AAPL-202", "3Q3 page 1: ", DOC_A_TEXT]
    _install_graph(
        http_app,
        "".join(answer_chunks),
        answer_chunks=answer_chunks,
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)
    answer_frames = _answer_frame_texts(body)
    terminal = answer_frames[-1]
    detail = client.get(f"/api/threads/{thread_id}").json()
    turns = [row for row in detail["turns"] if row.get("answer")]

    assert len(answer_frames) >= 2
    assert len(turns) == 1
    persisted = turns[0]["answer"]
    for frame in answer_frames:
        lowered = frame.lower()
        assert DOC_A not in frame
        assert "document_id" not in lowered
        assert "list_files" not in lowered
        assert "10-q" not in lowered
        assert "xbrl" not in lowered
    joined = "".join(answer_frames)
    assert DOC_A not in joined
    assert "Apple Inc. · 2023 Q3" in terminal
    assert "Apple reported net sales of 89,493 million dollars" in terminal
    assert terminal == persisted

    # The presentation changes; the admitted source locator does not.
    citation = turns[0]["citations"][0]
    assert citation["document_id"] == DOC_A
    assert citation["version"] == "v" * 8
    assert citation["unit"] == 1
    streamed_citation = _citation_frame(body)[0]
    assert streamed_citation["document_id"] == DOC_A
    assert streamed_citation["version"] == "v" * 8
    assert streamed_citation["unit"] == 1


def test_the_projection_leaves_the_answer_readable_not_empty(
    client: Any, http_app: Any
) -> None:
    """The projection replaces a leak; it does not delete the sentence.

    Without this the parity test above would also pass on an empty
    answer, and a broken "fix" that dropped the response entirely would
    look like a pass.
    """
    _install_graph(http_app, LEAKY_PROSE)
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)
    terminal = _answer_frame_texts(body)[-1]

    assert "Apple reported net sales of 89,493 million dollars" in terminal
    _assert_no_frame_leaks(_answer_frame_texts(body))


def test_the_projection_agrees_with_the_shared_entry_point(
    client: Any, http_app: Any
) -> None:
    """The surfaces must use the ONE admitted projection, not a second
    filter that happens to agree today.

    ``project_admitted_answer`` is the stream-safe entry: it joins the
    chunks before scanning, so a split leak cannot survive it. If the
    surfaces grew their own copy this assertion would diverge the moment
    one of them gained a rule.
    """
    _install_graph(http_app, LEAKY_PROSE)
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)
    terminal = _answer_frame_texts(body)[-1]
    from fin.host import run_payload

    assert terminal == run_payload._admitted_answer_text(
        LEAKY_PROSE, citations=_citation_frame(body)
    )
    assert DOC_A not in terminal


# ----- the leak the live view painted before the terminal frame -----
#
# T10 acceptance (plans/qa-chain-04-grounded-experience.md): "Inject a leak
# split across SSE chunks and confirm no prohibited fragment reaches the
# default view; final replacement is insufficient." The transport chooses
# where a chunk boundary falls, so the split below is a legal cut of one
# answer — there is no framing around it that the model did not write.

# ``document_`` | ``id AAPL-2023Q3`` — the cut lands INSIDE the label, so
# neither half matches a rule. A per-delta filter passes both through.
SPLIT_LEAK_CHUNKS = (
    "Apple reported net sales of 89,493 million dollars per document_",
    "id AAPL-2023Q3 for the quarter.",
)

# ``source_`` | ``ref: {`` | ``"doc": "AAPL-2023Q3"}`` — three cuts inside
# the label and its assigned value. The value charset regex accepts
# non-space, so a fragment split mid-value must reassemble too.
TRIPLE_SPLIT_LEAK_CHUNKS = (
    "The row is filed under source_",
    "ref: ",
    '{"doc": "AAPL-2023Q3"} and that is the whole citation.',
)


def _streamed_frames(body: str) -> list[str]:
    streamed = _streamed_answer_texts(body)
    assert streamed, "the run streamed no answer frame before status"
    return streamed


def test_a_leak_split_across_two_chunks_reaches_no_live_frame(
    client: Any, http_app: Any
) -> None:
    """The live view never paints a fragment the terminal frame removes.

    This is the defect under test: the seam filtered only the terminator,
    so the reader watched ``document_id AAPL-2023Q3`` paint token by token
    and then saw the terminal frame replace it with the redacted copy —
    one run, two different answers, the raw one first. Asserting on the
    joined text or the LAST frame passes while that happens, so the
    assertion is per frame and the streamed frames are the ones that
    matter.
    """
    _install_graph(
        http_app,
        "".join(SPLIT_LEAK_CHUNKS),
        answer_chunks=list(SPLIT_LEAK_CHUNKS),
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    _assert_no_frame_leaks(_streamed_frames(body))
    # The terminal frame is not the fix and must not be mistaken for it.
    _assert_no_frame_leaks(_answer_frame_texts(body))


def test_a_leak_split_across_three_chunks_reaches_no_live_frame(
    client: Any, http_app: Any
) -> None:
    """Two cuts inside one label: the hold-back must survive both.

    A hold-back sized for one chunk releases ``source_ref`` after the
    second delta and prints the value before the third closes it, so the
    bound the hold-back honors has to be the LONGEST forbidden label, not
    the length of one example.
    """
    _install_graph(
        http_app,
        "".join(TRIPLE_SPLIT_LEAK_CHUNKS),
        answer_chunks=list(TRIPLE_SPLIT_LEAK_CHUNKS),
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    streamed = _streamed_answer_texts(body)
    assert streamed == []
    assert "source_ref" not in body
    assert '"doc": "AAPL-2023Q3"' not in body
    status = next(data for event, data in _event_frames(body) if event == "status")
    assert status["status"] == "refuse"
    assert _answer_frame_texts(body) == [""]


def test_the_split_leak_is_replaced_not_deleted_on_the_live_frames(
    client: Any, http_app: Any
) -> None:
    """A projection that blanks the stream satisfies the two above.

    The whole point of the hold-back is that it delays a fragment and
    still delivers readable prose, so this pins both halves: the mark is
    present and the surrounding sentence survives, on the LIVE frames,
    not only on the terminal one.
    """
    _install_graph(
        http_app,
        "".join(SPLIT_LEAK_CHUNKS),
        answer_chunks=list(SPLIT_LEAK_CHUNKS),
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    streamed = "".join(_streamed_frames(body))
    assert HIDDEN_MARK in streamed
    assert "Apple reported net sales of 89,493 million dollars" in streamed
    assert "for the quarter." in streamed


def test_the_stream_still_grows_while_a_leak_is_held_back(
    client: Any, http_app: Any
) -> None:
    """Hold-back must not stall or blank a stream.

    A fix that buffered until the end would pass every leak assertion and
    break the pacing the browser lane measures (``growthSteps``,
    ``answerBeforeDone``). The prose before the leak must arrive as its
    own frame, i.e. the answer is delivered incrementally and the answer
    is already non-empty while the fragment is still withheld.
    """
    _install_graph(
        http_app,
        "".join(SPLIT_LEAK_CHUNKS),
        answer_chunks=list(SPLIT_LEAK_CHUNKS),
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    frames = _streamed_frames(body)
    assert len(frames) >= 2, f"the answer did not stream in steps: {frames!r}"
    assert "Apple reported net sales of 89,493 million dollars" in frames[0]


def test_an_ordinary_answer_grows_across_frames_unchanged(
    client: Any, http_app: Any
) -> None:
    """The per-frame projection must not disturb a clean run.

    A clean answer is not identical byte-for-byte across frames once a
    fragment is withheld, but every frame is a prefix of the final text
    and the streamed join equals the terminal projection — so the browser
    bubble and the history row carry the same sentence. The join is over
    the STREAMED frames: the terminal frame restates the whole answer, so
    joining it in would count it twice.
    """
    _install_graph(
        http_app,
        ADMITTED_ANSWER,
        answer_chunks=[
            "Apple reported ",
            "net sales of 89,493 ",
            "million dollars.",
        ],
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    frames = _streamed_frames(body)
    assert len(frames) >= 2, f"the answer did not stream in steps: {frames!r}"
    _assert_no_frame_leaks(_answer_frame_texts(body))
    assert "".join(frames) == ADMITTED_ANSWER
    assert _answer_frame_texts(body)[-1] == ADMITTED_ANSWER


def test_the_split_leak_reaches_no_surface_at_all(
    client: Any, http_app: Any
) -> None:
    """GE-5 on the split path: browser, history and export are one string.

    The parity the existing GE-5 test pins on an UNSPLIT leak has to hold
    for the split one too. When it does not, the operator reads the raw
    ``document_id`` in the bubble and the redacted copy in the history row
    — the disagreement the requirement exists to prevent, reached by a
    route the terminal-frame-only version of that test never took.
    """
    _install_graph(
        http_app,
        "".join(SPLIT_LEAK_CHUNKS),
        answer_chunks=list(SPLIT_LEAK_CHUNKS),
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)
    streamed = "".join(_streamed_frames(body))
    terminal = _answer_frame_texts(body)[-1]

    detail = client.get(f"/api/threads/{thread_id}").json()
    turns = [row for row in detail["turns"] if row.get("answer")]
    assert len(turns) == 1
    persisted = turns[0]["answer"]

    assert streamed == terminal, "the bubble and the history row disagree"
    assert terminal == persisted
    assert not leaked_fragments(terminal)



# ----- a bounce discards the abandoned attempt's withheld tail -----

BOUNCE_DRAFT = "Apple reported it per document_"
BOUNCE_RETRY_CHUNKS = (
    "Apple reported net sales of ",
    "89,493 million dollars.",
)


def test_a_bounce_discards_the_abandoned_attempts_withheld_tail(
    client: Any, http_app: Any
) -> None:
    """A retry frame presents the retry's OWN prose, not the bytes the
    abandoned attempt never finished writing.

    The seam withholds an ambiguous suffix (``document_`` here) so a
    fragment never reaches the wire. When the citation gate BOUNCES that
    attempt, the withheld suffix belongs to prose the run threw away; the
    ``reset: true`` frame starts the retry, so carrying the suffix into it
    prints the abandoned attempt's last characters at the head of the new
    one — the reader sees ``document_Apple reported net sales of ...``.
    """
    from langchain_core.messages import AIMessageChunk

    model = ScriptedChatModel(
        script=[AIMessage(content=""), AIMessage(content="")],
        stream_responses=[
            [
                AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "retrieve",
                            "args": json.dumps({"query": GROUNDED}),
                            "id": "c-retrieve-1",
                            "index": 0,
                        }
                    ],
                ),
            ],
            [AIMessageChunk(content=BOUNCE_DRAFT)],
            [AIMessageChunk(content=piece) for piece in BOUNCE_RETRY_CHUNKS],
        ],
    )
    g = compile_graph(
        checkpointer=InMemorySaver(),
        model=model,
        adapter=FakeRetrievePort(
            hits_by_query={
                GROUNDED: [
                    _chunk(DOC_A, DOC_A_TEXT, 1),
                    _chunk(DOC_B, DOC_B_TEXT, 1),
                ]
            },
            known_document_ids=set(KNOWN_IDS),
        ),
        known_document_ids=set(KNOWN_IDS),
    )
    from fin.host.http import get_graph

    http_app.dependency_overrides[get_graph] = lambda: g
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)

    reset_frames = [
        data
        for event, data in _event_frames(body)
        if event == "answer" and data.get("reset") is True
    ]
    assert reset_frames, "the bounce emitted no retry frame"
    assert reset_frames[0]["text"] == BOUNCE_RETRY_CHUNKS[0], reset_frames
    _assert_no_frame_leaks(_streamed_frames(body))




def test_a_benign_label_prefix_at_stream_end_still_reaches_the_live_view(
    client: Any, http_app: Any
) -> None:
    """The hold-back is not a truncation.

    An answer that ends in a word which happens to be a proper prefix of a
    forbidden label (``ret`` here) is withheld while more text could still
    arrive. At stream end no more text can, so the suffix can never grow
    into a label — dropping it would leave the live bubble a prefix of the
    persisted copy, exactly the live-vs-history disagreement the projection
    exists to prevent.
    """
    answer = ADMITTED_ANSWER + " we ret"
    _install_graph(
        http_app,
        answer,
        answer_chunks=[ADMITTED_ANSWER + " we ", "ret"],
    )
    thread_id = _new_thread(client)

    body = _raw_body(client, thread_id, GROUNDED)
    streamed = "".join(_streamed_frames(body))
    terminal = _answer_frame_texts(body)[-1]

    assert streamed == terminal
    assert streamed.endswith("we ret")
    _assert_no_frame_leaks(_answer_frame_texts(body))

