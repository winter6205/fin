"""T39 (EP3/EP4): the **behavioural** half of the private-route acceptance.

Why this file exists
--------------------
``test_live_t39_private_route_tools_list.py`` freezes T39's exclusion requirement
against the *declared* surface: the eight tool names, the per-tool top-level
``inputSchema`` property keys, and the ``retrieve.filters`` member verbatim. A
five-axis B2 review read that freeze and found it has **zero coverage of the
model-reachable structured switch inside ``filters.structured_query``**:

* the frozen ``filters`` schema is
  ``additionalProperties: {anyOf: [string, object]}``, and the object branch
  constrains nothing at all, so deep-equality on the whole member is blind to
  every member that rides *inside* it;
* the driver never sent a single ``tools/call``, so IK's Zod runtime boundary
  (``retrieve.ts``'s ``strictObject``, its ``filters`` refine, and
  ``request.ts``'s closed member set and schema/version token) was never
  touched. Empty arguments, wrong types, unknown members, over-sized values and
  the four private-route request guards were all unobserved.

Both halves are closed here by **behaviour**, not by schema shape. The
discriminating case is
``test_structured_query_refuses_an_evidence_shaped_member``: a real
``tools/call retrieve`` whose ``filters.structured_query`` carries an Evidence
shape. At the bound build it answers the closed structured vocabulary's
``malformed_structured_query``, and that exact code is the guard.

Why a behaviour guard is the only thing that can close it
---------------------------------------------------------
The B2 review's counter-example is the reason. Adding ``evidence_ref`` to the
closed member set at IK ``src/core/structured/request.ts`` — or accepting a
second schema token in ``requireRecognizedSchema`` — changes **only IK-internal**
state. The ``filters`` member IK publishes is produced separately, by
``retrieve.ts``'s own schema, so the published bytes do not move and the
schema-freeze test stays green straight through the change, while
``filters.structured_query`` has silently become a model-reachable Evidence
channel. The mutation record in ``docs/validation/`` carries the observed
before/after for exactly that edit; the point of re-stating it here is that a
guard which cannot go red on that edit is not a guard.

What the frozen codes in this file are
--------------------------------------
Every code below is **observed** on the bound build
(``BOUND_IKNOW_COMMIT``) and then frozen, rather than read off IK's source and
assumed. None of them is "did not raise": each assertion names the exact code,
and the positive controls (``..._still_answers_a_valid_request``) pin the other
direction, so a build that refused *everything* would fail here too.

Two bounds the run actually observed, recorded so the next reader is not
surprised by them:

* ``filters.structured_query`` is refused on the **default** (``graph``) tier
  with the *same* ``malformed_structured_query`` code the closed member set
  produces, because ``request.ts`` checks the host-bound tier before it reads
  the body. The two causes are indistinguishable from outside, so the
  discriminating cases below all pin ``search_mode: sql``, where the tier gate
  passes and the body checks are the only thing that can refuse.
* A private ``corpus_evidence`` dispatch whose ``params`` is not a JSON object
  gets **no response at all** on this build — the request handler's
  ``params: z.unknown()`` receives it and the session simply times out. That is
  a real boundary observation, not a fin-side transport bug, and it is asserted
  as such rather than skipped.

Strict dependency behaviour
---------------------------
Both opt-in marks, and the same runtime build-binding guard the sibling driver
enforces: this file carries ``mcp`` and ``sql_live`` and a live dependency that
cannot be resolved is a named failure, never a skip. The DSN is read by
variable name and only its *shape* is asserted, never its value.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from fin.retrieve.evidence_route import (
    EvidenceIdentityBinding,
    deadline_within,
    identity_env,
)
from fin.retrieve.mcp_session import MCPStdioSession, MCPStdioTransportError

from test_live_t39_private_route_tools_list import (
    BOUND_IKNOW_COMMIT,
    EVIDENCE_RESULT_SCHEMA,
    EXPECTED_TOOL_NAMES,
    IKNOW_ROOT,
    PRIVATE_METHOD,
    _EmbeddingHandler,
    _db_url_for_schema,
    _live_private_route,
    _node_executable,
    _pg_url,
    _schema_name,
    _create_pg_schema,
    _drop_pg_schema,
    _REQUEST_TIMEOUT_SECONDS,
)

from live_iknow_support import EMBED_DIMENSIONS


pytestmark = [pytest.mark.mcp, pytest.mark.sql_live]

#: The closed structured-refusal vocabulary IK publishes as
#: ``iknow.structured_error``. A code outside this set is not a structured
#: refusal at all: it means the request escaped the closed surface and reached
#: some other layer, which is exactly what the T39 guards must never allow.
STRUCTURED_ERROR_SCHEMA = "iknow.structured_error"

#: Every code this file asserts ``retrieve`` returns, frozen as one closed set.
#: An assertion naming a code outside it fails on the vocabulary, not on the
#: message, so a widened or newly-invented token cannot pass unnoticed.
FROZEN_STRUCTURED_CODES: frozenset[str] = frozenset(
    {
        "malformed_structured_query",
        "unsupported_structured_schema",
        "unsupported_subject",
        "unsupported_operation",
        "unsupported_field",
        "unsupported_operator",
        "invalid_value",
        "request_too_large",
        "structured_search_unavailable",
    }
)

#: The one code that must be reachable by a *structurally valid* request. It is
#: the positive control for every refusal above: the request cleared the closed
#: set and the closed vocabulary and stopped at the executor.
STRUCTURED_REACHES_EXECUTOR_CODE = "structured_search_unavailable"

#: The private route's request-contract failure token, frozen from the observed
#: ``parseEvidenceRequest`` refusals (IK ``src/core/storage/evidence-read.ts``).
EVIDENCE_MALFORMED_CODE = "malformed_evidence_contract"
EVIDENCE_MALFORMED_REASON = "unknown_field"

#: The trusted-binding refusal an *unbound* session answers for every request,
#: whatever the body. Recorded because it is the reason the guard below needs a
#: **bound** session: an unbound session never reaches the request guards, so
#: testing them unbound would prove nothing.
EVIDENCE_UNBOUND_CODE = "access_denied"
EVIDENCE_UNBOUND_REASON = "trusted_binding_unresolved"


# ----- shared wire helpers ---------------------------------------------------


def _tools_call(
    live: dict[str, Any],
    request_id: int,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """One real ``tools/call`` on the shared persistent session.

    The frame is written by hand rather than through the adapter so the
    assertion can name the exact argument that provoked the refusal. A fin-side
    adapter that swallows or rewrites an argument would otherwise make the
    server's answer unfalsifiable.
    """

    response = live["session"].call(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        _REQUEST_TIMEOUT_SECONDS,
    )
    assert response.get("id") == request_id, response
    assert "error" not in response, (
        f"tools/call {name} answered with a JSON-RPC error, not a tool result: "
        f"{response}"
    )
    assert isinstance(response.get("result"), dict), response
    return response["result"]


def _tool_text(result: dict[str, Any]) -> str:
    """The single text block of a tool result, or a named failure.

    A tool result with zero or several text blocks is not the documented shape,
    so it fails here rather than yielding an empty string that a substring
    assertion would silently pass against.
    """

    content = result.get("content")
    assert isinstance(content, list) and len(content) == 1, result
    block = content[0]
    assert block.get("type") == "text", block
    text = block.get("text")
    assert isinstance(text, str), block
    return text


def _refused_tool_text(result: dict[str, Any]) -> str:
    """The text of a result that must be an ``isError`` refusal."""

    assert result.get("isError") is True, (
        f"expected a typed refusal, got a success result: {result}"
    )
    return _tool_text(result)


def _structured_refusal_code(result: dict[str, Any]) -> tuple[str, bool]:
    """The ``code`` and ``retryable`` of an ``iknow.structured_error`` result.

    Parsed from the JSON the server published rather than from its prose, so the
    assertion is on the contract a host actually classifies from.
    """

    text = _refused_tool_text(result)
    envelope = json.loads(text)
    assert envelope.get("schema") == STRUCTURED_ERROR_SCHEMA, envelope
    assert envelope.get("schema_version") == 1, envelope
    error = envelope.get("error")
    assert isinstance(error, dict), envelope
    code = error.get("code")
    retryable = error.get("retryable")
    assert isinstance(code, str), envelope
    assert isinstance(retryable, bool), envelope
    assert code in FROZEN_STRUCTURED_CODES, (
        f"{code!r} is outside the closed structured vocabulary this file "
        f"freezes; a refusal outside the set means the request escaped the "
        f"closed surface"
    )
    return code, retryable


# A well-formed structured request: correct token, correct whole version, a
# closed subject and operation, and nothing else. Everything below mutates ONE
# member of this, so a refusal can be attributed to that member rather than to
# a body that was already wrong.
_VALID_STRUCTURED: dict[str, Any] = {
    "schema": "iknow.structured_query",
    "schema_version": 1,
    "subject": "entity",
    "operation": "list",
}


def _sql_tier(request: dict[str, Any]) -> dict[str, Any]:
    """``filters`` carrying ``request`` on the host-bound ``sql`` tier.

    The tier key is mandatory for every discriminating case: on the default
    ``graph`` tier ``request.ts`` refuses before the body is read, so an
    unknown-member probe there proves nothing about the closed member set.
    """

    return {"search_mode": "sql", "structured_query": request}


def _evidence_refusal(response: dict[str, Any], probe_id: int) -> dict[str, Any]:
    """The typed in-envelope private refusal, with no content member."""

    assert "result" in response, response
    result = response["result"]
    assert result.get("schema") == EVIDENCE_RESULT_SCHEMA, result
    error = result.get("error")
    assert isinstance(error, dict), result
    assert error.get("retryable") is False, result
    for member in ("value", "result_count", "receipt", "supports", "row"):
        assert member not in result, (
            f"the private dispatch at probe {probe_id} disclosed {member}: {result}"
        )
    assert response.get("id") == probe_id, response
    return error


# ----- H2: the model-visible tools/call boundary ------------------------------


def test_empty_arguments_are_refused_by_every_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All eight tools refuse an empty ``arguments`` object on the real build.

    This is the observation the declaration-only driver could not make: the
    server's own runtime boundary, not its published schema, is what rejects an
    empty call. Each refusal names the tool it came from, so a registry that
    silently routed a call to the wrong handler would fail here.
    """

    with _live_private_route(monkeypatch) as live:
        request_id = 600
        for name in EXPECTED_TOOL_NAMES:
            request_id += 1
            result = _tools_call(live, request_id, name, {})
            text = _refused_tool_text(result)
            assert name in text, (
                f"{name} refused an empty call without naming itself, so the "
                f"refusal cannot be attributed to it: {text}"
            )
            assert "validation" in text.lower() or "provide" in text, text


