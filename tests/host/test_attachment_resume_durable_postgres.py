"""A suspended run survives process death on REAL Postgres (T2f, opt-in).

Spec ``thread-attachment-source-read`` SC2: "a restart test preserves the
durable parse job AND suspended run; resumes the SAME run after
rebuilding". The hermetic suites each prove one fragment — the resume
worker drives a real graph over an ``InMemorySaver`` pause, and the boot
suite re-adopted job rows over real Postgres twins — but nothing yet
drives a WAITING run's resume over a DURABLE checkpoint. This file pins
the whole chain with every stateful part on real SQL:

* the pause is persisted by a production-shaped ``PostgresSaver``
  (``from_conn_string`` + ``setup()``, the connection held open by an
  ``ExitStack`` exactly like ``fin.host.compile.compile_for_runtime``);
* "process death" is honest: the first stack is CLOSED (its psycopg
  connection really ends) and the second process reopens a FRESH
  connection, fresh store instances, a fresh service, a fresh model,
  and a graph recompiled from scratch — nothing in-process is reused
  except the InMemory byte layer (the S3 adapter's own contract lives
  in ``tests/corpus``);
* the boot sweep path — ``recover()`` re-adopting the interrupted
  ``running`` claim, the drain settling it, and the resumer's sweep
  function finding the checkpointed pause through the fresh
  checkpointer — completes the SAME run id, which is read back from the
  Postgres checkpoint, not from any surviving variable's authority;
* the resumed turn's citation-eligible evidence and its hydrated
  attachment text are both Postgres-derived: the evidence rode the
  checkpoint (the citation gate only passes if the checkpoint replayed
  it) and the sentinel text reached the new model out of the frozen
  units the new service read from ``fin_thread_attachment_unit``;
* the turn table holds EXACTLY one row for the run — a re-sweep after
  completion drives nothing and writes nothing.

Skipped when ``FIN_CHECKPOINT_URL`` is unset, the repo convention of
``tests/host/test_attachment_jobs_boot_durable_postgres.py``. Everything
is driven directly (no daemon threads), and cleanup deletes only the
rows this case created, addressed by their run-unique ids.
"""

from __future__ import annotations

import os
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from fin.corpus.extracted import Locator
from fin.corpus.object_store import InMemoryObjectStore
from fin.host import EvidenceItem, ScriptedChatModel, compile_graph
from fin.host.attachment.attachment_job_store import PostgresThreadAttachmentJobStore
from fin.host.attachment.attachment_prepare import AttachmentPreparationService
from fin.host.attachment.attachment_store import PostgresAttachmentStore
from fin.host.graph import ATTACHMENT_PREPARATION_INTERRUPT_KIND
from fin.host.run_control import RunControl
from fin.host.state import reset_input_state
from fin.host.thread_store import PostgresThreadStore
from fin.host.workspace_http import _resume_attachment_waits_once

POSTGRES_URL_ENV = "FIN_CHECKPOINT_URL"

# Row ids carry this prefix so cleanup addresses exactly this run's
# rows (and the residue of a killed earlier run) and nothing else.
ID_PREFIX = "t2f-pg-restart-"

# Grounded routing + citation shape shared with the hermetic resume
# suite: pre-seeded run evidence, a >=32-char extractive answer, and a
# sentinel only the attachment payload carries, so "the model saw it"
# proves hydrated Postgres-derived unit text reached the prompt.
GROUNDED = "What was AAPL net sales in 2023 Q3?"
KNOWN_IDS = {"AAPL-2022Q3", "AAPL-2023Q1", "MSFT-2023Q1", "AMZN-2023Q1"}
EVIDENCE_TEXT = (
    "Microsoft Corporation reported total revenue of 52,857 million dollars."
)
QUOTED_ANSWER = f"Per the filing: {EVIDENCE_TEXT[:40]}"
_ANSWER = AIMessage(content=QUOTED_ANSWER)

ATTACH_SENTINEL = "T2F-PG-RESTART-SENTINEL 89,493 million"
REVENUE_TEXT = f"Q3 revenue was {ATTACH_SENTINEL} dollars.\n"


class RecordingScriptedModel(ScriptedChatModel):
    """Scripted model that records the FULL prompt of every call.

    Same seam as ``tests/host/test_attachment_send_wait.py``: the
    workspace graph always streams and ``_stream`` delegates to
    ``_generate`` with neither stream knob set, so overriding
    ``_generate`` records both paths.
    """

    seen_calls: list[list[BaseMessage]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_calls.append(list(messages))
        return super()._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )


# ----- env / schema fixtures -------------------------------------------------


@pytest.fixture
def postgres_url() -> Iterator[str]:
    url = (os.environ.get(POSTGRES_URL_ENV) or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; the durable restart resume "
            "chain over a real checkpointer is untested in this environment"
        )
    _drop_residue(url)
    yield url
    _drop_residue(url)


