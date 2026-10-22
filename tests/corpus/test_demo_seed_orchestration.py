"""The ``seed:demo`` chain, driven entirely through injected collaborators.

Nothing here touches the dev stack, docker, or a live database: the guard
reads a plain mapping, the ports and the reset / Plan A / embed seams are
fakes, and the only real process is the worker-teardown case, which proves
the ``finally`` SIGTERM-and-wait path against a genuine child process.

The fixture corpus is built inline under ``tmp_path`` so this file does not
depend on ``fixtures/demo-corpus/`` (written concurrently by another task).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from fin.corpus import demo_seed
from fin.corpus.catalog import list_filings, record_filings
from fin.corpus.demo_seed import (
    DEV_STACK_PORTS,
    DemoDocument,
    DemoNeo4jUnavailableError,
    ElasticsearchIndexReset,
    PostgresEmbedProbe,
    PostgresStoreReset,
    Neo4jGraphReset,
    EXPECTED_FIXTURE_NAME,
    DemoDefaultMemoryHomeError,
    DemoEmbeddingMismatchError,
    DemoFixtureError,
    DemoFixtureMissingError,
    DemoJobFailedError,
    DemoMemoryHomeUnsetError,
    DemoNonPersistentStorageError,
    DemoPollTimeoutError,
    DemoPortMismatchError,
    DemoSeedDeps,
    DemoSeedOptions,
    DemoUnit,
    EmbedGeneration,
    EmbedLimitBreachedError,
    PlanADevStackAdapter,
    PlanAEntryPointError,
    ReusedFailedJobError,
    SubprocessWorker,
    clear_demo_catalog_rows,
    content_identity_of,
    demo_catalog_rows,
    guard_dev_stack,
    load_demo_documents,
    probe_embedding_generation,
    run_demo_seed,
)
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import deserialize_source_ref
from fin.retrieve.index_job_port import (
    CommunityIndexState,
    IndexJobPoll,
    IndexJobRecord,
    IndexJobSubmission,
)
from fin.retrieve.push_port import PushPort

# ---------------------------------------------------------------------------
# fixture corpus, built inline
# ---------------------------------------------------------------------------


def _envelope(
    source_ref: str,
    *,
    document_type: str = "handbook",
    page_count: int = 2,
    chunks: int = 2,
    form: str = "10-K",
) -> dict[str, Any]:
    """One ingest envelope in the fixture file shape.

    A chunk carries the fixture's page marker (``Title · Page N``) the way the
    shipped corpus does: the seed reads the page from it and refuses a chunk
    whose heading and ordinal disagree.
    """
    pages = [
        f"ZEUS {source_ref} · Page {index + 1}\n"
        f"Section {index + 1} of the demo page."
        for index in range(chunks)
    ]
    return {
        "source_ref": source_ref,
        "title": f"ZEUS {source_ref}",
        "content": "\n".join(pages),
        "chunks": [
            {"content": page, "index": index}
            for index, page in enumerate(pages)
        ],
        # `form` is the EDGAR column the catalog filters by exact match, so the
        # seed refuses to write a row without it. `document_type` stays the
        # prose category and still reaches the push envelope as `doc_type`.
        "metadata": {
            "document_type": document_type,
            "page_count": page_count,
            "form": form,
        },
        "valid_from": "2026-01-05T00:00:00.000Z",
    }


def _fixture_dir(root: Path, *envelopes: dict[str, Any], **kwargs: Any) -> Path:
    directory = root / "demo-corpus"
    directory.mkdir(parents=True, exist_ok=True)
    for envelope in envelopes:
        (directory / f"{envelope['source_ref']}.json").write_text(
            json.dumps(envelope), encoding="utf-8"
        )
    if kwargs.get("expected"):
        (directory / "expected.json").write_text(
            json.dumps({"questions": []}), encoding="utf-8"
        )
    return directory


def _fixture(tmp_path: Path) -> Path:
    return _fixture_dir(
        tmp_path,
        _envelope("zeus-board-minutes", document_type="minutes", page_count=4),
        _envelope("zeus-handbook", document_type="handbook", page_count=2),
        expected=True,
    )


def _document_of_fixture(root: Path, document_id: str) -> DemoDocument:
    """The fixture document the seed reads under ``document_id``."""
    for document in load_demo_documents(_fixture(root)):
        if document.document_id == document_id:
            return document
    raise AssertionError(f"the fixture holds no document {document_id!r}")


def _units_of(root: Path, document_id: str) -> tuple[DemoUnit, ...]:
    """The frozen units the seed pushes for ``document_id``, one per page."""
    return _document_of_fixture(root, document_id).units


def _source_refs_of(root: Path, document_id: str) -> tuple[str, ...]:
    """The §B4 wire ``source_ref`` the seed pushes, one per unit."""
    return tuple(unit.source_ref for unit in _units_of(root, document_id))


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------


def _dev_env(**overrides: str) -> dict[str, str]:
    env = {
        "IKNOW_MEMORY_HOME": "/srv/iknow-memory-dev",
        "GRAPHRAG_MEMORY_DB_URL": "postgresql://user@127.0.0.1:5434/iknow_memory",
        "GRAPHRAG_MEMORY_ES_URL": "http://127.0.0.1:9201",
        "GRAPHRAG_MEMORY_NEO4J_URI": "bolt://127.0.0.1:7688",
        "GRAPHRAG_MEMORY_STORAGE": "pgvector",
    }
    env.update(overrides)
    return env


def test_guard_accepts_the_isolated_dev_stack() -> None:
    memory_home = guard_dev_stack(
        _dev_env(), user_home=Path("/home/operator")
    )

    assert memory_home == "/srv/iknow-memory-dev"
    assert DEV_STACK_PORTS == {
        "postgres": 5434,
        "elasticsearch": 9201,
        "neo4j": 7688,
    }


def test_guard_refuses_when_memory_home_is_unset() -> None:
    env = _dev_env()
    del env["IKNOW_MEMORY_HOME"]

    with pytest.raises(DemoMemoryHomeUnsetError) as raised:
        guard_dev_stack(env, user_home=Path("/home/operator"))

    assert raised.value.code == "demo_seed_memory_home_unset"
    assert raised.value.exit_code == 2


def test_guard_refuses_the_default_memory_home() -> None:
    env = _dev_env(IKNOW_MEMORY_HOME="/home/operator/.iknow-memory")

    with pytest.raises(DemoDefaultMemoryHomeError) as raised:
        guard_dev_stack(env, user_home=Path("/home/operator"))

    assert raised.value.code == "demo_seed_default_memory_home"
    assert raised.value.exit_code == 2


def test_guard_refuses_a_trailing_slash_on_the_default_memory_home() -> None:
    """The same directory reached by a different path is still the
    default directory, so the refusal is not a string comparison."""
    env = _dev_env(IKNOW_MEMORY_HOME="/home/operator/.iknow-memory/")

    with pytest.raises(DemoDefaultMemoryHomeError):
        guard_dev_stack(env, user_home=Path("/home/operator"))


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GRAPHRAG_MEMORY_DB_URL", "postgresql://user@127.0.0.1:5432/iknow_memory"),
        ("GRAPHRAG_MEMORY_ES_URL", "http://127.0.0.1:9200"),
        ("GRAPHRAG_MEMORY_NEO4J_URI", "bolt://127.0.0.1:7687"),
    ],
)
def test_guard_refuses_a_wrong_port(variable: str, value: str) -> None:
    with pytest.raises(DemoPortMismatchError) as raised:
        guard_dev_stack(_dev_env(**{variable: value}), user_home=Path("/home/x"))

    assert raised.value.code == "demo_seed_port_mismatch"
    assert raised.value.exit_code == 2
    assert variable in str(raised.value)


def test_guard_refuses_an_unset_port_variable() -> None:
    env = _dev_env()
    del env["GRAPHRAG_MEMORY_ES_URL"]

    with pytest.raises(DemoPortMismatchError) as raised:
        guard_dev_stack(env, user_home=Path("/home/x"))

    assert "GRAPHRAG_MEMORY_ES_URL" in str(raised.value)


def test_guard_refuses_the_unset_in_memory_storage_default() -> None:
    """iknow resolves ``memory`` when the selector is unset, and that backend
    accepts rows without keeping any, so an unset selector is a refusal."""
    env = _dev_env()
    del env["GRAPHRAG_MEMORY_STORAGE"]

    with pytest.raises(DemoNonPersistentStorageError) as raised:
        guard_dev_stack(env, user_home=Path("/home/x"))

    assert raised.value.code == "demo_seed_storage_not_persistent"
    assert raised.value.exit_code == 2
    assert "GRAPHRAG_MEMORY_STORAGE" in str(raised.value)
    assert "memory" in str(raised.value)


def test_guard_refuses_a_storage_backend_it_cannot_prove_persists() -> None:
    env = _dev_env(GRAPHRAG_MEMORY_STORAGE="memory")

    with pytest.raises(DemoNonPersistentStorageError) as raised:
        guard_dev_stack(env, user_home=Path("/home/x"))

    assert "memory" in str(raised.value)


# ---------------------------------------------------------------------------
# embed probe
# ---------------------------------------------------------------------------


class _StubEmbedProbe:
    def __init__(self, generation: EmbedGeneration) -> None:
        self.generation = generation
        self.calls = 0

    def read_embedding_generation(self) -> EmbedGeneration:
        self.calls += 1
        return self.generation


def test_probe_accepts_the_declared_dimension_and_generation() -> None:
    probe = _StubEmbedProbe(EmbedGeneration(dimensions=1536, generation="e5-small-v1"))

    observed = probe_embedding_generation(
        probe, expected_dimensions=1536, expected_generation="e5-small-v1"
    )

    assert observed == EmbedGeneration(dimensions=1536, generation="e5-small-v1")
    assert probe.calls == 1


def test_probe_refuses_a_mismatched_embedding_generation() -> None:
    probe = _StubEmbedProbe(EmbedGeneration(dimensions=1536, generation="e5-small-v2"))

    with pytest.raises(DemoEmbeddingMismatchError) as raised:
        probe_embedding_generation(
            probe, expected_dimensions=1536, expected_generation="e5-small-v1"
        )

    assert raised.value.code == "demo_seed_embedding_mismatch"
    assert raised.value.exit_code == 2


def test_probe_refuses_a_mismatched_dimension() -> None:
    probe = _StubEmbedProbe(EmbedGeneration(dimensions=768, generation="e5-small-v1"))

    with pytest.raises(DemoEmbeddingMismatchError) as raised:
        probe_embedding_generation(
            probe, expected_dimensions=1536, expected_generation="e5-small-v1"
        )

    assert "768" in str(raised.value)


# ---------------------------------------------------------------------------
# fixture + content identity
# ---------------------------------------------------------------------------


def test_documents_are_read_from_the_directory_in_document_id_order(
    tmp_path: Path,
) -> None:
    documents = load_demo_documents(_fixture(tmp_path))

    assert [document.document_id for document in documents] == [
        "zeus-board-minutes",
        "zeus-handbook",
    ]
    handbook = documents[1]
    assert handbook.document_type == "handbook"
    assert handbook.page_count == 2
    assert len(handbook.chunks) == 2


def test_expected_json_is_not_a_document(tmp_path: Path) -> None:
    directory = _fixture(tmp_path)

    assert EXPECTED_FIXTURE_NAME in {path.name for path in directory.iterdir()}
    assert all(
        document.document_id != "expected"
        for document in load_demo_documents(directory)
    )


def test_missing_fixture_directory_is_named(tmp_path: Path) -> None:
    missing = tmp_path / "absent"

    with pytest.raises(DemoFixtureMissingError) as raised:
        load_demo_documents(missing)

    assert raised.value.code == "demo_seed_fixture_missing"
    assert str(missing) in str(raised.value)


def test_envelope_without_chunks_is_named(tmp_path: Path) -> None:
    broken = _envelope("zeus-handbook")
    del broken["chunks"]
    directory = _fixture_dir(tmp_path, broken)

    with pytest.raises(DemoFixtureError) as raised:
        load_demo_documents(directory)

    assert raised.value.code == "demo_seed_fixture_invalid"
    assert "zeus-handbook" in str(raised.value)


def test_envelope_without_page_count_is_named(tmp_path: Path) -> None:
    broken = _envelope("zeus-handbook")
    del broken["metadata"]["page_count"]
    directory = _fixture_dir(tmp_path, broken)

    with pytest.raises(DemoFixtureError) as raised:
        load_demo_documents(directory)

    assert "page_count" in str(raised.value)


def test_a_chunk_index_below_zero_is_named(tmp_path: Path) -> None:
    """The unit is the chunk's 1-based ordinal, so the index must be >= 0.

    A negative index would derive ``unit <= 0`` -- a value fin's own
    canonical parser rejects and the host's route forbids.
    """
    broken = _envelope("zeus-handbook")
    broken["chunks"][0]["index"] = -1
    directory = _fixture_dir(tmp_path, broken)

    with pytest.raises(DemoFixtureError) as raised:
        load_demo_documents(directory)

    assert "non-negative" in str(raised.value)


def test_a_chunk_whose_page_disagrees_with_its_ordinal_is_named(
    tmp_path: Path,
) -> None:
    """The heading's page and the chunk's ordinal must be the same page.

    The wire ``unit`` is the ordinal and the host's ``page`` is the heading;
    the web resolves a citation only when the two agree, so a fixture whose
    heading and ordinal disagree would push a locator nothing can resolve.
    """
    broken = _envelope("zeus-handbook")
    broken["chunks"][1]["content"] = "ZEUS zeus-handbook · Page 7"
    directory = _fixture_dir(tmp_path, broken)

    with pytest.raises(DemoFixtureError) as raised:
        load_demo_documents(directory)

    assert "heads page 7" in str(raised.value)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "",
            "72a3086a323a18f665ec77d4ec70db0ca5119e994346a99571f6ebf81a3bdc04",
        ),
        (
            "hello",
            "e4691eead0cca21474505513ffd9d811fb02789e3252174b3ed534c15faf3fc8",
        ),
        (
            "ZEUS handbook §1",
            "4905086b30e5151b350947394ee2547e8eb7c86f8ee527c8860c9095bf67e489",
        ),
        (
            '行 "quoted"\nline2\tend',
            "2ba42d06f71ff42c456e5a168439d75e070c70943b612da23cdc160746738ee6",
        ),
        (
            "混排 unicode ✓",
            "4ad2034ad00087197b3bbd4b81610ca2b0ef3ef38c37deec65405999e3c8e20c",
        ),
    ],
)
def test_content_identity_matches_the_service_golden_vectors(
    content: str, expected: str
) -> None:
    """The vectors were produced by the service's own
    ``computeContentIdentity`` (iknow-memory ``src/tools/ingest.ts``), so a
    drift in fin's port of the formula fails here rather than as an
    ``index_job_identity_mismatch`` at seed time."""
    assert content_identity_of(content) == expected


def test_unit_content_identity_is_the_service_formula(tmp_path: Path) -> None:
    """The service validates the pair against the body it stored, so the
    seed's identity has to be the service's own formula over that body."""
    document = load_demo_documents(_fixture(tmp_path))[1]

    for unit in document.units:
        assert unit.content_identity == content_identity_of(unit.content)


# ---------------------------------------------------------------------------
# catalog rows generated from the fixture
# ---------------------------------------------------------------------------


def test_catalog_rows_come_from_the_fixture(tmp_path: Path) -> None:
    rows = demo_catalog_rows(_fixture(tmp_path))

    assert [row["document_id"] for row in rows] == [
        "zeus-board-minutes",
        "zeus-handbook",
    ]
    # `form` is the EDGAR column the catalog filters by exact match, so it
    # carries the fixture's `form`, not the prose `document_type`. The
    # document_type is not dropped -- it reaches the push envelope as
    # `doc_type`, asserted by
    # test_pushed_envelope_carries_the_sc20_metadata_surface below.
    assert [row["form"] for row in rows] == ["10-K", "10-K"]
    assert [row["page_count"] for row in rows] == [4, 2]
    assert {row["company"] for row in rows} == {"ZEUS"}


def test_a_catalog_row_carries_the_version_of_the_pushed_locator(
    tmp_path: Path,
) -> None:
    """The catalog's ``(document_id, version)`` is the join key the host uses
    to resolve a hit's locator to a catalog row."""
    documents = load_demo_documents(_fixture(tmp_path))
    rows = demo_catalog_rows(_fixture(tmp_path))

    assert [(row["document_id"], row["version"]) for row in rows] == [
        (document.document_id, document.source_sha256) for document in documents
    ]
    for row, document in zip(rows, documents, strict=True):
        for unit in document.units:
            locator = deserialize_source_ref(unit.source_ref)
            assert (locator.document_id, locator.version) == (
                row["document_id"],
                row["version"],
            )


