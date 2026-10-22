"""The parser service application entrypoint.

The parser service is an independently operated HTTP surface over the
parser-owned PostgreSQL authority. ``create_app`` mounts the ``/v1``
router; ``serve`` binds it for a deployment.

Construction opens no connection: the store is built on the first
request that needs it, so importing this module starts nothing and a
misconfigured deployment fails on the request rather than at import.
"""

from __future__ import annotations

from fastapi import FastAPI

from fin.parser.api import get_job_store, router
from fin.parser.ports import ParserJobStore


def create_app(*, store: ParserJobStore | None = None) -> FastAPI:
    """Build the parser service app.

    ``store`` injects the durable authority a caller already owns (a test
    store, or a store on another database). Without it the app builds the
    store named by ``FIN_PARSER_DSN`` on first use.
    """
    app = FastAPI(
        title="fin parser service",
        version="0.1.0",
        description=(
            "Durable parse-job acceptance and status/result reads over the "
            "parser-owned PostgreSQL schema."
        ),
    )
    if store is not None:
        app.dependency_overrides[get_job_store] = lambda: store
        app.state.job_store = store
    app.include_router(router)
    return app


# The deployed service. ``uvicorn fin.parser.app:app`` resolves here.
app = create_app()


def serve() -> None:
    """Entry point for the parser service: bind and serve.

    Lazy-imports uvicorn so the module stays importable in an
    environment without the runtime.
    """
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)


__all__ = ["app", "create_app", "serve"]