def test_retrieve_refuses_a_missing_empty_and_wrongly_typed_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``retrieve``'s three distinct empty/mistyped-query refusals, by text.

    Frozen separately because they are three different checks, not one: the
    member is absent (the strict object has no value to validate), it is present
    and empty (the ``min(1)`` bound), and it is present but whitespace-only (the
    separate ``refine``). A build that collapsed them into a single generic
    "invalid input" would fail here.
    """

    cases: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
        ("absent", {}, ("expected string, received undefined",)),
        ("empty", {"query": ""}, ("query must be a non-empty string",)),
        (
            "whitespace-only",
            {"query": "   "},
            ("query must contain at least one non-whitespace character",),
        ),
    )
    with _live_private_route(monkeypatch) as live:
        request_id = 620
        for label, arguments, expected in cases:
            request_id += 1
            result = _tools_call(live, request_id, "retrieve", arguments)
            text = _refused_tool_text(result)
            for fragment in expected:
                assert fragment in text, (
                    f"the {label} query refusal lost its own wording; observed "
                    f"{text!r}, expected it to name {fragment!r}"
                )


def test_retrieve_refuses_wrong_types_oversized_values_and_unknown_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``strictObject`` bound on the model plane, one case each.

    Three distinct refusal families, frozen apart on purpose:

    * a wrong JSON **type** (``limit`` as a string) — the parser's own error;
    * an out-of-range **value** (``limit`` above the published maximum) — the
      published bound, checked against the number 100 that
      ``EXPECTED_TOOL_INPUT_PROPERTIES`` already freezes the schema for, so the
      wire and the declaration cannot drift apart silently;
    * an **unknown top-level member** — the strict object, which is the reason
      ``top_k`` / ``filter`` / ``document_id`` style drift cannot hide.
    """

    cases: tuple[tuple[str, dict[str, Any], tuple[str, ...]], ...] = (
        (
            "wrong type",
            {"query": "x", "limit": "not-a-number"},
            ("limit", "expected number, received string"),
        ),
        (
            "oversized value",
            {"query": "x", "limit": 101},
            ("limit", "expected number to be <=100"),
        ),
        (
            "unknown top-level member",
            {"query": "x", "unknown_member": 1},
            ("Unrecognized key", "unknown_member"),
        ),
    )
    with _live_private_route(monkeypatch) as live:
        request_id = 640
        for label, arguments, expected in cases:
            request_id += 1
            result = _tools_call(live, request_id, "retrieve", arguments)
            text = _refused_tool_text(result)
            for fragment in expected:
                assert fragment in text, (
                    f"the {label} refusal lost its own wording; observed "
                    f"{text!r}, expected it to name {fragment!r}"
                )


