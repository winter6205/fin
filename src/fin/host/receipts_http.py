"""``GET /api/threads/{thread}/turns/{turn}/receipt`` — one turn's receipt.

A receipt is an operational record about a turn: how it ended, what it
measured, what was still unknown when it ended. An operator debugging a
stall or a refused answer needs it, and nothing else on the server
publishes it — the turn table is question and answer history, and it
carries no measurements, no cause and no revision.

Why its own route rather than a field on ``GET /api/status`` or on the
thread's own view:

* ``/api/status`` is boot-scoped and polled. A per-turn record on a
  polled surface would make every poll read a table, and would put
  run-scoped data on the one surface whose key set is pinned closed.
* ``GET /api/threads/{id}`` is the operator's conversation view. Its
  payload is the conversation; a receipt is an accounting of the run
  behind it, and folding the two together would make the history
  unreadable and put stage timings next to the answer.

The payload is the receipt and only the receipt: it carries no question,
no answer, no source identity and no credential, because the receipt
itself stores none of those. A turn with no receipt — one that predates
receipts, or a thread that was deleted — is a 404 rather than an empty
object, because "this turn has no receipt" and "this turn reported
nothing" are different facts.
"""

from __future__ import annotations

from typing import Any, Callable, cast

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse

from fin.host.thread_store import ThreadStore
from fin.host.turn_receipt_store import receipt_payload
from fin.host.turn_receipts import TurnReceiptProjector, default_receipts


def build_router(get_thread_store: Callable[[], ThreadStore]) -> APIRouter:
    """The receipt view, over the store the rest of the app writes to.

    The store dependency is injected rather than imported so the route
    reads the SAME store ``app.dependency_overrides`` redirects: a view
    built on a store of its own would report receipts nobody can write.
    """
    router = APIRouter(prefix="/api")

    @router.get("/threads/{thread_id}/turns/{turn_id}/receipt")
    def turn_receipt(
        thread_id: str,
        turn_id: str,
        store: ThreadStore = Depends(get_thread_store),
    ) -> Any:
        """This turn's receipt, or a 404 when it has none."""
        receipts: TurnReceiptProjector = default_receipts(store)
        receipt = receipts.read(thread_id, turn_id)
        if receipt is None:
            return cast(
                Any,
                JSONResponse(
                    status_code=404,
                    content={"error": "receipt_not_found"},
                ),
            )
        return receipt_payload(receipt)

    return router


def mount(app: FastAPI, get_thread_store: Callable[[], ThreadStore]) -> None:
    """Register the receipt view on an existing FastAPI app.

    Call this BEFORE the SPA catch-all: Starlette resolves routes in
    registration order, so a route registered after the static fallback
    would never be reached on a boot that serves ``web/dist``.
    """
    app.include_router(build_router(get_thread_store))


__all__ = ["build_router", "mount"]