def test_a_reseed_replaces_the_demo_rows_it_owns(tmp_path: Path) -> None:
    """A version change must not leave a second row for the same document.

    The catalog upserts on ``(document_id, version)``, so the row an older
    fixture wrote stayed next to the new one. The seed owns the rows of the
    documents the fixture holds and replaces them; the row of a document it
    does not own is carried through untouched.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "zeus-handbook",
                "title": "Earlier Zeus Handbook",
                "doc_type": "handbook",
                "company": "ZEUS",
                "period": "2026-01-05",
                "version": "b" * 64,
                "page_count": 2,
                "form": "10-K",
            },
            {
                "document_id": "AAPL-2023Q3",
                "title": "Apple Q3 2023",
                "doc_type": "financial_report",
                "company": "AAPL",
                "period": "2023 Q3",
                "version": "a" * 64,
                "page_count": 1,
                "form": "10-Q",
            },
        ],
        catalog_path=catalog_path,
    )

    written, _ = demo_seed.seed_demo_catalog(
        catalog_path, fixture_dir=_fixture(tmp_path)
    )

    assert written == 2
    filings = list_filings(catalog_path=catalog_path)["filings"]
    assert [filing["document_id"] for filing in filings] == [
        "AAPL-2023Q3",
        "zeus-board-minutes",
        "zeus-handbook",
    ]
    handbook = next(
        filing for filing in filings if filing["document_id"] == "zeus-handbook"
    )
    assert handbook["version"] == _document_of_fixture(
        tmp_path, "zeus-handbook"
    ).locator_version


def test_clear_removes_only_the_demo_rows(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "AAPL-2023Q3",
                "company": "AAPL",
                "period": "2023 Q3",
                "version": "a" * 64,
                "page_count": 1,
                "form": "10-Q",
                "title": "AAPL 2023Q3",
                "doc_type": "narrative",
            }
        ],
        catalog_path=catalog_path,
    )
    demo_seed.seed_demo_catalog(catalog_path, fixture_dir=_fixture(tmp_path))

    removed = clear_demo_catalog_rows(
        catalog_path, document_ids=["zeus-handbook", "zeus-board-minutes"]
    )

    assert removed == 2
    assert [
        filing["document_id"]
        for filing in demo_seed.list_filings(catalog_path=catalog_path)["filings"]
    ] == ["AAPL-2023Q3"]


def test_clear_of_an_absent_catalog_is_zero_rows(tmp_path: Path) -> None:
    catalog_path = tmp_path / "absent.json"

    assert clear_demo_catalog_rows(catalog_path, document_ids=["zeus-handbook"]) == 0


# ---------------------------------------------------------------------------
# Plan A adapter
# ---------------------------------------------------------------------------


def _iknow_root(root: Path, scripts: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        json.dumps({"name": "iknow-memory", "scripts": scripts}), encoding="utf-8"
    )
    return root


def test_plan_a_adapter_names_the_missing_entry_points(tmp_path: Path) -> None:
    root = _iknow_root(tmp_path / "iknow", {"index:worker": "node dist/w.js"})
    adapter = PlanADevStackAdapter(root)

    assert adapter.missing_entry_points() == ("index:reset-job", "migrate:dev")
    with pytest.raises(PlanAEntryPointError) as raised:
        adapter.require_entry_points()
    assert "index:reset-job" in str(raised.value)
    assert "migrate:dev" in str(raised.value)


def test_plan_a_adapter_builds_the_agreed_commands(tmp_path: Path) -> None:
    root = _iknow_root(
        tmp_path / "iknow",
        {
            "index:worker": "node dist/cli/index-job-worker.js",
            "index:reset-job": "node dist/cli/index-job-reset.js",
            "migrate:dev": "node dist/cli/migrate-dev.js",
        },
    )
    adapter = PlanADevStackAdapter(root)

    assert adapter.missing_entry_points() == ()
    assert adapter.reset_job_command("job-1") == [
        "npm",
        "run",
        "index:reset-job",
        "--",
        "--job-id",
        "job-1",
    ]
    assert adapter.schema_command() == ["npm", "run", "migrate:dev"]


def test_plan_a_reset_job_refuses_a_nonzero_exit(tmp_path: Path) -> None:
    root = _iknow_root(
        tmp_path / "iknow",
        {"index:reset-job": "false", "migrate:dev": "true"},
    )
    adapter = PlanADevStackAdapter(
        root, environ={**os.environ, "IKNOW_MEMORY_HOME": "/tmp/home"}
    )

    with pytest.raises(PlanAEntryPointError) as raised:
        adapter.reset_job("job-1")

    assert "index:reset-job" in str(raised.value)


def test_plan_a_data_table_allowlist_is_not_yet_published(tmp_path: Path) -> None:
    root = _iknow_root(
        tmp_path / "iknow",
        {"index:reset-job": "true", "migrate:dev": "true"},
    )
    adapter = PlanADevStackAdapter(root)

    with pytest.raises(PlanAEntryPointError) as raised:
        adapter.data_table_allowlist()

    assert "allowlist" in str(raised.value)


# ---------------------------------------------------------------------------
# chain doubles
# ---------------------------------------------------------------------------


class _RecordingResetPort:
    """Records the reset calls, in the order the chain makes them."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def truncate_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        self.calls.append(f"truncate:{','.join(tables)}")
        return tuple(tables)

    def delete_elasticsearch_indices(self) -> tuple[str, ...]:
        self.calls.append("delete-es")
        return ("chunks",)

    def clear_neo4j_graph(self) -> tuple[str, ...]:
        self.calls.append("clear-neo4j")
        return ("1",)