def test_a_valid_retrieve_call_still_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: the boundary refuses bad input, not everything.

    Without this, a build that answered ``isError`` to *every* ``tools/call``
    would pass all the cases above. An empty retained set is a valid typed
    answer on a freshly created schema — this ticket proves the leg reaches the
    wire, it does not assert corpus content.
    """

    with _live_private_route(monkeypatch) as live:
        result = _tools_call(live, 660, "retrieve", {"query": "operating revenue"})
        assert result.get("isError") not in (True,), result
        assert '"chunks"' in _tool_text(result), result


def test_repeated_calls_answer_every_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five identical calls, five answers with matching ids.

    A duplicate submission must not be swallowed, deduplicated into one answer,
    or leave the session wedged for the next caller. Only the tool's **content
    text** is compared: the JSON-RPC envelope legitimately differs per call,
    so an envelope comparison would pass regardless of what the server answered.
    """

    with _live_private_route(monkeypatch) as live:
        bodies = []
        for request_id in range(670, 675):
            result = _tools_call(live, request_id, "retrieve", {"query": "repeat"})
            assert result.get("isError") not in (True,), result
            bodies.append(_tool_text(result))
            # The session is still healthy between calls.
            _tools_call(live, 1000 + request_id, "retrieve", {"query": "probe"})
        assert len(bodies) == 5
        assert len(set(bodies)) == 1, (
            f"identical repeated calls answered differently: {bodies}"
        )


