"""Hermetic runtime probes for the PR #79 High-finding gate re-verify.

Not a pytest module: it is an operator-runnable script that exercises
the five High findings end to end on the merged verify branch and
prints one PASS/FAIL line each. Network egress is blocked for the
whole run, so any probe that reaches out to SEC fails loudly.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class NetworkBlocked(RuntimeError):
    pass


def block_network() -> None:
    """Deny outbound TCP without breaking ``socket`` for local imports."""

    def _audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            raise NetworkBlocked(
                "outbound socket blocked by gate_reverify_evidence"
            )

    sys.addaudithook(_audit)


# ----- A: host retrieve surfaces retrieve_unavailable -----


def probe_a() -> None:
    from fin.host.graph import _make_retrieve_tool

    class UnavailablePort:
        def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
            return {"error": "retrieve_unavailable"}

    command = _make_retrieve_tool(UnavailablePort()).invoke(
        {
            "name": "retrieve",
            "args": {"query": "total net sales"},
            "id": "c-1",
            "type": "tool_call",
        }
    )
    message = command.update["messages"][0]
    payload = json.loads(message.content)
    evidence = command.update.get("evidence", [])
    ok = payload == {"error": "retrieve_unavailable"} and not evidence
    record(
        "A retrieve_unavailable typed EXIT",
        ok,
        f"ToolMessage content={payload!r}, evidence={len(evidence)}",
    )


# ----- B: adapter kind comes from config, locator_map fails closed -----


def probe_b() -> None:
    from fin.host.compile import DEFAULT_RETRIEVE_ADAPTER, _resolve_retrieve_adapter
    from fin.host.graph import (
        RetrieveAdapterConfigError,
        _locator_map_from_tree,
        compile_graph,
    )
    from fin.corpus.extracted import load_extracted_tree
    from fin.retrieve import FakeRetrievePort, MCPRetrieveAdapter
    from langgraph.checkpoint.memory import InMemorySaver
    from fin.host import ScriptedChatModel

    default_kind = _resolve_retrieve_adapter({})
    env_kind = _resolve_retrieve_adapter({"FIN_RETRIEVE_ADAPTER": "fake"})

    # An InMemorySaver checkpointer must NOT be able to force the fake
    # port when configuration says mcp: the old heuristic did exactly
    # that, which is the finding under re-verify.
    tree = load_extracted_tree(
        REPO / "data" / "corpus" / "manifest.json",
        REPO / "data" / "corpus" / "extracted",
    )
    locator_map = _locator_map_from_tree(tree)
    built: dict[str, Any] = {}

    def _capture(self, **kwargs: Any) -> None:
        built.update(kwargs)
        raise _StopBuild

    class _StopBuild(Exception):
        pass

    import fin.host.graph as graph_module

    original_init = MCPRetrieveAdapter.__init__
    graph_module.MCPRetrieveAdapter.__init__ = _capture  # type: ignore[method-assign]
    mcp_selected_under_memory_saver = False
    try:
        compile_graph(
            checkpointer=InMemorySaver(),
            model=ScriptedChatModel(script=[]),
            retrieve_adapter="mcp",
            locator_map=locator_map,
            known_document_ids=set(tree.keys()),
        )
    except _StopBuild:
        mcp_selected_under_memory_saver = bool(built.get("locator_map"))
    finally:
        graph_module.MCPRetrieveAdapter.__init__ = original_init  # type: ignore[method-assign]

    # Fail-closed: mcp with an empty map must raise, not silently
    # answer every query with zero chunks.
    fail_closed = False
    try:
        compile_graph(
            checkpointer=InMemorySaver(),
            model=ScriptedChatModel(script=[]),
            retrieve_adapter="mcp",
            locator_map={},
            known_document_ids={"AAPL-2023Q3"},
        )
    except RetrieveAdapterConfigError:
        fail_closed = True

    unknown_kind_rejected = False
    try:
        compile_graph(
            checkpointer=InMemorySaver(),
            model=ScriptedChatModel(script=[]),
            retrieve_adapter="bm25-archived",
        )
    except RetrieveAdapterConfigError:
        unknown_kind_rejected = True

    fake_graph_adapter = compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(script=[]),
        retrieve_adapter="fake",
        known_document_ids={"AAPL-2023Q3"},
    )
    _ = fake_graph_adapter

    ok = (
        default_kind == "mcp" == DEFAULT_RETRIEVE_ADAPTER
        and env_kind == "fake"
        and bool(locator_map)
        and mcp_selected_under_memory_saver
        and fail_closed
        and unknown_kind_rejected
    )
    record(
        "B adapter from config + locator_map",
        ok,
        f"product default={default_kind}, env_override={env_kind}, "
        f"mcp_chosen_despite_InMemorySaver={mcp_selected_under_memory_saver} "
        f"(locator_map={len(built.get('locator_map', {}))} entries from "
        f"{len(locator_map)} tree units), "
        f"empty_map_fails_closed={fail_closed}, "
        f"unknown_kind_rejected={unknown_kind_rejected}",
    )


# ----- C: seed fact evidenced by page-004 frozen text -----


def probe_c() -> None:
    fact = json.loads(
        (REPO / "data" / "corpus" / "facts" / "AAPL-2023Q3.json").read_text()
    )["facts"][0]
    locator = fact["source_locator"]
    page = json.loads(
        (
            REPO
            / "data"
            / "corpus"
            / "extracted"
            / locator["document_id"]
            / f"page-{locator['page']:03d}.json"
        ).read_text()
    )
    numeral = f"{fact['value']:,}"
    ok = (
        page["version"] == locator["version"]
        and numeral in page["text"]
        and "(In millions" in page["text"]
        and fact["scale"] == "millions"
    )
    record(
        "C seed fact evidenced by page-004",
        ok,
        f"value={numeral} present={numeral in page['text']}, "
        f"scale={fact['scale']}, version_match="
        f"{page['version'] == locator['version']}",
    )


# ----- D: local ingest updates catalog fields with no SEC GET -----


def probe_d() -> None:
    from fin.corpus.ingest import run_ingest

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "corpus"
        shutil.copytree(REPO / "data" / "corpus", root)
        manifest_path = root / "manifest.json"

        # Wipe the catalog fields so the run has to re-emit them.
        payload = json.loads(manifest_path.read_text())
        for doc in payload["documents"]:
            for field in ("cik", "accession", "filing_date", "form", "ticker"):
                doc[field] = None
        manifest_path.write_text(json.dumps(payload, indent=2))

        seed_path = Path(tmp) / "seed.json"
        seed_path.write_text(
            json.dumps(
                {
                    "documents": {
                        "AAPL-2023Q3": {
                            "accession": "0000320193-23-000077",
                            "filing_date": "2023-08-04",
                        }
                    }
                }
            )
        )

        result = run_ingest(
            manifest_path=manifest_path,
            pdf_dir=root / "pdf",
            extracted_root=root / "extracted",
            facts_root=root / "facts",
            catalog_seed_path=seed_path,
        )
        after = json.loads(manifest_path.read_text())["documents"]
        row = next(d for d in after if d["document_id"] == "AAPL-2023Q3")
        ok = (
            result.catalog_updated > 0
            and row["cik"]
            and row["accession"] == "0000320193-23-000077"
            and row["filing_date"] == "2023-08-04"
            and row["form"]
            and row["ticker"]
        )
        record(
            "D local ingest emits catalog, no SEC GET",
            bool(ok),
            f"catalog_updated={result.catalog_updated}, AAPL row="
            f"{ {k: row[k] for k in ('cik', 'accession', 'filing_date', 'form', 'ticker')} }",
        )


# ----- E: live_fetch GETs the primary .htm and merges _index.json -----


def probe_e() -> None:
    import io
    from unittest import mock

    import fin.corpus.live_fetch as lf

    listing = json.dumps(
        {
            "directory": {
                "item": [
                    {"name": "0000320193-23-000077-index.htm", "size": "10"},
                    {"name": "R2.htm", "size": "9999"},
                    {"name": "aapl-20230701.htm", "size": "500000"},
                    {"name": "Financial_Report.xlsx", "size": "1"},
                ]
            }
        }
    ).encode()
    primary = b"<html><body><p>Total net sales 293,787</p></body></html>"
    fetched: list[str] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        url = request.full_url if hasattr(request, "full_url") else str(request)
        fetched.append(url)
        body = listing if url.endswith("index.json") else primary
        return io.BytesIO(body)

    with tempfile.TemporaryDirectory() as tmp:
        extracted = Path(tmp) / "extracted"
        facts = Path(tmp) / "facts"
        extracted.mkdir(parents=True)
        # A pre-existing index the live fetch must merge into, not clobber.
        (extracted / "_index.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "documents": {
                        "AAPL-2023Q3": {"version": "pre-existing", "page_count": 9}
                    },
                }
            )
        )
        with mock.patch.dict(os.environ, {"FIN_EDGAR_USER_AGENT": "gate probe"}):
            with mock.patch.object(lf, "urlopen", fake_urlopen):
                lf.fetch_and_ingest(
                    cik="0000320193",
                    accession="0000320193-23-000077",
                    manifest_path=REPO / "data" / "corpus" / "manifest.json",
                    extracted_root=extracted,
                    facts_root=facts,
                )
        index = json.loads((extracted / "_index.json").read_text())

    content_url = fetched[-1]
    ok = (
        content_url.endswith("aapl-20230701.htm")
        and "R2.htm" not in content_url
        and "AAPL-2023Q3" in index["documents"]
        and index["documents"]["AAPL-2023Q3"]["version"] == "pre-existing"
        and "0000320193-23-000077" in index["documents"]
    )
    record(
        "E live_fetch primary .htm + index merge",
        ok,
        f"content GET={content_url}, index documents="
        f"{sorted(index['documents'])}",
    )


def main() -> int:
    block_network()
    for probe in (probe_a, probe_b, probe_c, probe_d, probe_e):
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - script surfaces any probe fault
            record(probe.__name__, False, f"raised {type(exc).__name__}: {exc}")
    failures = [name for name, ok, _ in RESULTS if not ok]
    print()
    print(f"probes: {len(RESULTS)}, failures: {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