class _RecordingPlanA:
    """The Plan A half, scripted: schema applied, allowlist published."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply_schema(self) -> None:
        self.calls.append("schema")

    def data_table_allowlist(self) -> tuple[str, ...]:
        return ("chunks", "entities", "index_jobs")

    def reset_job_command(self, job_id: str) -> list[str]:
        return ["npm", "run", "index:reset-job", "--", "--job-id", job_id]


class _RecordingPushPort:
    def __init__(self) -> None:
        self.pushed: list[dict[str, Any]] = []

    def push(self, envelopes: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        self.pushed.extend(envelopes)
        return {
            "block_ids": ["b1"],
            "written": len(envelopes),
            "duplicates": 0,
            "content_identities": {},
        }


class _RememberingIndexJobPort:
    """A service that remembers the sources it was pushed and the jobs it ran.

    A source the service holds is joined instead of pushed again, and the job
    id is derived from the durable ``(source_ref, content_identity)`` pair, so
    the same unit is the same job in every run.
    """

    def __init__(self) -> None:
        self.known: set[str] = set()
        self.jobs: dict[tuple[str, str], str] = {}
        self.records: dict[str, IndexJobRecord] = {}
        self.submit_calls: list[tuple[str, str]] = []

    def submit_index_job(
        self, source_ref: str, content_identity: str
    ) -> IndexJobSubmission:
        self.submit_calls.append((source_ref, content_identity))
        if source_ref not in self.known:
            raise demo_seed.IndexJobUnknownSourceError(
                f"no pushed 资料 with source_ref {source_ref}"
            )
        pair = (source_ref, content_identity)
        job_id = self.jobs.setdefault(pair, f"job-{len(self.jobs) + 1}")
        record = self.records.setdefault(
            job_id,
            IndexJobRecord(
                job_id=job_id,
                source_ref=source_ref,
                content_identity=content_identity,
                status="in_progress",
                started_at=None,
                finished_at=None,
                failure_code=None,
            ),
        )
        return IndexJobSubmission(record=record, reused=record.status == "ready")

    def poll_index_job(self, job_id: str) -> IndexJobPoll:
        record = self.records[job_id]
        settled = replace(
            record, status="ready", finished_at="2026-10-15T00:00:00Z"
        )
        self.records[job_id] = settled
        return IndexJobPoll(
            record=settled,
            community_index_state=CommunityIndexState("ready", None),
        )


class _RememberingPushPort:
    """A push port that hands the pushed source refs to the service memory."""

    def __init__(self, service: _RememberingIndexJobPort) -> None:
        self.service = service
        self.pushed: list[dict[str, Any]] = []

    def push(self, envelopes: tuple[dict[str, Any], ...]) -> dict[str, Any]:
        self.pushed.extend(envelopes)
        for envelope in envelopes:
            self.service.known.add(str(envelope["source_ref"]))
        return {
            "block_ids": ["b1"],
            "written": len(envelopes),
            "duplicates": 0,
            "content_identities": {},
        }


def _record(
    job_id: str,
    source_ref: str,
    status: str,
    *,
    failure_code: str | None = None,
) -> IndexJobRecord:
    return IndexJobRecord(
        job_id=job_id,
        source_ref=source_ref,
        content_identity="c" * 64,
        status=status,
        started_at=None,
        finished_at=None if status == "in_progress" else "2026-10-15T00:00:00Z",
        failure_code=failure_code,
    )


class _ScriptedIndexJobPort:
    """Answers submit from a per-source script and poll from a per-round
    script, so a chain test states the job timeline instead of racing one.

    The script is keyed by the canonical ``source_ref`` (§B4): the seed pushes
    one envelope per unit, so the service holds one job per unit and is
    addressed by that unit's source ref.
    """

    def __init__(
        self,
        submissions: dict[str, IndexJobSubmission],
        *,
        unknown_sources: tuple[str, ...] = (),
        polls: list[dict[str, IndexJobRecord]] | None = None,
    ) -> None:
        self._submissions = submissions
        self._unknown_sources = unknown_sources
        self._polls = polls or []
        self._round_index = 0
        self._seen_in_round: set[str] = set()
        self._source_refs: dict[str, str] = {}
        self.submit_calls: list[tuple[str, str]] = []
        self.poll_calls: list[str] = []

    def submit_index_job(
        self, source_ref: str, content_identity: str
    ) -> IndexJobSubmission:
        self.submit_calls.append((source_ref, content_identity))
        if source_ref in self._unknown_sources:
            self._unknown_sources = tuple(
                candidate
                for candidate in self._unknown_sources
                if candidate != source_ref
            )
            raise demo_seed.IndexJobUnknownSourceError(
                f"no pushed 资料 with source_ref {source_ref}"
            )
        submission = self._submissions[source_ref]
        self._source_refs[submission.record.job_id] = source_ref
        return submission

    def poll_index_job(self, job_id: str) -> IndexJobPoll:
        self.poll_calls.append(job_id)
        step = (
            self._polls[min(self._round_index, len(self._polls) - 1)]
            if self._polls
            else {}
        )
        self._seen_in_round.add(job_id)
        if self._seen_in_round >= set(step):
            self._seen_in_round = set()
            self._round_index = min(self._round_index + 1, len(self._polls) - 1)
        submitted = next(
            submission.record
            for submission in self._submissions.values()
            if submission.record.job_id == job_id
        )
        record = step.get(job_id, submitted)
        return IndexJobPoll(
            record=replace(
                record,
                source_ref=self._source_refs.get(job_id, record.source_ref),
            ),
            community_index_state=CommunityIndexState("in_progress", None),
        )


@dataclass
class _FakeWorker:
    """A worker that records its lifecycle without starting a process."""

    pid: int = 424242
    terminated: list[tuple[float, int]] = field(default_factory=list)
    started: list[str] = field(default_factory=list)

    def start(self, *, memory_home: str) -> int:
        self.started.append(memory_home)
        return self.pid

    def terminate_and_wait(self, *, timeout_seconds: float, open_jobs: int = 0) -> int:
        self.terminated.append((timeout_seconds, open_jobs))
        return 0


def _options(tmp_path: Path, **overrides: Any) -> DemoSeedOptions:
    values: dict[str, Any] = {
        "fixture_dir": _fixture(tmp_path),
        "catalog_path": tmp_path / "catalog.json",
        "expected_dimensions": 1536,
        "expected_generation": "e5-small-v1",
        "user_home": Path("/home/operator"),
        "poll_timeout_seconds": 30.0,
        "poll_interval_seconds": 0.0,
    }
    values.update(overrides)
    return DemoSeedOptions(**values)


def _deps(
    *,
    index_job_port: Any,
    push_port: Any = None,
    reset_port: Any = None,
    plan_a: Any = None,
    probe: Any = None,
    worker: Any = None,
    environ: dict[str, str] | None = None,
    clock: Any = None,
    sleep: Any = None,
) -> DemoSeedDeps:
    ticks: list[float] = []

    def _clock() -> float:
        ticks.append(len(ticks) + 1.0)
        return ticks[-1]

    return DemoSeedDeps(
        environ=_dev_env() if environ is None else environ,
        push_port=push_port if push_port is not None else _RecordingPushPort(),
        index_job_port=index_job_port,
        reset_port=reset_port if reset_port is not None else _RecordingResetPort(),
        plan_a=plan_a if plan_a is not None else _RecordingPlanA(),
        embed_probe=(
            probe
            if probe is not None
            else _StubEmbedProbe(
                EmbedGeneration(dimensions=1536, generation="e5-small-v1")
            )
        ),
        worker_factory=(
            (lambda: worker)
            if worker is not None
            else (lambda: _FakeWorker())
        ),
        clock=_clock if clock is None else clock,
        sleep=(lambda _seconds: None) if sleep is None else sleep,
    )


#: The job id prefix the scripted service hands out per document. The seed
#: pushes one envelope per unit, so the service holds one job per unit.
_JOB_PREFIX: dict[str, str] = {
    "zeus-board-minutes": "job-minutes",
    "zeus-handbook": "job-handbook",
}


def _job_id(document_id: str, unit: int) -> str:
    """The job id the scripted service hands out for one unit."""
    return f"{_JOB_PREFIX[document_id]}-{unit}"


def _units(root: Path) -> tuple[tuple[str, str, int], ...]:
    """``(job_id, source_ref, unit)`` for every unit the seed pushes."""
    return tuple(
        (_job_id(document_id, unit.unit), unit.source_ref, unit.unit)
        for document_id in _JOB_PREFIX
        for unit in _units_of(root, document_id)
    )


def _submissions(
    root: Path,
    status: str = "in_progress",
    *,
    reused: bool = False,
) -> dict[str, IndexJobSubmission]:
    """One submission per unit, keyed by the unit's source ref."""
    return {
        source_ref: IndexJobSubmission(
            record=_record(job_id, source_ref, status), reused=reused
        )
        for job_id, source_ref, _ in _units(root)
    }