def test_concurrent_calls_each_get_their_own_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six concurrent ``tools/call`` frames, six answers, each id matched.

    The session serialises its I/O behind one lock, so this is the observable
    half of "the owner closes, on every path" applied to a shared connection:
    concurrent callers must each receive *their own* response. A server that
    answered all six with one frame, or crossed two ids, would fail on the id
    assertion in ``_tools_call``.
    """

    with _live_private_route(monkeypatch) as live:
        outcomes: dict[int, str] = {}
        guard = threading.Lock()

        def issue(request_id: int) -> None:
            try:
                result = _tools_call(
                    live, request_id, "retrieve", {"query": f"q{request_id}"}
                )
                text = _tool_text(result)
            except Exception as error:  # noqa: BLE001 - recorded, asserted below
                with guard:
                    outcomes[request_id] = f"{type(error).__name__}: {error}"
                return
            with guard:
                outcomes[request_id] = text

        # Warm the child so the measured threads race on requests, not on boot.
        _tools_call(live, 680, "retrieve", {"query": "warmup"})
        threads = [threading.Thread(target=issue, args=(n,)) for n in range(681, 687)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_REQUEST_TIMEOUT_SECONDS * 2)
            assert not thread.is_alive(), "a concurrent call never returned"

        # Every one of the six ids must have been answered by the session's own
        # id check inside `_tools_call`, so a missing key here means a thread
        # never reached it.
        assert sorted(outcomes) == [681, 682, 683, 684, 685, 686], sorted(outcomes)
        for request_id, text in sorted(outcomes.items()):
            assert not text.startswith(("MCPStdio", "AssertionError")), (
                f"concurrent call {request_id} failed at the transport: {text}"
            )
        assert all('"chunks"' in text for text in outcomes.values()), outcomes

        # The session still answers afterwards: concurrency did not wedge it.
        after = _tools_call(live, 687, "retrieve", {"query": "after"})
        assert '"chunks"' in _tool_text(after), after


# ----- H1: the structured_query behavioural guard ----------------------------


def test_structured_query_refuses_an_evidence_shaped_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The H1 guard.** A real ``tools/call`` cannot smuggle Evidence through
    ``filters.structured_query``.

    The published ``filters`` schema constrains nothing inside the object
    branch, so the declaration freeze cannot see this. What holds the line is
    IK's runtime closed member set, and this test is what makes that fact
    checkable rather than assumed.

    Each payload is a well-formed structured request with exactly **one** member
    that must be refused: an Evidence reference, an identity assertion, or an
    arbitrary unknown key. All three are answered with the closed vocabulary's
    ``malformed_structured_query`` and ``retryable: false``.
    """

    payloads: tuple[tuple[str, dict[str, Any]], ...] = (
        ("evidence_ref", {**_VALID_STRUCTURED, "evidence_ref": "grant-ref-0001"}),
        (
            "identity assertion",
            {**_VALID_STRUCTURED, "trusted_consumer": "fin-host", "actor_id": "x"},
        ),
        ("unknown member", {**_VALID_STRUCTURED, "wat": 1}),
    )
    with _live_private_route(monkeypatch) as live:
        request_id = 700
        for label, payload in payloads:
            request_id += 1
            result = _tools_call(
                live, request_id, "retrieve", {"query": "x", "filters": _sql_tier(payload)}
            )
            code, retryable = _structured_refusal_code(result)
            assert code == "malformed_structured_query", (
                f"filters.structured_query accepted a {label} on the bound build "
                f"{BOUND_IKNOW_COMMIT}: the closed member set did not hold. "
                f"observed={code}"
            )
            assert retryable is False, result


