from pathlib import Path

from fin.corpus.chunking import split_document
from fin.corpus.dataset.t1 import load_t1_manifest
from fin.corpus.parsers import parse_document
from fin.corpus.push import document_push_records
from fin.retrieve.push_port import MAX_PUSH_BATCH_RECORDS, PushPort


def push_t1_corpus(*, manifest_path: Path, text_dir: Path, port: PushPort) -> dict:
    """Chunk and push frozen T1 text (opt-in; never selects a live port).

    The caller supplies the port, making the default test path entirely
    in-process. Each sample is one atomic push batch; failures are retained
    by sample id and re-raised only after all independent samples are tried.
    """
    manifest = load_t1_manifest(manifest_path)
    report = {"samples": 0, "blocks": 0, "written": 0, "duplicates": 0, "failures": []}
    for sample in manifest.get("samples", []):
        sample_id = str(sample.get("sample_id", "<unknown>"))
        try:
            source = Path(text_dir) / str(sample["path"])
            text = source.read_bytes()
            parsed = parse_document(text, fmt="txt", document_id=str(sample["document_id"]), version=str(sample["sha256"]))
            chunked = split_document(parsed, doc_type="mixed")
            records = list(document_push_records(chunked))
            if not records:
                raise ValueError("t1_empty_chunks")
            if len(records) > MAX_PUSH_BATCH_RECORDS:
                raise ValueError(f"push_batch_overflow: {len(records)} > {MAX_PUSH_BATCH_RECORDS}")
            enriched = []
            for record in records:
                metadata = dict(record.metadata)
                metadata.update({"scene": sample["scene"], "license": sample["source"]["license"], "url": sample["source"]["url"]})
                from dataclasses import replace
                from types import MappingProxyType
                enriched.append(replace(record, metadata=MappingProxyType(metadata)))
            outcome = port.push([record.to_wire() for record in enriched])
            report["samples"] += 1
            report["blocks"] += len(enriched)
            report["written"] += int(outcome.get("written", 0))
            report["duplicates"] += int(outcome.get("duplicates", 0))
        except Exception as exc:
            report["failures"].append({"sample_id": sample_id, "code": getattr(exc, "code", "t1_push_failed"), "error": str(exc)})
    return report


def main(argv=None) -> int:
    import argparse, json, os
    from fin.retrieve.fake_push_port import FakePushPort
    from fin.retrieve.mcp_push_port import MCPPushPort
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(root / "data/corpus/dataset/t1_manifest.json"))
    parser.add_argument("--text-dir", default=str(root / "data/corpus/dataset/t1"))
    parser.add_argument("--adapter", choices=("fake", "mcp"), default=None, help="opt-in adapter; required to run")
    args = parser.parse_args(argv)
    adapter = args.adapter or os.environ.get("FIN_RETRIEVE_ADAPTER")
    if adapter not in ("fake", "mcp"):
        parser.error("opt-in required: pass --adapter fake|mcp or FIN_RETRIEVE_ADAPTER")
    port = FakePushPort() if adapter == "fake" else MCPPushPort()
    report = push_t1_corpus(manifest_path=Path(args.manifest), text_dir=Path(args.text_dir), port=port)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