def _polls(root: Path, status: str) -> dict[str, IndexJobRecord]:
    """The poll round in which every submitted job carries ``status``."""
    return {
        job_id: _record(job_id, source_ref, status)
        for job_id, source_ref, _ in _units(root)
    }


# ---------------------------------------------------------------------------
# chain: preview
# ---------------------------------------------------------------------------


def test_preview_writes_nothing(tmp_path: Path) -> None:
    reset_port = _RecordingResetPort()
    plan_a = _RecordingPlanA()
    push_port = _RecordingPushPort()
    worker = _FakeWorker()
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path))

    report = run_demo_seed(
        _deps(
            index_job_port=index_job_port,
            push_port=push_port,
            reset_port=reset_port,
            plan_a=plan_a,
            worker=worker,
        ),
        _options(tmp_path, reset=True),
    )

    assert report.wrote is False
    # One job per unit, not one per document: the seed pushes one envelope
    # per page, so a two-page document is two jobs.
    assert [(outcome.document_id, outcome.unit) for outcome in report.jobs] == [
        ("zeus-board-minutes", 1),
        ("zeus-board-minutes", 2),
        ("zeus-handbook", 1),
        ("zeus-handbook", 2),
    ]
    # A preview submitted nothing, so it has no job id to report.
    assert [outcome.job_id for outcome in report.jobs] == ["", "", "", ""]
    assert reset_port.calls == []
    # The schema entry point sits before the preview gate in the spec's order
    # and is an idempotent ``IF NOT EXISTS`` migration the embed probe needs;
    # nothing data-shaped was written.
    assert plan_a.calls == ["schema"]
    assert push_port.pushed == []
    assert index_job_port.submit_calls == []
    assert worker.started == []
    assert worker.terminated == []
    assert not (tmp_path / "catalog.json").exists()
    assert report.catalog_rows_written == 0


# ---------------------------------------------------------------------------
# chain: write path
# ---------------------------------------------------------------------------


def test_yes_runs_the_chain_in_order_and_records_the_jobs(tmp_path: Path) -> None:
    reset_port = _RecordingResetPort()
    plan_a = _RecordingPlanA()
    push_port = _RecordingPushPort()
    worker = _FakeWorker()
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        polls=[_polls(tmp_path, "ready")],
    )

    report = run_demo_seed(
        _deps(
            index_job_port=index_job_port,
            push_port=push_port,
            reset_port=reset_port,
            plan_a=plan_a,
            worker=worker,
        ),
        _options(tmp_path, reset=True, write=True),
    )

    assert report.wrote is True
    assert reset_port.calls == [
        "truncate:chunks,entities,index_jobs",
        "delete-es",
        "clear-neo4j",
    ]
    assert plan_a.calls == ["schema"]
    assert worker.started == ["/srv/iknow-memory-dev"]
    assert worker.terminated and worker.terminated[0][0] > 0
    # Zero, not four: every job the run submitted was driven to ready, so the
    # count the teardown reports is what was still open at that moment.
    assert worker.terminated[0][1] == 0
    # Four jobs, one per unit: the two documents carry two pages each.
    assert [outcome.status for outcome in report.jobs] == ["ready"] * 4
    assert [outcome.skipped for outcome in report.jobs] == [False] * 4
    assert report.catalog_rows_written == 2
    assert [
        filing["document_id"]
        for filing in demo_seed.list_filings(
            catalog_path=tmp_path / "catalog.json"
        )["filings"]
    ] == ["zeus-board-minutes", "zeus-handbook"]


def test_yes_without_reset_writes_no_reset_calls(tmp_path: Path) -> None:
    reset_port = _RecordingResetPort()
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        polls=[_polls(tmp_path, "ready")],
    )

    run_demo_seed(
        _deps(index_job_port=index_job_port, reset_port=reset_port),
        _options(tmp_path, write=True),
    )

    assert reset_port.calls == []


def test_reused_succeeded_is_recorded_as_skipped(tmp_path: Path) -> None:
    submissions = _submissions(tmp_path, "ready", reused=True)
    worker = _FakeWorker()

    report = run_demo_seed(
        _deps(index_job_port=_ScriptedIndexJobPort(submissions), worker=worker),
        _options(tmp_path, write=True),
    )

    assert [outcome.skipped for outcome in report.jobs] == [True] * 4
    assert all(outcome.reused for outcome in report.jobs)
    # Nothing was pending, so the worker was never started.
    assert worker.started == []
    assert worker.terminated == []