_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _existing_tables(conn: Any, names: tuple[str, ...]) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
        (list(names),),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _drop_rows(conn: Any, *, thread_id: str, attachment_id: str | None) -> None:
    """Delete exactly the rows one run created, by their unique ids."""
    tables = _existing_tables(
        conn, ("fin_thread", "fin_thread_attachment", *_CHECKPOINT_TABLES)
    )
    if "fin_thread_attachment" in tables and attachment_id is not None:
        # The job table has no FK to the thread by design, so it is
        # addressed by its own key; job-table creation is guaranteed by
        # any prior store construction, hence the existence probe.
        if "fin_thread_attachment_job" in _existing_tables(
            conn, ("fin_thread_attachment_job",)
        ):
            conn.execute(
                "DELETE FROM fin_thread_attachment_job WHERE attachment_id = %s",
                (attachment_id,),
            )
    if "checkpoints" in tables:
        for table in _CHECKPOINT_TABLES:
            if table in tables:
                conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
    if "fin_thread" in tables:
        # fin_run and the attachment rows leave with it by cascade.
        conn.execute("DELETE FROM fin_thread WHERE id = %s", (thread_id,))


def _drop_residue(postgres_url: str) -> None:
    """Clear rows a killed earlier run left behind, nothing else."""
    import psycopg

    with psycopg.connect(postgres_url) as conn:
        tables = _existing_tables(conn, ("fin_thread",))
        if "fin_thread" in tables:
            doomed = [
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM fin_thread WHERE id LIKE %s", (ID_PREFIX + "%",)
                ).fetchall()
            ]
            attachment_ids: list[str] = []
            if "fin_thread_attachment" in _existing_tables(
                conn, ("fin_thread_attachment",)
            ):
                attachment_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT attachment_id FROM fin_thread_attachment "
                        "WHERE thread_id LIKE %s",
                        (ID_PREFIX + "%",),
                    ).fetchall()
                ]
            for thread_id in doomed:
                _drop_rows(conn, thread_id=thread_id, attachment_id=None)
            for attachment_id in attachment_ids:
                if "fin_thread_attachment_job" in _existing_tables(
                    conn, ("fin_thread_attachment_job",)
                ):
                    conn.execute(
                        "DELETE FROM fin_thread_attachment_job "
                        "WHERE attachment_id = %s",
                        (attachment_id,),
                    )
        else:
            # No thread table at all: nothing this suite could have written.
            attachment_ids = []
        conn.commit()


# ----- the two "processes" ----------------------------------------------------


def _open_checkpointer(postgres_url: str) -> tuple[ExitStack, Any]:
    """The production durable-checkpointer construction, imitated.

    ``compile_for_runtime`` enters ``PostgresSaver.from_conn_string`` on
    an ``ExitStack`` and runs ``setup()`` once, holding the one psycopg
    connection for the process lifetime. Closing the returned stack is
    what kills the connection, which is how this case ends a "process".
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    stack = ExitStack()
    saver = stack.enter_context(PostgresSaver.from_conn_string(postgres_url))
    saver.setup()
    return stack, saver


def _boot_process(postgres_url: str, objects: InMemoryObjectStore) -> dict[str, Any]:
    """One process's whole stack: fresh connection, stores, service, graph."""
    stack, saver = _open_checkpointer(postgres_url)
    threads = PostgresThreadStore(postgres_url)
    service = AttachmentPreparationService(
        store=PostgresAttachmentStore(postgres_url),
        objects=objects,
        jobs=PostgresThreadAttachmentJobStore(postgres_url),
    )
    model = RecordingScriptedModel(script=[_ANSWER])
    graph = compile_graph(
        checkpointer=saver,
        model=model,
        known_document_ids=KNOWN_IDS,
        attachment_service=service,
    )
    return {
        "stack": stack,
        "threads": threads,
        "service": service,
        "model": model,
        "graph": graph,
    }


def _input(attachments: list[dict[str, str]]) -> dict[str, Any]:
    state = reset_input_state(GROUNDED, attachments=attachments)
    state["evidence"] = [
        EvidenceItem(
            locator=Locator(document_id="MSFT-2023Q1", version="e" * 64, unit=1),
            unit_text=EVIDENCE_TEXT,
        )
    ]
    return state


def _model_saw(process: dict[str, Any], needle: str) -> bool:
    return any(
        needle in str(getattr(message, "content", ""))
        for call in process["model"].seen_calls
        for message in call
    )


# ----- the SC2 restart clause -------------------------------------------------