def test_structured_query_refuses_a_widened_schema_token_or_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The **other** half of the B2 counter-example, refused behaviourally.

    ``requireRecognizedSchema`` accepts one token and one whole version. Adding
    a second token there is IK-internal and moves no published byte, so the
    schema freeze stays green — this is what refuses it on the wire instead, and
    distinguishes the two codes the SPEC separates: an unknown *token* and a
    recognized token carrying a wrong *version* are both
    ``unsupported_structured_schema``, while a version that is not a finite
    whole number is ``malformed_structured_query``.
    """

    cases: tuple[tuple[str, dict[str, Any], str], ...] = (
        (
            "second schema token",
            {**_VALID_STRUCTURED, "schema": "iknow.structured_query_v2"},
            "unsupported_structured_schema",
        ),
        (
            "unsupported whole version",
            {**_VALID_STRUCTURED, "schema_version": 2},
            "unsupported_structured_schema",
        ),
        (
            "fractional version",
            {**_VALID_STRUCTURED, "schema_version": 1.5},
            "malformed_structured_query",
        ),
        (
            "string version",
            {**_VALID_STRUCTURED, "schema_version": "1"},
            "malformed_structured_query",
        ),
    )
    with _live_private_route(monkeypatch) as live:
        request_id = 720
        for label, payload, expected in cases:
            request_id += 1
            result = _tools_call(
                live, request_id, "retrieve", {"query": "x", "filters": _sql_tier(payload)}
            )
            code, _retryable = _structured_refusal_code(result)
            assert code == expected, (
                f"the {label} probe on filters.structured_query was refused as "
                f"{code!r}, expected {expected!r}"
            )


def test_structured_query_closed_token_sets_stay_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed token sets inside the request, one out-of-set member each.

    Subject, operation and filter field are all closed sets. Each probe keeps
    the rest of the request valid, so the returned code names the token set that
    actually refused it rather than a body that was wrong anyway.
    """

    cases: tuple[tuple[str, dict[str, Any], str], ...] = (
        (
            "unsupported subject",
            {**_VALID_STRUCTURED, "subject": "not_a_subject"},
            "unsupported_subject",
        ),
        (
            "unsupported operation",
            {**_VALID_STRUCTURED, "operation": "delete"},
            "unsupported_operation",
        ),
        (
            "unsupported filter field",
            {
                **_VALID_STRUCTURED,
                "filters": [{"field": "not_a_field", "op": "eq", "value": "z"}],
            },
            "unsupported_field",
        ),
        (
            "malformed filter value",
            {
                **_VALID_STRUCTURED,
                "filters": [{"field": "id", "op": "eq", "value": "not-a-uuid"}],
            },
            "invalid_value",
        ),
    )
    with _live_private_route(monkeypatch) as live:
        request_id = 740
        for label, payload, expected in cases:
            request_id += 1
            result = _tools_call(
                live, request_id, "retrieve", {"query": "x", "filters": _sql_tier(payload)}
            )
            code, _retryable = _structured_refusal_code(result)
            assert code == expected, (
                f"the {label} probe on filters.structured_query was refused as "
                f"{code!r}, expected {expected!r}"
            )