def test_mixed_reused_and_pending_jobs_preserve_every_outcome(
    tmp_path: Path,
) -> None:
    submissions = _submissions(tmp_path)
    first_job_id, first_source_ref, _ = _units(tmp_path)[0]
    submissions[first_source_ref] = IndexJobSubmission(
        record=_record(first_job_id, first_source_ref, "ready"), reused=True
    )
    worker = _FakeWorker()
    index_job_port = _ScriptedIndexJobPort(
        submissions, polls=[_polls(tmp_path, "ready")]
    )

    report = run_demo_seed(
        _deps(index_job_port=index_job_port, worker=worker),
        _options(tmp_path, write=True),
    )

    assert [
        (outcome.job_id, outcome.status, outcome.skipped, outcome.reused)
        for outcome in report.jobs
    ] == [
        (job_id, "ready", index == 0, index == 0)
        for index, (job_id, _, _) in enumerate(_units(tmp_path))
    ]
    assert len(report.jobs) == len(_units(tmp_path))


def test_reused_failed_stops_before_the_worker_and_names_the_reset_command(
    tmp_path: Path,
) -> None:
    submissions = _submissions(tmp_path, reused=True)
    failed = _job_id("zeus-board-minutes", 1)
    submissions[_source_refs_of(tmp_path, "zeus-board-minutes")[0]] = (
        IndexJobSubmission(
            record=_record(
                failed,
                _source_refs_of(tmp_path, "zeus-board-minutes")[0],
                "failed",
                failure_code="entity_merge_failed",
            ),
            reused=True,
        )
    )
    worker = _FakeWorker()

    with pytest.raises(ReusedFailedJobError) as raised:
        run_demo_seed(
            _deps(index_job_port=_ScriptedIndexJobPort(submissions), worker=worker),
            _options(tmp_path, write=True),
        )

    message = str(raised.value)
    assert raised.value.code == "demo_seed_reused_failed_job"
    assert failed in message
    assert "index:reset-job" in message
    assert "entity_merge_failed" in message
    assert worker.started == []


def test_unknown_source_is_ingested_before_the_resubmit(tmp_path: Path) -> None:
    push_port = _RecordingPushPort()
    unknown = _source_refs_of(tmp_path, "zeus-handbook")[0]
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        unknown_sources=(unknown,),
        polls=[_polls(tmp_path, "ready")],
    )

    report = run_demo_seed(
        _deps(index_job_port=index_job_port, push_port=push_port),
        _options(tmp_path, write=True),
    )

    # The service is addressed by the §B4 ``source_ref`` -- the canonical
    # serialization of the locator triple -- and it is the value the push
    # carries and the index job is submitted under, so the two cannot differ.
    pushed_refs = [envelope["source_ref"] for envelope in push_port.pushed]
    assert pushed_refs == [unknown]
    assert [
        source_ref for source_ref, _ in index_job_port.submit_calls
    ] == [
        *_source_refs_of(tmp_path, "zeus-board-minutes"),
        unknown,
        unknown,
        *_source_refs_of(tmp_path, "zeus-handbook")[1:],
    ]
    assert report.ingested == (unknown,)


def test_pushed_envelope_carries_the_sc20_metadata_surface(tmp_path: Path) -> None:
    push_port = _RecordingPushPort()
    unknown = _source_refs_of(tmp_path, "zeus-handbook")[0]
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        unknown_sources=(unknown,),
        polls=[_polls(tmp_path, "ready")],
    )

    run_demo_seed(
        _deps(index_job_port=index_job_port, push_port=push_port),
        _options(tmp_path, write=True),
    )

    envelope = push_port.pushed[0]
    assert envelope["source_ref"] == unknown
    assert deserialize_source_ref(envelope["source_ref"]) == UnitLocator(
        document_id="zeus-handbook",
        version=_document_of_fixture(tmp_path, "zeus-handbook").source_sha256,
        unit=1,
    )
    assert envelope["content"].startswith("ZEUS zeus-handbook · Page 1")
    metadata = envelope["metadata"]
    for field in (
        "fmt",
        "doc_type",
        "section",
        "source_sha256",
        "parse_rule_version",
        "params_table_version",
        "normalization_rule_version",
    ):
        assert metadata[field], field
    assert metadata["doc_type"] == "handbook"


def test_the_seed_pushes_one_envelope_per_unit(tmp_path: Path) -> None:
    """The document is the catalog's unit; the wire's unit is the page, so a
    hit on the second page cites the second page."""
    push_port = _RecordingPushPort()
    units = _units(tmp_path)
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        unknown_sources=tuple(source_ref for _, source_ref, _ in units),
        polls=[_polls(tmp_path, "ready")],
    )

    report = run_demo_seed(
        _deps(index_job_port=index_job_port, push_port=push_port),
        _options(tmp_path, write=True),
    )

    assert [envelope["source_ref"] for envelope in push_port.pushed] == [
        source_ref for _, source_ref, _ in units
    ]
    assert report.ingested == tuple(source_ref for _, source_ref, _ in units)
    for envelope, (_, source_ref, unit) in zip(
        push_port.pushed, units, strict=True
    ):
        assert envelope["source_ref"] == source_ref
        assert envelope["chunks"] == [
            {"content": envelope["content"], "index": 0}
        ]
        assert envelope["metadata"]["locator"]["unit"] == unit
        assert envelope["body_content_id"] == content_identity_of(
            envelope["content"]
        )
    assert [outcome.unit for outcome in report.jobs] == [
        unit for _, _, unit in units
    ]


def test_a_second_run_over_unchanged_fixtures_pushes_nothing(
    tmp_path: Path,
) -> None:
    """The job id is derived from the durable ``(source_ref,
    content_identity)`` pair, so the second run joins the same jobs."""
    service = _RememberingIndexJobPort()
    push_port = _RememberingPushPort(service)

    first = run_demo_seed(
        _deps(index_job_port=service, push_port=push_port),
        _options(tmp_path, write=True),
    )
    pushed_after_first = list(push_port.pushed)
    calls_after_first = len(service.submit_calls)

    second = run_demo_seed(
        _deps(index_job_port=service, push_port=push_port),
        _options(tmp_path, write=True),
    )

    assert len(pushed_after_first) == 4
    assert push_port.pushed == pushed_after_first
    assert second.ingested == ()
    assert [outcome.job_id for outcome in second.jobs] == [
        outcome.job_id for outcome in first.jobs
    ]
    assert [outcome.skipped for outcome in second.jobs] == [True] * 4
    assert [
        source_ref for source_ref, _ in service.submit_calls[calls_after_first:]
    ] == [envelope["source_ref"] for envelope in pushed_after_first]


# ---------------------------------------------------------------------------
# chain: poll
# ---------------------------------------------------------------------------


def test_failed_poll_names_job_failure_code_and_reset_command(tmp_path: Path) -> None:
    polls = _polls(tmp_path, "in_progress")
    failed_job = _job_id("zeus-board-minutes", 1)
    polls[failed_job] = _record(
        failed_job,
        _source_refs_of(tmp_path, "zeus-board-minutes")[0],
        "failed",
        failure_code="extract_failed",
    )
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path), polls=[polls])

    with pytest.raises(DemoJobFailedError) as raised:
        run_demo_seed(
            _deps(index_job_port=index_job_port),
            _options(tmp_path, write=True),
        )

    message = str(raised.value)
    assert raised.value.job_id == failed_job
    assert raised.value.failure_code == "extract_failed"
    assert "index:reset-job" in message


def test_entity_merge_embed_limit_is_named_separately(tmp_path: Path) -> None:
    polls = _polls(tmp_path, "in_progress")
    failed_job = _job_id("zeus-board-minutes", 1)
    polls[failed_job] = _record(
        failed_job,
        _source_refs_of(tmp_path, "zeus-board-minutes")[0],
        "failed",
        failure_code="ENTITY_MERGE_EMBED_LIMIT",
    )
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path), polls=[polls])

    with pytest.raises(EmbedLimitBreachedError) as raised:
        run_demo_seed(
            _deps(index_job_port=index_job_port),
            _options(tmp_path, write=True),
        )

    assert raised.value.code == "demo_seed_entity_merge_embed_limit"
    assert raised.value.failure_code == "ENTITY_MERGE_EMBED_LIMIT"


def test_stale_job_is_a_terminal_failure_not_a_skip(tmp_path: Path) -> None:
    polls = _polls(tmp_path, "in_progress")
    stale_job = _job_id("zeus-board-minutes", 1)
    polls[stale_job] = _record(
        stale_job,
        _source_refs_of(tmp_path, "zeus-board-minutes")[0],
        "stale",
    )
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path), polls=[polls])

    with pytest.raises(DemoJobFailedError) as raised:
        run_demo_seed(
            _deps(index_job_port=index_job_port),
            _options(tmp_path, write=True),
        )

    assert raised.value.job_id == stale_job


