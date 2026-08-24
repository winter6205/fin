"""UA-gated live EDGAR fetch (T1).

This module is the **operator-only** ingest path that pulls the
official primary iXBRL ``.htm`` from ``www.sec.gov`` and writes it
into the extracted tree under the live locator contract (ADR 0028).
The local PDF path (``fin.corpus.ingest``) stays separate, per the
"ingest CLI vs ask path stay split" rule (ACR complexity-anti-drift).

SEC HTTP contract
-----------------

The only environment signal is ``FIN_EDGAR_USER_AGENT``. Per spec
(T1 acceptance / § Boundaries):

* **Unset or empty** → ``SecUaUnsetError`` is raised, NO HTTP call is
  made. The ask path / default pytest MUST NOT touch SEC hosts.
* **Set + HTTP success** → the response bytes are hashed as the
  ``version`` and a stub extracted tree + manifest entry are
  written. (Full HTML partitioning into ``unit`` is deferred — ADR
  0028 leaves that decision to a later ADR; T1 ships the typed-EXIT
  scaffolding, not the partitioner.)
* **Set + HTTP failure (non-2xx)** → ``SecHttpFailError`` is raised,
  the manifest is NOT updated, the extracted tree for that accession
  is NOT created. This is the ``sec_http_fail`` typed EXIT (spec §
  Boundaries).

Imports kept minimal
--------------------

We use stdlib ``urllib.request`` only — no new dep. ``pdfplumber``
does not appear on this path; if it did, the ask-path contract (ADR
0020) would be violated the moment ``fin.corpus.live_fetch`` was
imported.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from fin.corpus.errors import IngestMissingError


# Import at module level so tests can mock
# ``fin.corpus.live_fetch.urlopen`` to assert no SEC request is made
# when ``FIN_EDGAR_USER_AGENT`` is unset. Without this re-binding the
# ``urllib.request.urlopen`` call inside ``_sec_get`` would be a
# free-function reference, not a module attribute, and the test mock
# would fail with ``AttributeError``.
urlopen = urllib.request.urlopen


_UA_ENV = "FIN_EDGAR_USER_AGENT"

_EDGAR_ARCHIVES_HOST = "www.sec.gov"


class SecUaUnsetError(IngestMissingError):
    """Typed EXIT: ``FIN_EDGAR_USER_AGENT`` is unset or empty.

    Per spec T1 acceptance: unset UA → no SEC request. We raise the
    ``IngestMissingError`` family so the rest of the corpus slice can
    handle it uniformly. The caller (the live-fetch CLI) exits
    non-zero with this typed signal.
    """


class SecHttpFailError(IngestMissingError):
    """Typed EXIT: UA is set but the SEC HTTP call failed.

    Carries ``status_code`` (the failing ``HTTPError.code``) and the
    ``accession`` so the operator log has enough context to decide
    whether to retry. Per spec: ``sec_http_fail`` MUST NOT mark the
    accession complete — i.e. this is raised before any catalog /
    extracted-tree write happens.
    """

    def __init__(self, message: str, *, status_code: int, accession: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.accession = accession


def _read_ua() -> str:
    """Read ``FIN_EDGAR_USER_AGENT`` from the environment.

    Empty / whitespace-only strings are treated as unset (spec:
    "unset ``FIN_EDGAR_USER_AGENT`` never opens SEC hosts"). Raises
    ``SecUaUnsetError`` so the caller can exit non-zero without
    having made any HTTP request.
    """
    ua = os.environ.get(_UA_ENV, "").strip()
    if not ua:
        raise SecUaUnsetError(
            f"{_UA_ENV} is unset or empty; refusing to open SEC hosts "
            "(spec: ingest CLI must not fetch without an explicit UA)"
        )
    return ua


def _sec_get(url: str, ua: str, *, accession: str) -> bytes:
    """Issue a SEC HTTP GET and return the response body.

    Any non-2xx response is converted to ``SecHttpFailError`` (typed
    EXIT). Network-level errors (``URLError``) propagate unchanged —
    those are operator-environment failures, not spec contract
    failures, and the live-fetch CLI logs them as-is. The
    ``accession`` is included in the error message so the operator
    log carries enough context to decide whether to retry.
    """
    request = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urlopen(request) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise SecHttpFailError(
            f"SEC HTTP {exc.code} for accession={accession} url={url}",
            status_code=int(exc.code),
            accession=accession,
        ) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _write_live_stub(
    *,
    accession: str,
    body: bytes,
    extracted_root: Path,
    facts_root: Path,
) -> None:
    """Persist the live-fetched HTML bytes into the extracted tree.

    T1 ships only the typed-EXIT scaffolding for the live path; the
    HTML partitioning into ``unit`` is deferred (ADR 0028 leaves that
    decision to a later ADR). For T1 we hash the bytes, write a single
    stub ``page-001.json`` carrying the raw HTML as ``text``, and a
    stub fact file with the live locator (no XBRL tag facts yet — T5
    owns the parser that emits ``us-gaap:Revenues`` rows from iXBRL).
    """
    version = _sha256_bytes(body)
    doc_dir = extracted_root / accession
    doc_dir.mkdir(parents=True, exist_ok=True)
    page_payload = json.dumps(
        {
            "document_id": accession,
            "version": version,
            "page": 1,
            "text": body.decode("utf-8", errors="replace"),
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write(doc_dir / "page-001.json", page_payload)
    index_payload = json.dumps(
        {
            "schema_version": 1,
            "documents": {accession: {"version": version, "page_count": 1}},
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write(extracted_root / "_index.json", index_payload)
    # No facts emitted for live path in T1: iXBRL tag parsing is
    # deferred to a later ADR (T5 owns the get_xbrl_fact lookup and
    # the XBRL parser that feeds it).
    facts_root.mkdir(parents=True, exist_ok=True)
    facts_payload = json.dumps(
        {"facts": []}, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")
    _atomic_write(facts_root / f"{accession}.json", facts_payload)


def fetch_and_ingest(
    *,
    cik: str,
    accession: str,
    manifest_path: Path,
    extracted_root: Path,
    facts_root: Path,
) -> bytes:
    """Run the UA-gated live fetch + write.

    Returns the response body so the caller (CLI) can log a digest.

    Raises
    ------
    SecUaUnsetError
        ``FIN_EDGAR_USER_AGENT`` is unset or empty. NO HTTP request
        was made.
    SecHttpFailError
        The SEC HTTP call returned non-2xx. The catalog/extracted
        tree for ``accession`` was NOT created.
    """
    ua = _read_ua()
    padded = cik.zfill(10)
    url = (
        f"https://{_EDGAR_ARCHIVES_HOST}/Archives/edgar/data/"
        f"{padded}/{accession.replace('-', '')}/"
    )
    body = _sec_get(url, ua, accession=accession)
    _write_live_stub(
        accession=accession,
        body=body,
        extracted_root=extracted_root,
        facts_root=facts_root,
    )
    # The ``manifest_path`` argument is part of the public API; the
    # full manifest update (write a row keyed by ``accession``) lands
    # when the live partitioner (deferred ADR) is in. For T1 we only
    # need the typed-EXIT scaffolding, so the manifest stays
    # untouched here.
    _ = manifest_path
    return body


def _default_paths() -> tuple[Path, Path, Path]:
    project_root = Path(__file__).resolve().parents[3]
    corpus_dir = project_root / "data" / "corpus"
    return (
        corpus_dir / "manifest.json",
        corpus_dir / "extracted",
        corpus_dir / "facts",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m fin.corpus.live_fetch``.

    Parses ``--cik`` and ``--accession``, reads ``FIN_EDGAR_USER_AGENT``
    from the environment, and exits non-zero with a typed message on
    any of the spec's EXITs (UA-unset → ``SecUaUnsetError``; HTTP fail
    → ``SecHttpFailError``).
    """
    import argparse

    default_manifest, default_extracted, default_facts = _default_paths()
    parser = argparse.ArgumentParser(
        prog="python -m fin.corpus.live_fetch",
        description=(
            "fin S1 UA-gated live EDGAR fetch: writes the official "
            "primary iXBRL HTML into the extracted tree (ADR 0028). "
            "Refuses to call SEC without FIN_EDGAR_USER_AGENT set."
        ),
    )
    parser.add_argument("--cik", required=True, help="EDGAR CIK, e.g. 0000320193")
    parser.add_argument(
        "--accession",
        required=True,
        help="EDGAR accession number, e.g. 0000320193-23-000106",
    )
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument(
        "--extracted-root", type=Path, default=default_extracted
    )
    parser.add_argument("--facts-root", type=Path, default=default_facts)
    args = parser.parse_args(argv)

    try:
        fetch_and_ingest(
            cik=args.cik,
            accession=args.accession,
            manifest_path=args.manifest,
            extracted_root=args.extracted_root,
            facts_root=args.facts_root,
        )
    except SecUaUnsetError as exc:
        print(f"live_fetch: {exc}", file=__import__("sys").stderr)
        return 2
    except SecHttpFailError as exc:
        print(
            f"live_fetch: sec_http_fail status={exc.status_code} "
            f"accession={exc.accession}: {exc}",
            file=__import__("sys").stderr,
        )
        return 3
    return 0


__all__ = [
    "SecHttpFailError",
    "SecUaUnsetError",
    "fetch_and_ingest",
    "main",
]


if __name__ == "__main__":
    import sys

    sys.exit(main())