def test_structured_query_refuses_a_non_object_and_an_oversized_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container and size bounds, on the real boundary.

    ``structured_query`` as a string still reaches the closed validator (the
    filter union admits a string) and is refused as a non-object. As an array it
    fails earlier, in the ``filters`` record itself, so it is asserted on that
    refusal's own wording — a different layer, deliberately not conflated with
    the structured vocabulary. The over-large body is refused with
    ``request_too_large`` before its contents are read at all.
    """

    with _live_private_route(monkeypatch) as live:
        # A non-object that the filter union admits reaches the closed set.
        result = _tools_call(
            live, 760, "retrieve", {"query": "x", "filters": {"structured_query": "nope"}}
        )
        code, retryable = _structured_refusal_code(result)
        assert code == "malformed_structured_query", result
        assert retryable is False, result

        # An array is refused by the record itself, naming the member.
        result = _tools_call(
            live, 761, "retrieve", {"query": "x", "filters": {"structured_query": ["a"]}}
        )
        text = _refused_tool_text(result)
        assert "filters.structured_query" in text, text

        # Over the 32 KiB bound: refused as a resource fact, not after a walk.
        oversized = {**_VALID_STRUCTURED, "cursor": "z" * 40_000}
        result = _tools_call(
            live, 762, "retrieve", {"query": "x", "filters": _sql_tier(oversized)}
        )
        code, retryable = _structured_refusal_code(result)
        assert code == "request_too_large", result
        assert retryable is False, result


def test_a_valid_structured_query_reaches_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control for every structured refusal above.

    A structurally valid request on the host-bound ``sql`` tier clears the
    closed member set, the schema token, the version and every closed token set,
    and stops at the executor with ``structured_search_unavailable``. If a
    future build refused it with one of the codes frozen above instead, this
    fails — so the refusals cannot be made trivially true by refusing
    everything.

    It also pins that the *tier gate* is real: the same valid request without
    ``search_mode: sql`` is refused on the default tier, and on this build with
    the same code, which is why the discriminating cases above pin the tier.
    """

    with _live_private_route(monkeypatch) as live:
        result = _tools_call(
            live, 780, "retrieve", {"query": "x", "filters": _sql_tier(_VALID_STRUCTURED)}
        )
        code, retryable = _structured_refusal_code(result)
        assert code == STRUCTURED_REACHES_EXECUTOR_CODE, result
        assert retryable is True, result

        # The tier gate, observed: a request may not ride a tier other than the
        # host-bound sql one, and it may not select its own tier either.
        result = _tools_call(
            live, 781, "retrieve", {"query": "x", "filters": {"structured_query": _VALID_STRUCTURED}}
        )
        code, _retryable = _structured_refusal_code(result)
        assert code in FROZEN_STRUCTURED_CODES, result

        # An invalid search_mode is refused before the structured body is read.
        result = _tools_call(
            live,
            782,
            "retrieve",
            {
                "query": "x",
                "filters": {"search_mode": "nonsense", "structured_query": _VALID_STRUCTURED},
            },
        )
        text = _refused_tool_text(result)
        assert "search_mode" in text and "invalid_search_mode" in text, text


