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
* **Set + HTTP success** → the accession's primary document is
  resolved first, then fetched; those bytes are hashed as the
  ``version`` and the unit + catalog artifacts are written.
* **Set + HTTP failure (non-2xx)** → ``SecHttpFailError`` is raised,
  the manifest is NOT updated, the extracted tree for that accession
  is NOT created. This is the ``sec_http_fail`` typed EXIT (spec §
  Boundaries).

Which bytes are the filing
--------------------------

An EDGAR accession is a directory of dozens of files: the primary
iXBRL document, its XBRL sidecars, exhibits, and the rendered
``R*.htm`` fragments. Only the primary document is the filing, and
spec § **locator** pins ``version`` to *its* bytes — so this module
resolves the primary document **before** the GET whose response it
hashes. Resolution reads the accession's ``index.json`` listing and
applies ``_pick_primary_document``; ``--primary-doc`` skips it when
the operator already knows the filename.

The frozen unit
---------------

Spec § **locator**: the live unit is "a deterministic frozen partition
of that HTML's stripped text". ``_strip_html`` does the stripping with
stdlib ``html.parser``. The partition itself is still the trivial
one — the whole document is unit 1 — because how HTML splits into
multiple units is explicitly left open by ADR 0028. What the unit is
*made of* is not open, so the text is stripped rather than raw markup:
a citation gate matching a quote against ``<span>``-laden source would
refuse every real span.

Imports kept minimal
--------------------

We use stdlib ``urllib.request`` only — no new dep. ``pdfplumber``
does not appear on this path; if it did, the ask-path contract (ADR
0020) would be violated the moment ``fin.corpus.live_fetch`` was
imported. That is also why the passage window below is spelled out
here instead of imported from ``fin.corpus.ingest``: importing that
module would pull ``pdfplumber`` onto this path. The rule (512-char
window, 64-char overlap) is the same one, and must stay the same.
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from fin.corpus.errors import IngestMissingError, MalformedExtractedIndexError


# Import at module level so tests can mock
# ``fin.corpus.live_fetch.urlopen`` to assert no SEC request is made
# when ``FIN_EDGAR_USER_AGENT`` is unset. Without this re-binding the
# ``urllib.request.urlopen`` call inside ``_sec_get`` would be a
# free-function reference, not a module attribute, and the test mock
# would fail with ``AttributeError``.
urlopen = urllib.request.urlopen


_UA_ENV = "FIN_EDGAR_USER_AGENT"

_EDGAR_ARCHIVES_HOST = "www.sec.gov"

# Passage window — must match ``fin.corpus.ingest`` (see module
# docstring for why it is not imported from there).
_PASSAGE_SIZE = 512
_PASSAGE_OVERLAP = 64

# Inline-XBRL filings name their primary document ``<slug>-YYYYMMDD.htm``
# (``aapl-20230701.htm``). Nothing else in an accession matches, so
# this is the high-confidence pick.
_IXBRL_PRIMARY_NAME = re.compile(r"^[a-z0-9]+-\d{8}\.html?$", re.IGNORECASE)

# Rendered report fragments EDGAR generates from the XBRL — never the
# filing itself.
_RENDERED_FRAGMENT_NAME = re.compile(r"^R\d+\.html?$", re.IGNORECASE)

# HTML elements whose character data is not filing text. ``ix:header``
# is the inline-XBRL header block: tens of kilobytes of hidden tagged
# facts and context definitions that no reader sees and no citation
# can quote.
_NON_TEXT_ELEMENTS = frozenset({"ix:header", "script", "style", "title"})

# Elements that end a line of visible text. Keeping the break makes
# the stripped text stable and readable instead of one long run-on
# paragraph.
_BLOCK_ELEMENTS = frozenset(
    {
        "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "ol",
        "p", "table", "tbody", "thead", "tr", "ul",
    }
)

# Table cells separate with a space, not a line break, so one row of a
# financial statement freezes as one line — "Products $ 60,584 $
# 63,355" is the shape a claimed span is quoted in. Breaking per cell
# would make every table figure unquotable.
_CELL_ELEMENTS = frozenset({"td", "th"})


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