def test_poll_timeout_names_the_pending_jobs(tmp_path: Path) -> None:
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path))
    ticks = count(0.0, 1.0)
    worker = _FakeWorker()

    with pytest.raises(DemoPollTimeoutError) as raised:
        run_demo_seed(
            _deps(index_job_port=index_job_port, worker=worker, clock=lambda: next(ticks)),
            _options(tmp_path, write=True, poll_timeout_seconds=2.0),
        )

    assert raised.value.code == "demo_seed_poll_timeout"
    assert raised.value.job_ids == tuple(
        job_id for job_id, _, _ in _units(tmp_path)
    )
    assert "index:reset-job" in str(raised.value)
    # The teardown ran on the way out, and was told every job was still open --
    # that count is what an operator reads beside a worker that outlived the run.
    assert worker.terminated == [(demo_seed.WORKER_TERMINATE_TIMEOUT_SECONDS, 4)]


def test_polling_stops_once_every_job_is_terminal(tmp_path: Path) -> None:
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        polls=[
            _polls(tmp_path, "in_progress"),
            _polls(tmp_path, "ready"),
        ],
    )

    report = run_demo_seed(
        _deps(index_job_port=index_job_port),
        _options(tmp_path, write=True),
    )

    job_ids = [job_id for job_id, _, _ in _units(tmp_path)]
    assert index_job_port.poll_calls == job_ids * 2
    assert [outcome.status for outcome in report.jobs] == ["ready"] * 4


# ---------------------------------------------------------------------------
# worker supervision
# ---------------------------------------------------------------------------


class _DriftingPlanA(_RecordingPlanA):
    """Re-points the stack between the first guard and the worker start."""

    def __init__(self, environ: dict[str, str]) -> None:
        super().__init__()
        self._environ = environ

    def apply_schema(self) -> None:
        super().apply_schema()
        self._environ["IKNOW_MEMORY_HOME"] = "/home/operator/.iknow-memory"


def test_guard_runs_again_before_the_worker_starts(tmp_path: Path) -> None:
    """A stack whose selection changes after the first guard must stop the
    run before the worker can be pointed at the main store."""
    mutable = _dev_env()
    plan_a = _DriftingPlanA(mutable)
    worker = _FakeWorker()
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        polls=[_polls(tmp_path, "ready")],
    )

    with pytest.raises(DemoDefaultMemoryHomeError):
        run_demo_seed(
            _deps(
                index_job_port=index_job_port,
                plan_a=plan_a,
                worker=worker,
                environ=mutable,
            ),
            _options(tmp_path, write=True),
        )

    assert plan_a.calls == ["schema"]
    assert worker.started == []
    assert worker.terminated == []


def test_a_non_persistent_store_is_refused_before_anything_is_submitted(
    tmp_path: Path,
) -> None:
    """The refusal costs nothing: no submit, no push, no worker, no catalog."""
    environ = _dev_env()
    del environ["GRAPHRAG_MEMORY_STORAGE"]
    push_port = _RecordingPushPort()
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path))
    reset_port = _RecordingResetPort()
    plan_a = _RecordingPlanA()
    worker = _FakeWorker()

    with pytest.raises(DemoNonPersistentStorageError):
        run_demo_seed(
            _deps(
                index_job_port=index_job_port,
                push_port=push_port,
                reset_port=reset_port,
                plan_a=plan_a,
                worker=worker,
                environ=environ,
            ),
            _options(tmp_path, reset=True, write=True),
        )

    assert index_job_port.submit_calls == []
    assert push_port.pushed == []
    assert reset_port.calls == []
    assert plan_a.calls == []
    assert worker.started == []
    assert not (tmp_path / "catalog.json").exists()


def test_subprocess_worker_leaves_no_process_behind() -> None:
    """The real teardown: SIGTERM, wait, and prove the pid is gone."""
    worker = SubprocessWorker(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=Path.cwd(),
        environ=dict(os.environ),
    )
    pid = worker.start(memory_home="/srv/iknow-memory-dev")
    time.sleep(0.2)
    assert _process_alive(pid)

    returncode = worker.terminate_and_wait(timeout_seconds=10.0)

    assert returncode == -signal.SIGTERM
    assert not _process_alive(pid)


def test_worker_is_terminated_when_polling_raises(tmp_path: Path) -> None:
    """The ``finally`` path: a failed poll still leaves no worker running."""
    polls = _polls(tmp_path, "in_progress")
    failed_job = _job_id("zeus-board-minutes", 1)
    polls[failed_job] = _record(
        failed_job,
        _source_refs_of(tmp_path, "zeus-board-minutes")[0],
        "failed",
        failure_code="extract_failed",
    )
    index_job_port = _ScriptedIndexJobPort(_submissions(tmp_path), polls=[polls])
    worker = SubprocessWorker(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=Path.cwd(),
        environ=dict(os.environ),
    )

    with pytest.raises(DemoJobFailedError):
        run_demo_seed(
            _deps(index_job_port=index_job_port, worker=worker),
            _options(tmp_path, write=True),
        )

    assert worker.pid != -1, "the worker never started, so nothing proved teardown"
    assert not _process_alive(worker.pid)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not expected on a child
        return True
    return True


def _wait_until_dead(pid: int, *, within_s: float = 5.0) -> bool:
    """True once the kernel has stopped answering for a pid."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.02)
    return False


def _await_file(path: Path, *, within_s: float = 10.0) -> str:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written within {within_s}s")


def _shell_worker(tmp_path: Path, script: str) -> SubprocessWorker:
    """A real :class:`SubprocessWorker` over a shell that spawns a child.

    ``bash -c 'sleep & wait'`` is the same shape as ``npm``: it forks rather
    than exec-ing into the long-running command, which is the whole reason the
    teardown has to signal the group. ``/bin/sh`` (dash) is deliberately not
    used: it execs the last command of a trap-less script, which would make the
    grandchild tests silently prove nothing.
    """
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to model the npm -> shell -> worker chain"
    return SubprocessWorker(
        [bash, "-c", script], cwd=tmp_path, environ=dict(os.environ)
    )


def test_teardown_kills_the_grandchild_npm_would_have_orphaned(tmp_path: Path) -> None:
    # npm does not exec: it spawns a shell that spawns node. Signalling only the
    # direct child leaves the worker alive, reparented to init, still claiming
    # jobs after the seed exits -- so the teardown has to reach the group.
    marker = tmp_path / "grandchild.pid"
    worker = _shell_worker(tmp_path, f"sleep 120 & echo $! > '{marker}'; wait")
    pid = worker.start(memory_home="/srv/iknow-memory-dev")
    grandchild = int(_await_file(marker))
    # The point of the experiment: the grandchild is not the pid we started.
    assert grandchild != pid
    assert _process_alive(grandchild)

    worker.terminate_and_wait(timeout_seconds=10.0)

    assert _wait_until_dead(grandchild), f"pid {grandchild} outlived the teardown"


def test_teardown_names_the_pids_it_could_not_prove_it_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The post-signal verification has to stand on its own: it is the only thing
    # standing between "the reap was reported" and "the reap happened". A process
    # inside the group cannot outrun SIGKILL, so this drives the case the signal
    # cannot fix on its own -- the group signal never reaching the worker, which
    # is what a mis-recorded pgid looks like from here. The teardown must fail
    # the run and name the pids rather than report a clean reap.
    marker = tmp_path / "grandchild.pid"
    worker = _shell_worker(tmp_path, f"sleep 120 & echo $! > '{marker}'; wait")
    pid = worker.start(memory_home="/srv/iknow-memory-dev")
    grandchild = int(_await_file(marker))
    signalled: list[int] = []
    monkeypatch.setattr(
        demo_seed.os, "killpg", lambda pgid, sig: signalled.append(sig)
    )
    try:
        with pytest.raises(demo_seed.DemoWorkerError) as raised:
            worker.terminate_and_wait(timeout_seconds=0.2)

        message = str(raised.value)
        assert str(pid) in message
        assert str(grandchild) in message
        # Both signals were attempted before the run was called a failure.
        assert signalled == [signal.SIGTERM, signal.SIGKILL]
    finally:
        for target in (grandchild, pid):
            try:
                os.kill(target, signal.SIGKILL)
            except ProcessLookupError:  # pragma: no cover - it already exited
                pass
    assert _wait_until_dead(grandchild)


def test_teardown_signals_the_group_even_when_the_child_already_exited(
    tmp_path: Path,
) -> None:
    # The real shape of the bug: npm exits, node keeps running. The direct child
    # is already reaped when the teardown runs, so a teardown that skips the
    # group on ``poll() is not None`` never signals the worker at all -- which is
    # the orphan that was observed with ``ppid`` of ``/init``.
    marker = tmp_path / "orphan.pid"
    worker = _shell_worker(
        tmp_path, f"sleep 120 & echo $! > '{marker}'; exit 0"
    )
    worker.start(memory_home="/srv/iknow-memory-dev")
    orphan = int(_await_file(marker))
    _wait_until_dead(worker.pid)

    returncode = worker.terminate_and_wait(timeout_seconds=10.0)

    assert returncode == 0, "the worker's own status is npm's, not the orphan's"
    assert _wait_until_dead(orphan), f"pid {orphan} outlived the teardown"


def test_teardown_kills_a_worker_that_ignores_sigterm(tmp_path: Path) -> None:
    # SIGTERM is a request. A worker that traps it must still be ended, by
    # SIGKILL, and the escalation must happen within the teardown budget rather
    # than leaving the group running after the seed has moved on.
    worker = _shell_worker(tmp_path, "trap '' TERM; sleep 120 & echo $! > pgid; wait")
    worker.start(memory_home="/srv/iknow-memory-dev")
    stubborn = int(_await_file(tmp_path / "pgid"))

    returncode = worker.terminate_and_wait(timeout_seconds=1.0)

    assert returncode == -signal.SIGKILL
    assert _wait_until_dead(stubborn), f"pid {stubborn} outlived the teardown"


def test_teardown_is_idempotent_and_keeps_one_answer(tmp_path: Path) -> None:
    # The ``finally`` can be reached once, but a caller that also stops the
    # worker must get the same answer rather than a different status, and an
    # already-exited worker must not be signalled twice.
    worker = _shell_worker(tmp_path, "exit 3")
    worker.start(memory_home="/srv/iknow-memory-dev")
    _wait_until_dead(worker.pid)

    assert worker.terminate_and_wait(timeout_seconds=5.0) == 3
    assert worker.terminate_and_wait(timeout_seconds=5.0) == 3

    stubborn = _shell_worker(tmp_path, "trap '' TERM; sleep 120 & echo $! > pgid; wait")
    stubborn.start(memory_home="/srv/iknow-memory-dev")
    int((tmp_path / "pgid").unlink(missing_ok=True) or 0)
    first = stubborn.terminate_and_wait(timeout_seconds=1.0)
    assert stubborn.terminate_and_wait(timeout_seconds=1.0) == first


def test_teardown_refuses_before_the_worker_was_started(tmp_path: Path) -> None:
    # Nothing to reap, and nothing to prove: a never-started worker is a chain
    # bug, not a clean teardown, so it stays a failure.
    worker = SubprocessWorker([sys.executable, "-c", "pass"], cwd=tmp_path, environ={})

    with pytest.raises(demo_seed.DemoWorkerError):
        worker.terminate_and_wait(timeout_seconds=1.0)


def test_teardown_logs_the_group_the_signal_and_the_open_jobs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    marker = tmp_path / "grandchild.pid"
    worker = _shell_worker(tmp_path, f"sleep 120 & echo $! > '{marker}'; wait")
    pid = worker.start(memory_home="/srv/iknow-memory-dev")
    grandchild = int(_await_file(marker))

    with caplog.at_level(logging.INFO, logger="fin.corpus.demo_seed"):
        worker.terminate_and_wait(timeout_seconds=10.0, open_jobs=2)

    line = " ".join(
        record.getMessage()
        for record in caplog.records
        if "worker teardown" in record.getMessage()
    )
    assert f"worker_pgid={pid}" in line
    assert "signal=SIGTERM" in line
    assert "open_jobs=2" in line
    assert "survivors=[]" in line
    assert _wait_until_dead(grandchild)


def test_worker_command_is_the_agreed_npm_script() -> None:
    """``index:worker --batch-size 1`` is the command the worker actually
    runs, built from the paired repo's real script name."""
    from fin.corpus.demo_seed import worker_command

    assert worker_command() == [
        "npm",
        "run",
        "index:worker",
        "--",
        "--batch-size",
        "1",
    ]


