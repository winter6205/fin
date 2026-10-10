"""T14 -- EP4: page and select a row grant's frozen supports through Fin's
private session.

This is the live acceptance for the EP4 support continuation, and it is the
counterpart T14's freeze record said was missing: a row grant's supports read
over the real Fin dispatch client, against a real built iknow child and real
PostgreSQL.

The frozen expectations come from the ticket and from iknow's continuation
contract, not from anything the backend answered.  The support ids below are
literals chosen here and seeded from those same literals, so a drift between
seed and reader cannot pass.

The environment is T13's, reused rather than rebuilt: the same trusted identity
decision, the same real session, the same strict prerequisites.  The one thing
this lane adds is a grant whose membership does not fit in a single page, which
is the only way a cursor and a selected support beyond the inline prefix can be
reached at all.

The wire cases that reach no database and no process live in
``test_evidence_route_supports_contract.py`` instead: the ``pytestmark`` below is
the opt-in boundary for the ten cases here that genuinely need a live run, and a
hermetic contract pinned behind it would not run in the default lane at all.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from fin.retrieve.evidence_route import (
    EvidenceGrantRefusal,
    EvidenceRouteClient,
)


pytestmark = [pytest.mark.mcp, pytest.mark.sql_live]

sys.path.insert(0, str(Path(__file__).parent))

# The seed, the identities and the live environment are T13's; this lane adds no
# second opinion about any of them. `t13_env` is re-instantiated for this module,
# so the schema and the child processes here belong to this run alone.
from test_live_t13_private_evidence_read import (  # noqa: E402
    AUTH_REV,
    EXPIRED_REF,
    PURGE_REV,
    ROW_VALUE,
    SCOPE_A,
    SOURCE_A1,
    SOURCE_A2,
    SOURCE_VERSION_A1,
    SOURCE_VERSION_A2,
    SUPPORT_A1,
    _connect,
    t13_env,  # noqa: F401
)


# ----- frozen expectations, chosen before the run ----------------------------

#: One row grant with nine pinned supports. iknow's page bound is eight, so the
#: ninth is reachable only by following a cursor or by naming it -- which is what
#: makes the first page and the selected read two separate claims rather than one.
PAGED_REF = "d1000000-0000-4000-8000-000000000000"
PAGED_SUPPORT_IDS: tuple[str, ...] = tuple(
    f"d2000000-0000-4000-8000-{index:012d}" for index in range(1, 10)
)
PAGED_PAGE_ONE = PAGED_SUPPORT_IDS[:8]
PAGED_PAGE_TWO = PAGED_SUPPORT_IDS[8:]
#: The support that only a cursor or a selector reaches.
PAGED_LAST = PAGED_SUPPORT_IDS[-1]

#: A valid uuid that this grant did not pin. It is pinned by T13's row grant
#: instead, so it is a real support of a real grant and still not a member here.
SUPPORT_FOREIGN = SUPPORT_A1

#: The exact descriptor the first support must carry, rebuilt from its columns.
PAGED_FIRST_LOCATION = {"document_id": f"doc-{PAGED_PAGE_ONE[0]}", "version": "1", "unit": 1}
PAGED_FIRST_SPAN = {"start": 0, "end": 4}


# ----- the seed: one grant whose membership needs a second page ---------------


def _seed_paged_grant(scoped_url: str, *, now: datetime) -> None:
    """Pin nine supports on a fresh row grant, on the sources T13's identity holds.

    Every support carries **two** extraction links on purpose: the one the grant
    pins, and a second one in the live table. A continuation that read the current
    table would answer with both and claim the frozen citation had gained a link,
    so the descriptor's `extraction_refs` is the pinned one and only that one.
    """

    expires_at = now + timedelta(hours=6)
    with _connect(scoped_url) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO evidence_grants (evidence_ref, grant_kind, subject, "
                "operation, query_digest, scope_binding, read_revision, "
                "authorization_revision, purge_revision, queried_at, expires_at, "
                "context, value, result_count, authorization_receipt_id) "
                "VALUES (%s, 'row', 'entity', 'entity', %s, %s, %s, %s, %s, %s, "
                "%s, %s::jsonb, %s::jsonb, NULL, NULL)",
                (
                    PAGED_REF,
                    "t14-query-digest-frozen",
                    SCOPE_A,
                    "0",
                    AUTH_REV,
                    PURGE_REV,
                    now,
                    expires_at,
                    json.dumps(
                        {
                            "schema": "iknow.evidence_context",
                            "schema_version": 1,
                            "operation": "entity.get",
                        }
                    ),
                    json.dumps({"entity": {"ref": "fin-t14-entity-alpha"}}),
                ),
            )
            for index, support_id in enumerate(PAGED_SUPPORT_IDS):
                # Both of T13's active source versions, so neither obligation
                # fence on the seeded snapshot is in play for this grant.
                on_a1 = index % 2 == 0
                cur.execute(
                    "INSERT INTO assertion_supports (support_id, subject, "
                    "result_id, source_ref, source_version, source_version_id, "
                    "document_id, unit_version, unit, span_start, span_end, "
                    "created_at) VALUES (%s, 'entity', %s, %s, 'v1', %s, %s, "
                    "'1', 1, %s, %s, %s)",
                    (
                        support_id,
                        "fin-t14-entity-alpha",
                        SOURCE_A1 if on_a1 else SOURCE_A2,
                        SOURCE_VERSION_A1 if on_a1 else SOURCE_VERSION_A2,
                        f"doc-{support_id}",
                        index,
                        index + 4,
                        now,
                    ),
                )
                cur.execute(
                    "INSERT INTO support_extractions (support_id, extraction_ref, "
                    "created_at) VALUES (%s, %s, %s), (%s, %s, %s)",
                    (
                        support_id,
                        f"t14-pinned-{support_id}",
                        now,
                        support_id,
                        f"t14-live-{support_id}",
                        now,
                    ),
                )
                cur.execute(
                    "INSERT INTO evidence_grant_supports (evidence_ref, "
                    "support_id, role) VALUES (%s, %s, 'occurrence')",
                    (PAGED_REF, support_id),
                )
                cur.execute(
                    "INSERT INTO evidence_grant_extractions (evidence_ref, "
                    "support_id, extraction_ref) VALUES (%s, %s, %s)",
                    (PAGED_REF, support_id, f"t14-pinned-{support_id}"),
                )
        connection.commit()


@pytest.fixture(scope="module")
def t14_env(t13_env) -> Iterator[dict[str, Any]]:
    scoped_url = t13_env["scoped_url"]
    _seed_paged_grant(scoped_url, now=t13_env["now"])
    yield {
        "evidence": EvidenceRouteClient(
            t13_env["session_a"], binding=t13_env["binding_a"]
        ),
        "evidence_narrow": t13_env["evidence_narrow"],
        "scoped_url": scoped_url,
    }


def _supports_refusal(t14_env, ref: str, **selectors: str) -> EvidenceGrantRefusal:
    """Read ``ref`` through the supports path and return the refusal it earned."""

    with pytest.raises(EvidenceGrantRefusal) as excinfo:
        t14_env["evidence"].supports(ref, **selectors)
    return excinfo.value


# ----- the continuation, reached over the real session -----------------------


def test_row_grant_supports_without_a_selector_returns_the_first_page(t14_env) -> None:
    """The opening page of a frozen membership, in stable UUID order."""

    page = t14_env["evidence"].supports(PAGED_REF)
    assert page["evidence_ref"] == PAGED_REF
    assert page["total_supports"] == len(PAGED_SUPPORT_IDS)
    assert [entry["support_id"] for entry in page["supports"]] == list(PAGED_PAGE_ONE)
    # Nine pinned supports do not fit in a page, so the page opens a sequence.
    assert PAGED_LAST not in PAGED_PAGE_ONE
    assert page["next_cursor"] is not None
    assert page["selected"] is False


def test_a_support_descriptor_carries_its_locator_and_pinned_extractions(
    t14_env,
) -> None:
    """The descriptor is rebuilt from the stored columns, and pins its own links."""

    descriptor = t14_env["evidence"].supports(PAGED_REF)["supports"][0]
    assert descriptor["support_id"] == PAGED_PAGE_ONE[0]
    assert descriptor["role"] == "occurrence"
    assert descriptor["subject"] == "entity"
    assert descriptor["result_id"] == "fin-t14-entity-alpha"
    assert descriptor["source_version"] == "v1"
    assert descriptor["source_version_id"] in (
        SOURCE_VERSION_A1,
        SOURCE_VERSION_A2,
    )
    assert descriptor["location"] == PAGED_FIRST_LOCATION
    assert descriptor["span"] == PAGED_FIRST_SPAN
    # The pinned link, not the one the live extraction table now carries.
    assert descriptor["extraction_refs"] == [f"t14-pinned-{PAGED_PAGE_ONE[0]}"]


def test_supports_cursor_returns_the_following_page(t14_env) -> None:
    """Following the cursor finishes the sequence and closes it."""

    first = t14_env["evidence"].supports(PAGED_REF)
    second = t14_env["evidence"].supports(PAGED_REF, cursor=first["next_cursor"])
    assert [entry["support_id"] for entry in second["supports"]] == list(PAGED_PAGE_TWO)
    assert second["total_supports"] == len(PAGED_SUPPORT_IDS)
    assert second["selected"] is False
    # The last page of a sequence carries no cursor, because a position past the
    # end resolves to nothing.
    assert second["next_cursor"] is None
    # The continuation echoes the grant's own fixed expiry and renews nothing.
    assert second["expires_at"] == first["expires_at"]
    # Concatenated, the two pages are the exact ascending membership the grant
    # pinned, which is what a caller reconstructs the sequence from.
    paged = [
        entry["support_id"] for entry in first["supports"] + second["supports"]
    ]
    assert paged == list(PAGED_SUPPORT_IDS)


def test_supports_support_id_resolves_a_member_beyond_the_inline_prefix(
    t14_env,
) -> None:
    """A selected support answers on its own and never opens a sequence."""

    assert PAGED_LAST not in PAGED_PAGE_ONE
    selected = t14_env["evidence"].supports(PAGED_REF, support_id=PAGED_LAST)
    assert [entry["support_id"] for entry in selected["supports"]] == [PAGED_LAST]
    assert selected["selected"] is True
    assert selected["next_cursor"] is None
    assert selected["total_supports"] == len(PAGED_SUPPORT_IDS)


# ----- refusals this path can now emit ----------------------------------------


def test_supports_refuses_a_cursor_and_a_selected_support_together(t14_env) -> None:
    """The two selectors are mutually exclusive, and neither one is honoured.

    The wire reason is `unknown_field`, not the continuation's own
    `contract_violation`: iknow folds a contract violation onto the member that
    already means "this request is not on the contract", and the failure token
    alongside it is what separates a malformed selector from an oversized page.
    Fin carries both halves through unchanged rather than opening its own.
    """

    first = t14_env["evidence"].supports(PAGED_REF)
    refusal = _supports_refusal(
        t14_env,
        PAGED_REF,
        cursor=first["next_cursor"],
        support_id=PAGED_LAST,
    )
    assert refusal.code == "malformed_evidence_contract"
    assert refusal.reason == "unknown_field"
    assert PAGED_LAST not in str(refusal)


def test_supports_maps_a_selector_outside_the_grant_to_a_typed_refusal(
    t14_env,
) -> None:
    """A real support of another grant is refused, and names no membership.

    The refusal cannot double as a membership oracle: it does not distinguish
    "no such grant" from "not one of its supports".
    """

    refusal = _supports_refusal(t14_env, PAGED_REF, support_id=SUPPORT_FOREIGN)
    assert refusal.code == "malformed_evidence_contract"
    assert refusal.reason == "unknown_field"
    assert SUPPORT_FOREIGN not in str(refusal)
    assert PAGED_SUPPORT_IDS[0] not in str(refusal)


def test_supports_refuses_a_selector_that_is_not_a_uuid(t14_env) -> None:
    """A selector that cannot name a member is refused on its own shape."""

    refusal = _supports_refusal(t14_env, PAGED_REF, support_id="not-a-uuid")
    assert refusal.code == "malformed_evidence_contract"
    assert refusal.reason == "unknown_field"


def test_supports_replays_the_grants_expiry_on_the_continuation_path(
    t14_env,
) -> None:
    """The obligation fences run on this path too, not only on a read.

    The expired grant belongs to T13's seed and pins a real support, so the only
    thing that can refuse it is the replay.
    """

    refusal = _supports_refusal(t14_env, EXPIRED_REF)
    assert refusal.code == "evidence_expired"
    assert refusal.reason == "expired"
    assert ROW_VALUE["entity"]["name"] not in str(refusal)


def test_a_narrower_identity_still_cannot_page_the_row_grant(t14_env) -> None:
    """The authorized-subset boundary holds on the supports path as well.

    This identity holds the grant's own scope and only one of its two pinned
    sources, so only the subset check can stop it. Which of the two read-side and
    continuation-side arms refuses is an IK interior choice; what is pinned here
    is that the refusal is an access refusal that discloses no source.
    """

    with pytest.raises(EvidenceGrantRefusal) as refused:
        t14_env["evidence_narrow"].supports(PAGED_REF)
    assert refused.value.code == "access_denied"
    assert SOURCE_A2 not in str(refused.value)
    assert SOURCE_A1 not in str(refused.value)


def test_supports_continuation_not_read_is_not_on_the_row_grant_path(t14_env) -> None:
    """The EP3 placeholder is gone: a row grant answers, and refuses by fact.

    Before EP4 this path refused every row-grant supports read with
    `supports_continuation_not_read`. A page now comes back, and the refusals it
    does raise name the obligation that produced them instead.
    """

    page = t14_env["evidence"].supports(PAGED_REF)
    assert page["supports"], "a row grant answered with an empty page"
    assert "supports_continuation_not_read" not in json.dumps(page, sort_keys=True)

    refusal = _supports_refusal(t14_env, PAGED_REF, support_id=SUPPORT_FOREIGN)
    assert refusal.reason != "supports_continuation_not_read"
    assert "supports_continuation_not_read" not in json.dumps(
        {"code": refusal.code, "reason": refusal.reason, "message": str(refusal)},
        sort_keys=True,
    )
