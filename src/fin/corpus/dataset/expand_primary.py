"""Opt-in expansion of the tier-1 PDF corpus (plan T10 实现余地).

B1 requires >= 20 samples per format for all 7 formats; the frozen
private fixture source freeze provides 12 tier-1 10-Q PDFs. The compliant path to
the floor is freezing more official filings — **never** synthesizing
PDFs or counting unsourced files.

This module wraps the existing UA-gated :meth:`fin.corpus.live_fetch.
fetch_and_ingest` so an operator can freeze additional EDGAR 10-Qs in
one command::

    FIN_EDGAR_USER_AGENT="name <email>" uv run python -m fin.corpus.dataset \
        expand-primary --cik 0000320193 --accession 0000320193-24-000010

Multiple pairs: repeat ``--cik`` / ``--accession`` (they zip pairwise).
Every fetch is a real SEC HTTP call under the operator's UA — that is
why this is a separate, explicitly-invoked command: the dataset build,
verify, the default pytest suite, and the eval suite **never** perform
network access (B1 禁止：并发压测访问外部数据源；SC1：默认套件不访问外网).

After new filings are frozen the operator re-runs ``build`` so the
manifest counts them as tier-1 samples.
"""

from __future__ import annotations

from pathlib import Path

from fin.corpus.errors import IngestMissingError
from fin.corpus.live_fetch import fetch_and_ingest


def run_expand_primary(pairs: list[tuple[str, str]]) -> int:
    """Fetch + ingest each (cik, accession) pair; returns a process exit code.

    One failing pair does not abort the remaining pairs — each result is
    printed with a named outcome, and the command exits non-zero only if
    at least one fetch failed (no silent swallowing).
    """
    from fin.corpus.dataset.build import default_paths

    manifest_path, extracted_root, facts_root = _resolve_paths()
    failures = 0
    for cik, accession in pairs:
        try:
            body = fetch_and_ingest(
                cik=cik,
                accession=accession,
                manifest_path=manifest_path,
                extracted_root=extracted_root,
                facts_root=facts_root,
            )
        except IngestMissingError as exc:
            failures += 1
            print(f"FAIL {accession}: {type(exc).__name__}: {exc}")
            continue
        print(f"OK {accession}: frozen {len(body)} bytes (live locator contract)")
    if failures:
        print(f"expand-primary: {failures} of {len(pairs)} fetch(es) failed")
        return 1
    print(
        "expand-primary: done — now re-run `uv run python -m fin.corpus.dataset build` "
        "so the manifest counts the new tier-1 samples"
    )
    return 0


def _resolve_paths() -> tuple[Path, Path, Path]:
    from fin.corpus.live_fetch import _default_paths

    return _default_paths()