def test_an_object_filter_key_other_than_structured_query_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``filters`` publishes an object branch, and exactly one key may use it.

    This is the third leg of the H1 guard. The published schema admits an
    object value for **any** filter key; the refusal for every key but
    ``structured_query`` is what stops an Evidence payload from riding
    ``filters.some_other_key`` while the schema still claims it is admissible.
    """

    with _live_private_route(monkeypatch) as live:
        for request_id, key in ((790, "evidence"), (791, "structured"), (792, "corpus")):
            result = _tools_call(
                live, request_id, "retrieve", {"query": "x", "filters": {key: {"a": 1}}}
            )
            text = _refused_tool_text(result)
            assert "filters.structured_query" in text, (
                f"the object-valued filter key {key!r} was not refused by the "
                f"runtime boundary: {text}"
            )


# ----- the private route's own four request guards ---------------------------


@pytest.fixture
def bound_evidence_route() -> Iterator[dict[str, Any]]:
    """A real IK child carrying a **trusted identity binding**.

    This is what makes the four private-route request guards observable at all.
    An unbound session answers ``access_denied`` /
    ``trusted_binding_unresolved`` for **every** request, whatever the body, so
    the guards are unreachable behind it and testing them there would assert
    nothing about ``parseEvidenceRequest``.

    The binding is a local scope and a local revision with no seeded grants, so
    the well-formed control below is refused by the **store** rather than by the
    request contract. That is what makes the two directions distinguishable: a
    body the contract rejects yields ``malformed_evidence_contract``, and a body
    it accepts reaches the store.
    """

    schema = _schema_name()
    _create_pg_schema(schema)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="fin-t39-behaviour-embedding", daemon=True
    )
    thread.start()
    session: MCPStdioSession | None = None
    primary: BaseException | None = None
    try:
        transport = {
            "GRAPHRAG_MEMORY_STORAGE": "pgvector",
            "GRAPHRAG_MEMORY_DB_URL": _db_url_for_schema(_pg_url(), schema),
            "GRAPHRAG_MEMORY_EMBED_DIMENSIONS": str(EMBED_DIMENSIONS),
            "GRAPHRAG_MEMORY_PGVECTOR_ANN_MAX_DIMENSIONS": "2000",
            "GRAPHRAG_MEMORY_EMBED_API_KEY": "fin-t39-behaviour",
            "GRAPHRAG_MEMORY_EMBED_BASE_URL": (
                f"http://127.0.0.1:{server.server_port}/v1"
            ),
            "GRAPHRAG_MEMORY_EMBED_MODEL": "fin-t39-behaviour",
            "GRAPHRAG_MEMORY_RETRIEVAL_BM25_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_GRAPH_ENABLED": "false",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_ENABLED": "false",
            "GRAPHRAG_MEMORY_NEO4J_URI": "",
            "GRAPHRAG_MEMORY_RETRIEVAL_RERANK_URL": "",
            "ANTHROPIC_AUTH_TOKEN": "",
        }
        binding = EvidenceIdentityBinding(
            actor_id="fin-t39-behaviour-reader",
            owner_scope_binding="fin_t39_behaviour_scope",
            reader_binding="fin-t39-behaviour-reader",
            source_refs=("t39-behaviour-source",),
            authorization_revision="t39-behaviour-rev-1",
            deadline_at=deadline_within(600.0),
        )
        session = MCPStdioSession(
            [_node_executable(), str(IKNOW_ROOT / "dist" / "index.js")],
            cwd=str(IKNOW_ROOT),
            timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
            env=identity_env(binding, transport_env=transport),
        )
        # The handshake proves the child booted against this schema before any
        # private dispatch is attempted on it.
        session.call(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            _REQUEST_TIMEOUT_SECONDS,
        )
        yield {"session": session, "schema": schema}
    except BaseException as error:  # noqa: BLE001 - re-raised after teardown
        primary = error
        raise
    finally:
        if session is not None:
            session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _drop_pg_schema(schema)
        if primary is not None:
            raise primary


def test_the_private_route_refuses_every_off_contract_request(
    bound_evidence_route: dict[str, Any],
) -> None:
    """All four of the private route's request guards, on the real boundary.

    These four refusals had no test at all: nothing in the diff sent a private
    request whose body was off-contract. Each case below perturbs exactly one
    member of a well-formed request, and each must answer with the *contract*
    failure token rather than any store denial — the store denial is what a
    well-formed body earns here, and conflating the two would let a broken
    guard pass.

    The four guards, as IK implements them: the request must be a JSON object;
    ``schema``/``schema_version`` must be the recognized pair; ``action`` must
    be a string in the closed ``read|supports`` set; the member set must be
    exactly the four keys the action accepts; and ``evidence_ref`` must be a
    non-empty string.
    """

    valid = {
        "schema": "iknow.evidence_request",
        "schema_version": 1,
        "action": "read",
        "evidence_ref": "t39-behaviour-ref",
    }
    session = bound_evidence_route["session"]

    def probe(request_id: int, params: Any, label: str) -> dict[str, Any]:
        response = session.call(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": PRIVATE_METHOD,
                "params": params,
            },
            _REQUEST_TIMEOUT_SECONDS,
        )
        error = _evidence_refusal(response, request_id)
        assert error.get("code") == EVIDENCE_MALFORMED_CODE, (
            f"the {label} probe was not refused by the private request contract: "
            f"{error}"
        )
        assert error.get("reason") == EVIDENCE_MALFORMED_REASON, error
        return error

    # The control first: a well-formed body is ACCEPTED by the contract, so it
    # reaches the store and is refused there instead. Without it, the cases
    # below could all pass against a build whose contract refuses everything.
    control = session.call(
        {"jsonrpc": "2.0", "id": 800, "method": PRIVATE_METHOD, "params": valid},
        _REQUEST_TIMEOUT_SECONDS,
    )
    control_error = _evidence_refusal(control, 800)
    assert control_error.get("code") != EVIDENCE_MALFORMED_CODE, (
        f"a well-formed private request was refused by the request contract, so "
        f"the guards below would prove nothing: {control_error}"
    )

    cases: tuple[tuple[int, str, Any], ...] = (
        (801, "empty params", {}),
        (802, "missing schema", {k: v for k, v in valid.items() if k != "schema"}),
        (803, "wrong schema_version", {**valid, "schema_version": 2}),
        (804, "missing action", {k: v for k, v in valid.items() if k != "action"}),
        (805, "action outside the closed set", {**valid, "action": "not_an_action"}),
        (806, "non-string action", {**valid, "action": 7}),
        (807, "unknown member", {**valid, "wat": 1}),
        (
            808,
            "identity asserted on the request channel",
            {**valid, "trusted_consumer": "fin-host", "actor_id": "forged"},
        ),
        (809, "missing evidence_ref", {k: v for k, v in valid.items() if k != "evidence_ref"}),
        (810, "empty evidence_ref", {**valid, "evidence_ref": ""}),
        (811, "blank evidence_ref", {**valid, "evidence_ref": "   "}),
        (812, "non-string evidence_ref", {**valid, "evidence_ref": 7}),
        (
            813,
            "a member only the other action accepts",
            {**valid, "action": "supports", "cursor": "x"},
        ),
    )
    for request_id, label, params in cases:
        probe(request_id, params, label)


def test_the_private_route_refuses_a_non_object_request_by_not_answering(
    bound_evidence_route: dict[str, Any],
) -> None:
    """A non-object ``params`` on the private route gets no answer at all.

    Observed on the bound build: the private handler is registered with
    ``params: z.unknown()``, so a params value that is not a JSON object is
    accepted by the SDK's own parsing and never reaches
    ``parseEvidenceRequest`` — the request simply receives no response. The
    session surfaces that as ``MCPStdioTransportError``.

    This is asserted rather than skipped because it is a boundary fact a host has
    to plan for, and it is the reason the *first* guard above is asserted on the
    empty object rather than on ``null``: only the empty object is a parseable
    ``params`` that reaches the contract.
    """

    session = bound_evidence_route["session"]
    with pytest.raises(MCPStdioTransportError):
        session.call(
            {
                "jsonrpc": "2.0",
                "id": 820,
                "method": PRIVATE_METHOD,
                "params": [1, 2, 3],
            },
            15.0,
        )


def test_an_unbound_session_refuses_before_reading_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unbound refusal, and why it is recorded here.

    On the shared unbound session the private route answers
    ``access_denied`` / ``trusted_binding_unresolved`` for **every** body,
    well-formed or not. That is the right refusal — no identity, no authority —
    and it is also why the request-contract guards need their own bound
    session. Recorded here so the two are not confused later.
    """

    cases: tuple[tuple[int, dict[str, Any]], ...] = (
        (830, {}),
        (831, {"action": "read", "evidence_ref": "t39-probe"}),
        (
            832,
            {
                "schema": "iknow.evidence_request",
                "schema_version": 1,
                "action": "read",
                "evidence_ref": "t39-probe",
                "trusted_consumer": "fin-host",
            },
        ),
    )
    with _live_private_route(monkeypatch) as live:
        for request_id, params in cases:
            response = live["session"].call(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": PRIVATE_METHOD,
                    "params": params,
                },
                _REQUEST_TIMEOUT_SECONDS,
            )
            error = _evidence_refusal(response, request_id)
            assert error.get("code") == EVIDENCE_UNBOUND_CODE, error
            assert error.get("reason") == EVIDENCE_UNBOUND_REASON, error
            assert "fin-host" not in str(error.get("message")), error