def test_worker_start_passes_the_memory_home_explicitly() -> None:
    worker = SubprocessWorker(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=Path.cwd(),
        environ={"PATH": "/usr/bin"},
    )

    pid = worker.start(memory_home="/srv/iknow-memory-dev")

    try:
        assert worker.environ["IKNOW_MEMORY_HOME"] == "/srv/iknow-memory-dev"
        assert worker.environ["PATH"] == "/usr/bin"
        assert worker.pid == pid
    finally:
        worker.terminate_and_wait(timeout_seconds=10.0)


def test_worker_start_refuses_when_the_command_is_missing(tmp_path: Path) -> None:
    worker = SubprocessWorker(
        [str(tmp_path / "no-such-binary")],
        cwd=tmp_path,
        environ=dict(os.environ),
    )

    with pytest.raises(demo_seed.DemoWorkerError) as raised:
        worker.start(memory_home="/srv/iknow-memory-dev")

    assert raised.value.code == "demo_seed_worker_unavailable"


def test_worker_start_refuses_a_relative_command() -> None:
    worker = SubprocessWorker(
        ["node", "dist/cli/index-job-worker.js"],
        cwd=Path.cwd(),
        environ=dict(os.environ),
    )

    with pytest.raises(demo_seed.DemoWorkerError):
        worker.start(memory_home="/srv/iknow-memory-dev")


def test_command_runs_in_the_iknow_root_and_inherits_the_memory_home(
    tmp_path: Path,
) -> None:
    """``npm run`` resolves the script through the paired repo's
    ``package.json``, so the worker process must start inside that root."""
    assert subprocess.run is not None  # the adapter shells out, not fin's Python
    assert demo_seed.PLAN_A_NPM_SCRIPTS["schema"] == "migrate:dev"
    assert demo_seed.PLAN_A_NPM_SCRIPTS["reset_job"] == "index:reset-job"


# ---------------------------------------------------------------------------
# concrete store seams (injected transport, no live database)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self._connection.statements.append(statement)

    def fetchone(self) -> tuple[object, ...] | None:
        return self._connection.row


class _FakeConnection:
    def __init__(self, row: tuple[object, ...] | None = None) -> None:
        self.statements: list[str] = []
        self.row = row
        self.committed = 0

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed += 1


def test_postgres_truncate_names_only_the_allowlisted_tables() -> None:
    connection = _FakeConnection()
    port = PostgresStoreReset(lambda: connection)

    truncated = port.truncate_tables(("chunks", "entities", "index_jobs"))

    assert truncated == ("chunks", "entities", "index_jobs")
    assert connection.statements == [
        'TRUNCATE TABLE "chunks", "entities", "index_jobs"'
    ]
    assert connection.committed == 1


def test_postgres_truncate_refuses_a_name_that_is_not_an_identifier() -> None:
    port = PostgresStoreReset(_FakeConnection)

    with pytest.raises(demo_seed.DemoStoreUnavailableError) as raised:
        port.truncate_tables(("chunks; DROP TABLE users",))

    assert "identifier" in str(raised.value)


def test_postgres_truncate_reports_a_failed_statement() -> None:
    class _Failing:
        def __enter__(self) -> _Failing:
            raise RuntimeError("connection refused")

        def __exit__(self, *_exc: object) -> None:
            return None

    port = PostgresStoreReset(lambda: _Failing())

    with pytest.raises(demo_seed.DemoStoreUnavailableError) as raised:
        port.truncate_tables(("chunks",))

    assert "connection refused" in str(raised.value)


def test_embed_probe_reads_the_singleton() -> None:
    connection = _FakeConnection(row=(1536, "e5-small-v1"))

    observed = PostgresEmbedProbe(lambda: connection).read_embedding_generation()

    assert observed == EmbedGeneration(dimensions=1536, generation="e5-small-v1")
    assert "embedding_generation" in connection.statements[0]
    assert "WHERE id = 1" in connection.statements[0]


def test_embed_probe_refuses_a_missing_singleton() -> None:
    probe = PostgresEmbedProbe(_FakeConnection(row=None))

    with pytest.raises(demo_seed.DemoStoreUnavailableError) as raised:
        probe.read_embedding_generation()

    assert "embedding_generation" in str(raised.value)


def test_embed_probe_refuses_an_unreadable_row() -> None:
    probe = PostgresEmbedProbe(_FakeConnection(row=(None, None)))

    with pytest.raises(demo_seed.DemoStoreUnavailableError):
        probe.read_embedding_generation()


def test_es_reset_deletes_every_listed_index() -> None:
    seen: list[tuple[str, str]] = []

    def opener(method: str, url: str) -> tuple[int, bytes]:
        seen.append((method, url))
        if url.endswith("/_cat/indices?format=json"):
            return 200, json.dumps(
                [{"index": "chunks"}, {"index": "lexical_chunks"}]
            ).encode()
        return 200, b"{}"

    port = ElasticsearchIndexReset("http://127.0.0.1:9201", opener=opener)

    deleted = port.delete_elasticsearch_indices()

    assert deleted == ("chunks", "lexical_chunks")
    assert seen == [
        ("GET", "http://127.0.0.1:9201/_cat/indices?format=json"),
        ("DELETE", "http://127.0.0.1:9201/chunks"),
        ("DELETE", "http://127.0.0.1:9201/lexical_chunks"),
    ]


def test_es_reset_refuses_an_unreachable_store() -> None:
    def opener(method: str, url: str) -> tuple[int, bytes]:
        raise OSError("connection refused")

    port = ElasticsearchIndexReset("http://127.0.0.1:9201", opener=opener)

    with pytest.raises(demo_seed.DemoStoreUnavailableError) as raised:
        port.index_names()

    assert "connection refused" in str(raised.value)