def test_suspended_run_and_durable_job_resume_over_a_fresh_postgres_process(
    tmp_path: Path, postgres_url: str
) -> None:
    """Process A pauses on the gate and dies; process B rebuilds and
    completes the SAME run over the SAME durable checkpoint.

    Every assertion below reads through process B's fresh objects only —
    the paused run, the ``running`` claim, the frozen units, and the one
    turn row all crossed the closed-connection boundary in SQL, not in
    Python.
    """
    suffix = uuid.uuid4().hex
    thread_id = f"{ID_PREFIX}{suffix}"
    run_id = f"{ID_PREFIX}-run-{suffix}"
    attachment_id: str | None = None
    sha256: str | None = None

    objects = InMemoryObjectStore()
    a = _boot_process(postgres_url, objects)
    try:
        a["threads"].create_thread(id=thread_id, title="pg restart resume")
        record = a["service"].accept(
            thread_id=thread_id, filename="notes.txt", payload=REVENUE_TEXT.encode()
        )
        attachment_id = record.attachment_id
        sha256 = record.sha256

        # The grounded send pauses on the preparation gate and the
        # interrupt lands in the POSTGRES checkpoint.
        a["graph"].invoke(
            _input(
                attachments=[
                    {
                        "attachment_id": attachment_id,
                        "filename": "notes.txt",
                        "text": "",
                    }
                ]
            ),
            config={"configurable": {"thread_id": thread_id, "run_id": run_id}},
        )
        payload = a["graph"].interrupt_pending(thread_id)
        assert payload is not None
        assert payload["kind"] == ATTACHMENT_PREPARATION_INTERRUPT_KIND
        assert payload["attachment_ids"] == [attachment_id]
        assert a["graph"].checkpoint_run_id(thread_id) == run_id
        # A paused run persists no turn row, and the model was never reached.
        assert a["threads"].list_runs(thread_id) == []
        assert a["model"].seen_calls == []
        # The gate's ask queued exactly one durable job, and the executor
        # of this era dies holding the claim (state ``running`` in SQL).
        claimed = a["service"].jobs.claim_next()
        assert claimed is not None
        assert claimed.state == "running"
    finally:
        # Process death, honestly simulated: the checkpointer connection
        # is CLOSED (not merely a new one opened alongside it).
        a["stack"].close()

    # ---- the restarted process: every part fresh over the same DSN ----
    b = _boot_process(postgres_url, objects)
    try:
        # The suspended run is readable from the FRESH checkpointer
        # connection: same run id, same gate interrupt, still no turn row.
        resumed_payload = b["graph"].interrupt_pending(thread_id)
        assert resumed_payload is not None
        assert resumed_payload["kind"] == ATTACHMENT_PREPARATION_INTERRUPT_KIND
        assert b["graph"].checkpoint_run_id(thread_id) == run_id
        assert b["threads"].list_runs(thread_id) == []
        # The ask survived too: the row reads ``parsing`` through fresh
        # store instances.
        paused = b["service"].status(attachment_id)
        assert paused is not None
        assert paused.status == "parsing"

        # The boot sweep, driven directly for determinism: recover
        # re-adopts the orphaned claim exactly once, the drain settles it.
        assert b["service"].recover() == 1
        assert b["service"].recover() == 0  # no second adoption, no duplicate
        assert b["service"].drain_attachment_jobs() >= 1
        ready = b["service"].status(attachment_id)
        assert ready is not None
        assert ready.status == "ready"
        assert ready.searchable is True
        # The frozen units are real Postgres rows the new process read back.
        units = b["service"].units(attachment_id)
        assert ATTACH_SENTINEL in "\n".join(unit.text for unit in units)
        finished = b["service"].jobs.get(
            attachment_id=attachment_id, content_sha256=sha256
        )
        assert finished is not None
        assert finished.state == "succeeded"

        # The resumer's sweep finds the checkpointed pause over the fresh
        # connection and drives the SAME run to its turn row.
        resumed = _resume_attachment_waits_once(
            b["graph"], b["threads"], b["service"], RunControl(),
            workspace_root=tmp_path,
        )
        assert resumed == 1

        rows = b["threads"].list_runs(thread_id)
        assert [row.id for row in rows] == [run_id]  # exactly one, the paused run
        assert rows[0].status == "done"
        assert rows[0].answer == QUOTED_ANSWER
        assert rows[0].citations  # evidence rode the checkpoint, not memory
        assert b["graph"].interrupt_pending(thread_id) is None
        # The turn ran on the NEW process's model, and the hydrated
        # attachment text reached its prompt out of the Postgres-derived
        # frozen units. The quoted answer above is itself the proof the
        # PRE-RESTART evidence rode the checkpoint: the citation gate
        # admits it only against the run state deserialized from SQL.
        assert _model_saw(b, ATTACH_SENTINEL)

        # A completed pause is never driven twice: the sweep settles at
        # zero and the single-row promise holds.
        assert (
            _resume_attachment_waits_once(
                b["graph"], b["threads"], b["service"], RunControl(),
                workspace_root=tmp_path,
            )
            == 0
        )
        assert len(b["threads"].list_runs(thread_id)) == 1
    finally:
        b["stack"].close()
        import psycopg

        with psycopg.connect(postgres_url) as conn:
            _drop_rows(conn, thread_id=thread_id, attachment_id=attachment_id)
            conn.commit()