def test_a_private_dispatch_never_perturbs_the_model_visible_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole file's invariant, restated against behaviour rather than shape.

    After every structured refusal and every private dispatch above, the tool
    listing is byte-identical to the one observed first. If a refusal had, say,
    registered a tool to carry its envelope, this fails — which is the shape of
    regression the declaration-only driver cannot see coming.
    """

    with _live_private_route(monkeypatch) as live:
        before = live["session"].call(
            {"jsonrpc": "2.0", "id": 840, "method": "tools/list"},
            _REQUEST_TIMEOUT_SECONDS,
        )["result"]["tools"]

        _tools_call(
            live,
            841,
            "retrieve",
            {
                "query": "x",
                "filters": _sql_tier({**_VALID_STRUCTURED, "evidence_ref": "ref"}),
            },
        )
        live["session"].call(
            {
                "jsonrpc": "2.0",
                "id": 842,
                "method": PRIVATE_METHOD,
                "params": {"action": "read", "evidence_ref": "t39-probe"},
            },
            _REQUEST_TIMEOUT_SECONDS,
        )

        after = live["session"].call(
            {"jsonrpc": "2.0", "id": 843, "method": "tools/list"},
            _REQUEST_TIMEOUT_SECONDS,
        )["result"]["tools"]
        assert after == before, "the model-visible surface moved under a refusal"
        names = tuple(tool["name"] for tool in after)
        assert names == EXPECTED_TOOL_NAMES, names
        assert PRIVATE_METHOD not in names, names