class SecPrimaryDocUnresolvedError(IngestMissingError):
    """Typed EXIT: the accession listing names no primary document.

    Guessing here would pin a cite identity to an exhibit or to a
    rendered ``R*.htm`` fragment, so the run stops with the accession
    unmarked and the operator re-runs with ``--primary-doc``.
    """

    def __init__(self, message: str, *, accession: str) -> None:
        super().__init__(message)
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


def _accession_base_url(cik: str, accession: str) -> str:
    """Archive directory URL for one accession.

    EDGAR keys the archive path on the CIK with leading zeros stripped
    and the accession with its dashes removed; the zero-padded form
    answers 301 to this one.
    """
    return (
        f"https://{_EDGAR_ARCHIVES_HOST}/Archives/edgar/data/"
        f"{cik.strip().lstrip('0') or '0'}/{accession.replace('-', '')}/"
    )


def _listing_documents(listing: bytes, *, accession: str) -> list[tuple[str, int]]:
    """Parse an accession ``index.json`` into ``(name, size)`` pairs.

    A listing we cannot parse is the same EXIT as a listing with no
    primary document: either way this run has no defensible way to
    choose the filing's bytes.
    """
    try:
        payload = json.loads(listing)
        items = payload["directory"]["item"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SecPrimaryDocUnresolvedError(
            f"accession {accession} listing is not an EDGAR index.json",
            accession=accession,
        ) from exc
    out: list[tuple[str, int]] = []
    for item in items:
        name = str(item.get("name", ""))
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append((name, size))
    return out


def _pick_primary_document(
    documents: list[tuple[str, int]], *, accession: str
) -> str:
    """Choose the accession's primary iXBRL document.

    Candidates are the ``.htm`` / ``.html`` entries that are not the
    ``-index`` wrappers and not rendered ``R*.htm`` fragments. Among
    them the inline-XBRL naming convention wins outright; filings that
    predate it (``msft-10q_20220930.htm``) fall back to the largest
    candidate, which is the filing body by a wide margin — exhibits
    are a few kilobytes against the document's megabytes.

    Ties break on the name so two runs over one accession resolve to
    the same bytes.
    """
    candidates = [
        (name, size)
        for name, size in documents
        if name.lower().endswith((".htm", ".html"))
        and "-index" not in name.lower()
        and not _RENDERED_FRAGMENT_NAME.match(name)
    ]
    if not candidates:
        raise SecPrimaryDocUnresolvedError(
            f"accession {accession} listing has no candidate primary "
            "document; re-run with --primary-doc",
            accession=accession,
        )
    ixbrl = sorted(
        name for name, _ in candidates if _IXBRL_PRIMARY_NAME.match(name)
    )
    if ixbrl:
        return ixbrl[0]
    return sorted(candidates, key=lambda pair: (-pair[1], pair[0]))[0][0]


def _resolve_primary_url(
    *, base_url: str, ua: str, accession: str, primary_doc: str | None
) -> str:
    """Return the URL of the accession's primary document.

    Costs one extra GET (the listing) unless ``primary_doc`` is given.
    That GET's bytes are never hashed — only the document it points
    at is.
    """
    if primary_doc:
        return base_url + primary_doc.lstrip("/")
    listing = _sec_get(base_url + "index.json", ua, accession=accession)
    name = _pick_primary_document(
        _listing_documents(listing, accession=accession), accession=accession
    )
    return base_url + name


class _TextExtractor(html.parser.HTMLParser):
    """Collect the visible character data of an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _NON_TEXT_ELEMENTS:
            self._suppress += 1
        elif not self._suppress:
            self._parts.append(self._separator(tag))

    def handle_endtag(self, tag: str) -> None:
        if tag in _NON_TEXT_ELEMENTS:
            self._suppress = max(0, self._suppress - 1)
        elif not self._suppress:
            self._parts.append(self._separator(tag))

    @staticmethod
    def _separator(tag: str) -> str:
        if tag in _BLOCK_ELEMENTS:
            return "\n"
        if tag in _CELL_ELEMENTS:
            return " "
        return ""

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _strip_html(body: bytes) -> str:
    """Render the official HTML down to its visible text.

    Deterministic by construction: decoding errors are replaced rather
    than guessed at, non-breaking spaces collapse to ordinary ones,
    and blank lines are dropped — so the same bytes always freeze to
    the same unit text, which is what the citation verifier's
    substring check depends on.
    """
    parser = _TextExtractor()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    lines = (
        re.sub(r"[^\S\n]+", " ", line).strip()
        for line in parser.text().replace("\xa0", " ").split("\n")
    )
    return "\n".join(line for line in lines if line)


def _chunk_unit_text(text: str) -> list[dict[str, object]]:
    """Fixed 512/64 sliding window over the unit text.

    Same rule as ``fin.corpus.ingest`` — see the module docstring for
    why it is restated instead of imported.
    """
    chunks: list[dict[str, object]] = []
    step = _PASSAGE_SIZE - _PASSAGE_OVERLAP
    start = 0
    while start < len(text):
        end = min(start + _PASSAGE_SIZE, len(text))
        chunks.append(
            {"text": text[start:end], "char_start": start, "char_end": end}
        )
        if end >= len(text):
            break
        start += step
    return chunks


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _dump(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")


def _read_index_documents(index_path: Path) -> dict[str, dict[str, object]]:
    """Read the per-document records out of an existing ``_index.json``.

    A missing index is an empty catalog. A present-but-unreadable one
    is an EXIT: overwriting it would turn a damaged index into a
    single-entry index and quietly drop every other document from the
    query SSOT (ADR 0020).
    """
    if not index_path.is_file():
        raise MalformedExtractedIndexError(
            f"extracted index is not a regular file: {index_path}"
        )
    try:
        payload = json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise MalformedExtractedIndexError(
            f"malformed extracted index: {index_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise MalformedExtractedIndexError(
            f"extracted index must be a JSON object: {index_path}"
        )
    documents = payload.get("documents")
    if not isinstance(documents, dict):
        raise MalformedExtractedIndexError(
            f"extracted index has no 'documents' object: {index_path}"
        )
    for document_id, entry in documents.items():
        if (
            not isinstance(document_id, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("version"), str)
            or not entry["version"]
            or not isinstance(entry.get("page_count"), int)
            or isinstance(entry["page_count"], bool)
            or entry["page_count"] < 0
        ):
            raise MalformedExtractedIndexError(
                f"extracted index has an invalid document record: {index_path}"
            )
    return documents


def _write_live_unit(
    *,
    accession: str,
    body: bytes,
    source_url: str,
    extracted_root: Path,
    facts_root: Path,
    existing_documents: dict[str, dict[str, object]] | None = None,
) -> str:
    """Freeze the primary document into the extracted tree.

    Writes the unit text, its passages, and a merged ``_index.json``
    entry, then returns the ``version``. The index is read-merged so a
    live fetch adds one document to the tree rather than becoming the
    tree.
    """
    version = _sha256_bytes(body)
    text = _strip_html(body)

    index_path = extracted_root / "_index.json"
    extracted_root.mkdir(parents=True, exist_ok=True)
    documents = (
        dict(existing_documents)
        if existing_documents is not None
        else _read_index_documents(index_path)
    )

    doc_dir = extracted_root / accession
    doc_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        doc_dir / "page-001.json",
        _dump(
            {
                "document_id": accession,
                "version": version,
                "page": 1,
                "text": text,
                "ocr_applied": False,
                "source_url": source_url,
            }
        ),
    )
    _atomic_write(
        doc_dir / "passages-001.json",
        _dump(
            {
                "document_id": accession,
                "version": version,
                "page": 1,
                "passages": _chunk_unit_text(text),
            }
        ),
    )

    documents[accession] = {
        "version": version,
        "page_count": 1,
        "source_url": source_url,
    }
    _atomic_write(
        index_path,
        _dump({"schema_version": 1, "documents": dict(sorted(documents.items()))}),
    )

    # No facts emitted for live path in T1: iXBRL tag parsing is
    # deferred to a later ADR (T5 owns the get_xbrl_fact lookup and
    # the XBRL parser that feeds it).
    facts_root.mkdir(parents=True, exist_ok=True)
    _atomic_write(facts_root / f"{accession}.json", _dump({"facts": []}))
    return version


def _read_manifest_payload(manifest_path: Path) -> dict[str, object]:
    """Read the catalog manifest before staging any live artifacts.

    A missing manifest is the initialization case. A present malformed
    manifest is corruption, not an empty catalog, because treating it as
    empty would discard rows owned by another ingest path.
    """
    if not manifest_path.exists():
        return {"documents": []}
    try:
        payload = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise IngestMissingError(
            f"refusing to merge into an unreadable manifest: {manifest_path}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("documents"), list
    ):
        raise IngestMissingError(
            f"manifest has no 'documents' list: {manifest_path}"
        )
    if any(not isinstance(document, dict) for document in payload["documents"]):
        raise IngestMissingError(
            f"manifest has an invalid document record: {manifest_path}"
        )
    return payload


def _live_manifest_payload(
    *,
    manifest_payload: dict[str, object],
    cik: str,
    accession: str,
    primary_url: str,
    body: bytes,
    version: str,
) -> dict[str, object]:
    """Return a manifest copy with the live accession registered.

    Existing metadata for the same accession is reused when available.
    A fetch with no prior catalog row gets explicit placeholder values
    for fields that cannot be inferred from the two live-fetch inputs.
    """
    documents = list(manifest_payload["documents"])
    template: dict[str, object] = {}
    for document in documents:
        if document.get("document_id") == accession or document.get(
            "accession"
        ) == accession:
            template = dict(document)
            break

    filename = primary_url.rsplit("/", 1)[-1]
    row = {
        **template,
        "document_id": accession,
        "source_id": template.get("source_id", f"EDGAR-{cik}"),
        "company": template.get("company", cik),
        "ticker": template.get("ticker"),
        "cik": cik,
        "accession": accession,
        "filing_date": template.get("filing_date"),
        "form": template.get("form"),
        "period": template.get("period", "live"),
        "doc_type": template.get("doc_type", template.get("form")),
        "filename": filename,
        "upstream_name": template.get("upstream_name", filename),
        "bytes": len(body),
        "sha256": version,
    }
    documents = [
        document
        for document in documents
        if document.get("document_id") != accession
    ]
    documents.append(row)
    return {**manifest_payload, "documents": documents}


def _move_to_backup(target: Path, backup: Path) -> bool:
    """Move an existing publish target aside on the same filesystem."""
    if not target.exists():
        return False
    os.replace(target, backup)
    return True


_PUBLISH_JOURNAL_NAME = ".live-fetch-publish.json"


def _publish_journal_path(manifest_path: Path) -> Path:
    return manifest_path.parent / _PUBLISH_JOURNAL_NAME


def _remove_publish_target(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _read_publish_journal(journal_path: Path) -> dict[str, object]:
    try:
        payload = json.loads(journal_path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise IngestMissingError(
            f"malformed live publish journal: {journal_path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("state") not in {"prepared", "committed"}
        or not isinstance(payload.get("entries"), list)
        or not isinstance(payload.get("cleanup_dirs"), list)
    ):
        raise IngestMissingError(
            f"invalid live publish journal: {journal_path}"
        )
    return payload


def _recover_interrupted_publish(*, manifest_path: Path) -> None:
    """Complete or roll back a publish left by a process crash.

    The manifest is the catalog's commit pointer: it is replaced last.
    The journal makes that pointer durable across a crash, so a later
    live-fetch operation can discard every uncommitted target before it
    reads the manifest or extracted index.
    """
    journal_path = _publish_journal_path(manifest_path)
    if not journal_path.is_file():
        return

    payload = _read_publish_journal(journal_path)
    raw_entries = payload["entries"]
    raw_cleanup_dirs = payload["cleanup_dirs"]
    entries: list[tuple[Path, Path, Path, bool]] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise IngestMissingError(
                f"invalid live publish journal entry: {journal_path}"
            )
        staged = raw_entry.get("staged")
        target = raw_entry.get("target")
        backup = raw_entry.get("backup")
        target_existed = raw_entry.get("target_existed")
        if (
            not all(isinstance(path, str) for path in (staged, target, backup))
            or not isinstance(target_existed, bool)
        ):
            raise IngestMissingError(
                f"invalid live publish journal entry: {journal_path}"
            )
        entries.append(
            (Path(staged), Path(target), Path(backup), target_existed)
        )

    committed = payload["state"] == "committed" or all(
        not staged.exists() for staged, _target, _backup, _existed in entries
    )
    if not committed:
        for staged, target, backup, target_existed in reversed(entries):
            if backup.exists():
                _remove_publish_target(target)
                os.replace(backup, target)
            elif not staged.exists() and not target_existed:
                _remove_publish_target(target)

    for raw_dir in raw_cleanup_dirs:
        if not isinstance(raw_dir, str):
            raise IngestMissingError(
                f"invalid live publish cleanup path: {journal_path}"
            )
        shutil.rmtree(raw_dir, ignore_errors=True)
    journal_path.unlink(missing_ok=True)


def _rollback_publish(
    *,
    published: list[Path],
    backups: list[tuple[Path, Path]],
) -> None:
    """Restore targets after a failed multi-file publish attempt."""
    for target in reversed(published):
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    for target, backup in reversed(backups):
        if backup.exists():
            os.replace(backup, target)


def _publish_staged(
    *,
    staged_doc_dir: Path,
    staged_index: Path,
    staged_facts: Path,
    staged_manifest: Path,
    extracted_root: Path,
    facts_root: Path,
    manifest_path: Path,
) -> None:
    """Publish prepared artifacts with crash recovery across four targets.

    Page and passage files are only written below staging directories.
    A durable journal records the generation before any target is
    replaced. The manifest is the catalog commit pointer and is last,
    so catalog readers never discover an accession before its extracted
    artifacts.
    """
    targets = [
        (staged_doc_dir, extracted_root / staged_doc_dir.name, extracted_root),
        (staged_facts, facts_root / staged_facts.name, facts_root),
        (staged_index, extracted_root / "_index.json", extracted_root),
        (staged_manifest, manifest_path, manifest_path.parent),
    ]
    backup_dirs: dict[Path, Path] = {}
    for _staged, target, parent in targets:
        backup_dirs.setdefault(
            parent,
            Path(tempfile.mkdtemp(prefix=".live-publish-backup-", dir=parent)),
        )

    journal_path = _publish_journal_path(manifest_path)
    journal_entries = [
        {
            "staged": str(staged),
            "target": str(target),
            "backup": str(backup_dirs[parent] / target.name),
            "target_existed": target.exists(),
        }
        for staged, target, parent in targets
    ]
    cleanup_dirs = [
        str(staged_doc_dir.parent),
        str(staged_facts.parent),
        str(staged_manifest.parent),
        *(str(backup_dir) for backup_dir in backup_dirs.values()),
    ]
    published: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    try:
        _atomic_write(
            journal_path,
            _dump(
                {
                    "cleanup_dirs": cleanup_dirs,
                    "entries": journal_entries,
                    "state": "prepared",
                }
            ),
        )
        for staged, target, parent in targets:
            backup = backup_dirs[parent] / target.name
            if _move_to_backup(target, backup):
                backups.append((target, backup))
            published.append(target)
            os.replace(staged, target)
        _atomic_write(
            journal_path,
            _dump(
                {
                    "cleanup_dirs": cleanup_dirs,
                    "entries": journal_entries,
                    "state": "committed",
                }
            ),
        )
        journal_path.unlink(missing_ok=True)
    except BaseException:
        _rollback_publish(published=published, backups=backups)
        journal_path.unlink(missing_ok=True)
        raise
    finally:
        for backup_dir in backup_dirs.values():
            shutil.rmtree(backup_dir, ignore_errors=True)


def fetch_and_ingest(
    *,
    cik: str,
    accession: str,
    manifest_path: Path,
    extracted_root: Path,
    facts_root: Path,
    primary_doc: str | None = None,
) -> bytes:
    """Run the UA-gated live fetch + write.

    Resolves the accession's primary iXBRL document, fetches it, and
    freezes its stripped text as unit 1 under ``accession``. Returns
    the primary document's bytes so the caller (CLI) can log a digest.

    Raises
    ------
    SecUaUnsetError
        ``FIN_EDGAR_USER_AGENT`` is unset or empty. NO HTTP request
        was made.
    SecHttpFailError
        A SEC HTTP call returned non-2xx. The catalog/extracted tree
        for ``accession`` was NOT created.
    SecPrimaryDocUnresolvedError
        The accession listing names no primary document. Nothing was
        written; re-run with ``primary_doc``.
    """
    ua = _read_ua()
    _recover_interrupted_publish(manifest_path=manifest_path)
    base_url = _accession_base_url(cik, accession)
    primary_url = _resolve_primary_url(
        base_url=base_url, ua=ua, accession=accession, primary_doc=primary_doc
    )
    body = _sec_get(primary_url, ua, accession=accession)
    manifest_payload = _read_manifest_payload(manifest_path)
    existing_documents = _read_index_documents(
        extracted_root / "_index.json"
    ) if (extracted_root / "_index.json").exists() else {}
    version = _sha256_bytes(body)

    extracted_root.parent.mkdir(parents=True, exist_ok=True)
    facts_root.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    staged_extracted = Path(
        tempfile.mkdtemp(prefix=".live-fetch-", dir=extracted_root.parent)
    )
    staged_facts_root = Path(
        tempfile.mkdtemp(prefix=".live-fetch-facts-", dir=facts_root.parent)
    )
    staged_manifest_root = Path(
        tempfile.mkdtemp(prefix=".live-fetch-manifest-", dir=manifest_path.parent)
    )
    try:
        _write_live_unit(
            accession=accession,
            body=body,
            source_url=primary_url,
            extracted_root=staged_extracted,
            facts_root=staged_facts_root,
            existing_documents=existing_documents,
        )
        staged_manifest = staged_manifest_root / manifest_path.name
        _atomic_write(
            staged_manifest,
            _dump(
                _live_manifest_payload(
                    manifest_payload=manifest_payload,
                    cik=cik,
                    accession=accession,
                    primary_url=primary_url,
                    body=body,
                    version=version,
                )
            ),
        )
        extracted_root.mkdir(parents=True, exist_ok=True)
        facts_root.mkdir(parents=True, exist_ok=True)
        _publish_staged(
            staged_doc_dir=staged_extracted / accession,
            staged_index=staged_extracted / "_index.json",
            staged_facts=staged_facts_root / f"{accession}.json",
            staged_manifest=staged_manifest,
            extracted_root=extracted_root,
            facts_root=facts_root,
            manifest_path=manifest_path,
        )
    finally:
        shutil.rmtree(staged_extracted, ignore_errors=True)
        shutil.rmtree(staged_facts_root, ignore_errors=True)
        shutil.rmtree(staged_manifest_root, ignore_errors=True)
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
    → ``SecHttpFailError``; unresolvable primary document →
    ``SecPrimaryDocUnresolvedError``).
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
        help="EDGAR accession number, e.g. 0000320193-23-000077",
    )
    parser.add_argument(
        "--primary-doc",
        default=None,
        help=(
            "Filename of the primary iXBRL document within the "
            "accession, e.g. aapl-20230701.htm. Skips the index.json "
            "lookup; required when that lookup cannot resolve one."
        ),
    )
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument(
        "--extracted-root", type=Path, default=default_extracted
    )
    parser.add_argument("--facts-root", type=Path, default=default_facts)
    args = parser.parse_args(argv)

    import sys

    try:
        body = fetch_and_ingest(
            cik=args.cik,
            accession=args.accession,
            manifest_path=args.manifest,
            extracted_root=args.extracted_root,
            facts_root=args.facts_root,
            primary_doc=args.primary_doc,
        )
    except SecUaUnsetError as exc:
        print(f"live_fetch: {exc}", file=sys.stderr)
        return 2
    except SecHttpFailError as exc:
        print(
            f"live_fetch: sec_http_fail status={exc.status_code} "
            f"accession={exc.accession}: {exc}",
            file=sys.stderr,
        )
        return 3
    except SecPrimaryDocUnresolvedError as exc:
        print(f"live_fetch: primary_doc_unresolved: {exc}", file=sys.stderr)
        return 4
    print(
        f"live_fetch: accession={args.accession} "
        f"version={_sha256_bytes(body)} bytes={len(body)}"
    )
    return 0


__all__ = [
    "SecHttpFailError",
    "SecPrimaryDocUnresolvedError",
    "SecUaUnsetError",
    "fetch_and_ingest",
    "main",
]


if __name__ == "__main__":
    import sys

    sys.exit(main())