def test_es_reset_refuses_an_unparseable_listing() -> None:
    port = ElasticsearchIndexReset(
        "http://127.0.0.1:9201", opener=lambda _m, _u: (200, b"not json")
    )

    with pytest.raises(demo_seed.DemoStoreUnavailableError) as raised:
        port.index_names()

    assert "not JSON" in str(raised.value)


def test_es_reset_refuses_a_failed_index_delete() -> None:
    def opener(method: str, url: str) -> tuple[int, bytes]:
        if method == "GET":
            return 200, json.dumps([{"index": "chunks"}]).encode()
        return 500, b"boom"

    port = ElasticsearchIndexReset("http://127.0.0.1:9201", opener=opener)

    with pytest.raises(demo_seed.DemoStoreUnavailableError) as raised:
        port.delete_elasticsearch_indices()

    assert "500" in str(raised.value)


def test_neo4j_reset_is_refused_by_name() -> None:
    port = Neo4jGraphReset(uri="bolt://127.0.0.1:7688")

    with pytest.raises(DemoNeo4jUnavailableError) as raised:
        port.clear_neo4j_graph()

    assert raised.value.code == "demo_seed_neo4j_client_unavailable"
    assert "DETACH DELETE" in str(raised.value)


# ---------------------------------------------------------------------------
# ordinary-store isolation
# ---------------------------------------------------------------------------
#
# The spec's ordinary-store clause (`specs/demo-seed.md`: "the count and
# states of the main store's `index_jobs` are unchanged before and after the
# seed") is an isolation claim, so it is measured here rather than asserted
# about: two stores stand in for the two stacks, and the assertion compares
# sanitized counts and states only.

#: The ordinary store's Postgres port -- the one the guard refuses.
_ORDINARY_PG_PORT = 5432

#: The ordinary store's URL: the dev target with the dev port swapped out, so
#: the two differ only in where they point and carry no separate secret.
_ORDINARY_DSN = _dev_env()[demo_seed.DB_URL_ENV].replace(
    f":{DEV_STACK_PORTS['postgres']}", f":{_ORDINARY_PG_PORT}"
)


class _IndexJobsStore:
    """One store's ``index_jobs``, as a stand-in for the real table.

    :meth:`snapshot` reports the count and the state distribution -- the two
    quantities the isolation clause compares. No statement text, DSN, or
    credential is recorded, so nothing here can carry one into an assertion
    message.
    """

    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = dict(rows)
        self.connections = 0
        self.truncated_tables: tuple[str, ...] = ()

    def snapshot(self) -> tuple[int, dict[str, int]]:
        states: dict[str, int] = {}
        for status in self.rows.values():
            states[status] = states.get(status, 0) + 1
        return len(self.rows), states

    def connect(self) -> _IndexJobsConnection:
        self.connections += 1
        return _IndexJobsConnection(self)

    def _execute(self, statement: str) -> None:
        if not statement.startswith("TRUNCATE TABLE"):
            return
        named = tuple(
            part.strip().strip('"')
            for part in statement.split("TABLE", 1)[1].split(",")
        )
        self.truncated_tables = named
        if "index_jobs" in named:
            self.rows.clear()


class _IndexJobsCursor:
    def __init__(self, store: _IndexJobsStore) -> None:
        self._store = store

    def __enter__(self) -> _IndexJobsCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self._store._execute(statement)

    def fetchone(self) -> tuple[object, ...]:
        # The singleton the embed probe reads; the row is a value, not text.
        return (1536, "e5-small-v1")


class _IndexJobsConnection:
    def __init__(self, store: _IndexJobsStore) -> None:
        self._store = store

    def __enter__(self) -> _IndexJobsConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _IndexJobsCursor:
        return _IndexJobsCursor(self._store)

    def commit(self) -> None:
        return None


class _StoreRouter:
    """Hands each connection to the store its DSN names.

    A seed pointed at the wrong store is therefore visible in that store's
    ``connections`` count instead of being absorbed by a single fake.
    """

    def __init__(self, stores: dict[str, _IndexJobsStore]) -> None:
        self._stores = stores

    def connect(self, dsn: str, **_kwargs: Any) -> _IndexJobsConnection:
        return self._stores[dsn].connect()


class _RecordingIndexReset:
    """Stand-in for the composition root's Elasticsearch seam.

    The production class opens a socket on construction-free use, and the
    ordinary-store clause is about the ``index_jobs`` table, not about index
    names, so this records the call and reads nothing.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def delete_elasticsearch_indices(self) -> tuple[str, ...]:
        return ()


class _GraphStepDouble:
    """The production reset port with only its graph leg replaced.

    ``Neo4jGraphReset`` refuses by name because fin ships no bolt client, so
    the production port cannot finish a reset. Swapping that one leg leaves
    the store legs -- the ones that hold ``index_jobs`` -- exactly as the
    composition root built them.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def truncate_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        return self._inner.truncate_tables(tables)

    def delete_elasticsearch_indices(self) -> tuple[str, ...]:
        return self._inner.delete_elasticsearch_indices()

    def clear_neo4j_graph(self) -> tuple[str, ...]:
        return ()


def _seed_demo_cli():
    """The ``seed:demo`` composition root, loaded from ``scripts/``.

    Its reset port is where the seed decides which store it addresses, so the
    isolation claim is measured against that code rather than against a
    stand-in rebuilt here. The script guards its own ``__main__``, so loading
    it runs nothing.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("seed_demo_cli_orchestration", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_demo_cli = _seed_demo_cli()


def _ordinary_store_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_IndexJobsStore, _IndexJobsStore, _StoreRouter]:
    """The ordinary and dev stores, wired through the composition root.

    The ordinary store carries a mixed set of job states, so an unchanged
    snapshot means both the row count and every row's state survived.
    """
    ordinary = _IndexJobsStore(
        {"job-a": "ready", "job-b": "ready", "job-c": "failed"}
    )
    dev = _IndexJobsStore({"stale-job": "failed"})
    router = _StoreRouter(
        {
            _ORDINARY_DSN: ordinary,
            _dev_env()[demo_seed.DB_URL_ENV]: dev,
        }
    )
    monkeypatch.setattr(seed_demo_cli, "psycopg", router)
    monkeypatch.setattr(
        seed_demo_cli, "ElasticsearchIndexReset", _RecordingIndexReset
    )
    return ordinary, dev, router


def test_reset_and_submit_leave_the_ordinary_store_index_jobs_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec's ordinary-store clause, measured across the full write path.

    The dev store is emptied by the same run, which is what makes the
    ordinary store's unchanged snapshot evidence rather than a tautology: the
    assertion distinguishes the two stores rather than observing a no-op.
    """
    ordinary, dev, router = _ordinary_store_pair(monkeypatch)
    before = ordinary.snapshot()
    index_job_port = _ScriptedIndexJobPort(
        _submissions(tmp_path),
        polls=[_polls(tmp_path, "ready")],
    )

    report = run_demo_seed(
        _deps(
            index_job_port=index_job_port,
            reset_port=_GraphStepDouble(seed_demo_cli._store_reset_port(_dev_env())),
            probe=PostgresEmbedProbe(
                lambda: router.connect(_dev_env()[demo_seed.DB_URL_ENV])
            ),
        ),
        _options(tmp_path, reset=True, write=True),
    )

    assert report.wrote is True
    assert ordinary.snapshot() == before == (3, {"ready": 2, "failed": 1})
    assert dev.snapshot() == (0, {})
    assert "index_jobs" in dev.truncated_tables


def test_the_graph_refusal_leaves_the_ordinary_store_index_jobs_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abort path, not just the completing one.

    The production reset port refuses at its graph step, after the table
    truncation has already run. That partial state must still leave the
    ordinary store untouched.
    """
    ordinary, dev, _router = _ordinary_store_pair(monkeypatch)
    before = ordinary.snapshot()

    with pytest.raises(DemoNeo4jUnavailableError):
        run_demo_seed(
            _deps(
                index_job_port=_ScriptedIndexJobPort(_submissions(tmp_path)),
                reset_port=seed_demo_cli._store_reset_port(_dev_env()),
            ),
            _options(tmp_path, reset=True, write=True),
        )

    assert ordinary.snapshot() == before
    assert dev.snapshot() == (0, {})


def test_an_ordinary_store_target_is_refused_before_any_store_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard, not luck, is what keeps the two stores apart.

    With the ordinary store's URL exported, the run stops before opening a
    connection to either store, so the ordinary store is never a candidate.
    """
    ordinary, dev, _router = _ordinary_store_pair(monkeypatch)
    before = ordinary.snapshot()
    environ = _dev_env(**{demo_seed.DB_URL_ENV: _ORDINARY_DSN})

    with pytest.raises(demo_seed.DemoPortMismatchError):
        run_demo_seed(
            _deps(
                index_job_port=_ScriptedIndexJobPort(_submissions(tmp_path)),
                reset_port=seed_demo_cli._store_reset_port(environ),
                environ=environ,
            ),
            _options(tmp_path, reset=True, write=True),
        )

    assert ordinary.snapshot() == before
    assert ordinary.connections == 0
    assert dev.connections == 0
