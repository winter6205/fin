"""Metadata-free upload: title sanitization, doc type, identity (T6).

The frozen contract (``specs/qa-chain-corpus-retrieval.md`` section
"Metadata-free ingest and document identity") says an upload needs no
sidecar metadata file and no financial identity, and that identity is
derived ONLY from the trusted corpus-scope id and the SHA-256 of the
uploaded bytes. The filename is display metadata only — it reaches the
title and nothing else.

Covered here:

* ``sanitize_upload_title`` — both separator styles, NFC, ``Cc``
  control removal, wildcard replacement, trimming, UTF-8-boundary
  truncation, and the ``Untitled document`` fallback;
* ``resolve_explicit_metadata`` — the shared normalized non-empty
  string / 1-256-byte / no-control bounds, and the ``*`` / ``?``
  rejection that happens BEFORE any write;
* ``derive_document_identity`` — the literal formula, its stability
  across a retry, its distinctness across scopes, filenames and
  contents.

The ``doc_type`` half of the same contract — accepted override, the
deterministic classifier rollup, and the refusal of an override
outside the closed vocabulary — is resolved by the RUN rather than
here, so it is pinned at the endpoint that owns the single rollup
(``tests/corpus/test_ingest_endpoint.py``) instead.

Every case is pure: no store, no filesystem, no network.
"""

from __future__ import annotations

import hashlib
import unicodedata

import pytest

from fin.corpus.upload_metadata import (
    UNKNOWN_DOC_TYPE,
    UNTITLED_DOCUMENT,
    InvalidUploadMetadataError,
    derive_document_identity,
    resolve_explicit_metadata,
    sanitize_upload_title,
)


# ---------- title: filename fallback ----------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("/tmp/nested/report.pdf", "report.pdf"),
        (r"C:\Users\me\report.pdf", "report.pdf"),
        # Both separator styles keep only the FINAL component, so a
        # Windows path arriving on a POSIX host is not rendered whole.
        ("a/b\\c/deep.md", "deep.md"),
        ("dir/", UNTITLED_DOCUMENT),
        ("", UNTITLED_DOCUMENT),
        ("///", UNTITLED_DOCUMENT),
    ],
)
def test_only_the_final_path_component_survives(
    filename: str, expected: str
) -> None:
    assert sanitize_upload_title(None, filename=filename) == expected


def test_nfc_normalization_is_applied() -> None:
    """Decomposed input becomes its composed form.

    ``e`` + COMBINING ACUTE and the precomposed ``é`` are the same title
    to an operator and must produce the same catalog title, or one
    document is listed twice under two spellings.
    """
    decomposed = "Cafe\u0301"  # e + COMBINING ACUTE ACCENT
    composed = unicodedata.normalize("NFC", decomposed)

    assert decomposed != composed
    assert sanitize_upload_title(None, filename=decomposed + ".pdf") == (
        composed + ".pdf"
    )


def test_control_characters_are_removed() -> None:
    """``Cc`` controls go; the rest of the name stays readable."""
    assert sanitize_upload_title(None, filename="re\x00po\x07rt.pdf") == (
        "report.pdf"
    )


def test_a_bare_newline_and_tab_inside_the_name_are_removed() -> None:
    assert sanitize_upload_title(None, filename="re\nport\t.pdf") == "report.pdf"


def test_wildcards_are_replaced_with_underscore() -> None:
    """``*`` and ``?`` never reach the title verbatim.

    They are the filter vocabulary's wildcard characters; a title
    carrying one would name a document no exact-match filter can reach.
    """
    assert sanitize_upload_title(None, filename="re*port?.pdf") == "re_port_.pdf"


def test_surrounding_whitespace_is_trimmed() -> None:
    assert sanitize_upload_title(None, filename="  report.pdf \t ") == (
        "report.pdf"
    )


def test_truncation_lands_on_a_utf8_boundary() -> None:
    """A long name is cut at 256 BYTES, never mid-character.

    Cutting mid-character would store a title that cannot be decoded,
    searched or rendered.
    """
    title = sanitize_upload_title(None, filename="é" * 200 + ".pdf")

    assert len(title.encode("utf-8")) <= 256
    # Every character is intact — the cut dropped whole characters.
    assert "�" not in title
    title.encode("utf-8").decode("utf-8")  # round-trips


def test_an_exactly_256_byte_title_is_not_truncated() -> None:
    title = sanitize_upload_title(None, filename="a" * 256)

    assert title == "a" * 256


def test_a_257_byte_title_is_truncated_to_256() -> None:
    title = sanitize_upload_title(None, filename="a" * 300)

    assert len(title) == 256


def test_a_name_that_sanitizes_to_nothing_is_untitled() -> None:
    assert sanitize_upload_title(None, filename="\x00\x01") == "Untitled document"
    assert sanitize_upload_title(None, filename=None) == "Untitled document"
    assert sanitize_upload_title(None, filename="   ") == "Untitled document"


def test_an_explicit_title_wins_over_the_filename() -> None:
    assert sanitize_upload_title("Quarterly plan", filename="q3.pdf") == (
        "Quarterly plan"
    )


def test_an_explicit_title_is_normalized_and_validated_too() -> None:
    """The explicit path takes the SAME bounds as the fallback.

    An explicit title is not a privileged channel: a caller cannot
    smuggle a control character or a 300-byte string past the bounds the
    filename path enforces. It is VALIDATED rather than silently edited,
    so an over-long or control-bearing title is a named refusal the
    uploader can fix by retyping — not a title fin quietly changed.
    """
    assert sanitize_upload_title("  Cafe\u0301  ", filename="x.pdf") == "Café"
    with pytest.raises(InvalidUploadMetadataError):
        sanitize_upload_title("a" * 300, filename="x.pdf")
    with pytest.raises(InvalidUploadMetadataError):
        sanitize_upload_title("a\x00b", filename="x.pdf")


def test_an_explicit_title_sanitizing_to_nothing_falls_back_to_the_filename() -> (
    None
):
    """A blank explicit title does not discard the name the operator sent."""
    assert sanitize_upload_title("  ", filename="report.pdf") == "report.pdf"


# ---------- explicit metadata bounds ----------


@pytest.mark.parametrize("value", ["  Acme  ", "Acme"])
def test_explicit_metadata_is_nfc_normalized_and_trimmed(value: str) -> None:
    assert resolve_explicit_metadata(value, field="company") == (
        unicodedata.normalize("NFC", value).strip()
    )


def test_whitespace_only_explicit_metadata_is_absent_not_blank() -> None:
    """Blank means "the caller did not say", never "the company is ''"."""
    assert resolve_explicit_metadata("   ", field="company") is None
    assert resolve_explicit_metadata("", field="company") is None
    assert resolve_explicit_metadata(None, field="company") is None


def test_a_256_byte_explicit_metadata_value_is_accepted() -> None:
    assert resolve_explicit_metadata("a" * 256, field="title") == "a" * 256


@pytest.mark.parametrize(
    "value",
    ["a" * 257, "x\x00y", "x\x07y", "re*port", "re?port"],
)
def test_out_of_bounds_or_wildcard_explicit_metadata_is_rejected(
    value: str,
) -> None:
    """Named ``invalid_upload_metadata``, decided before any write.

    The wildcard rejection matters most: ``*`` and ``?`` are the filter
    vocabulary's wildcards, so a title carrying one would be a value no
    exact-match filter can ever reach.
    """
    with pytest.raises(InvalidUploadMetadataError) as exc_info:
        resolve_explicit_metadata(value, field="title")
    assert exc_info.value.field == "title"


def test_a_non_string_explicit_metadata_value_is_rejected() -> None:
    with pytest.raises(InvalidUploadMetadataError):
        resolve_explicit_metadata(17, field="company")  # type: ignore[arg-type]


def test_the_rejection_names_the_field_and_not_the_value() -> None:
    """The message names the FIELD; the message never echoes the value.

    The value is caller-supplied free text, and the audit/journal
    surfaces render this message — echoing it would put unvalidated
    text into an operator-facing log.
    """
    with pytest.raises(InvalidUploadMetadataError) as exc_info:
        resolve_explicit_metadata("secret-looking-value\x00", field="company")
    message = str(exc_info.value)
    assert "company" in message
    assert "secret-looking-value" not in message


# ---------- identity ----------


_SCOPE = "fin-default"


def _expected_document_id(scope: str, data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00"
        + scope.encode("utf-8")
        + b"\x00"
        + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()


def test_identity_matches_the_specified_formula() -> None:
    """The literal preimage: domain tag, scope, NUL, raw digest.

    Written out longhand here rather than by calling the production
    helper, so a change to the formula fails this test instead of
    redefining itself.
    """
    data = b"plain upload bytes"

    identity = derive_document_identity(
        data, corpus_scope_id=_SCOPE, filename="whatever.pdf"
    )

    assert identity.version == hashlib.sha256(data).hexdigest()
    assert identity.version == identity.version.lower()
    assert len(identity.version) == 64
    assert identity.document_id == _expected_document_id(_SCOPE, data)


def test_the_same_bytes_in_the_same_scope_reuse_one_identity() -> None:
    """A retry joins the accepted job instead of creating a second row."""
    first = derive_document_identity(b"same", corpus_scope_id=_SCOPE)
    second = derive_document_identity(b"same", corpus_scope_id=_SCOPE)

    assert first == second


def test_the_same_filename_with_different_bytes_stays_distinct() -> None:
    """The filename is display metadata and is not part of identity."""
    first = derive_document_identity(
        b"one", corpus_scope_id=_SCOPE, filename="q3.pdf"
    )
    second = derive_document_identity(
        b"two", corpus_scope_id=_SCOPE, filename="q3.pdf"
    )

    assert first.document_id != second.document_id
    assert first.version != second.version


def test_the_same_bytes_under_different_filenames_reuse_one_identity() -> None:
    """Display metadata is not identity: a renamed re-upload joins."""
    first = derive_document_identity(
        b"same", corpus_scope_id=_SCOPE, filename="a.pdf"
    )
    second = derive_document_identity(
        b"same", corpus_scope_id=_SCOPE, filename="b.pdf"
    )

    assert first.document_id == second.document_id


def test_the_same_bytes_in_two_scopes_produce_two_identities() -> None:
    """The scope is part of identity, not a decoration on it."""
    first = derive_document_identity(b"same", corpus_scope_id="scope-a")
    second = derive_document_identity(b"same", corpus_scope_id="scope-b")

    assert first.document_id != second.document_id
    # The version is the content's own digest and stays shared.
    assert first.version == second.version


def test_the_document_id_is_a_doc_prefixed_lowercase_hex_digest() -> None:
    identity = derive_document_identity(b"x", corpus_scope_id=_SCOPE)

    assert identity.document_id.startswith("doc_")
    assert identity.document_id[4:] == identity.document_id[4:].lower()
    int(identity.document_id[4:], 16)  # is hex


def test_a_blank_scope_id_is_refused() -> None:
    """An unconfigured trusted scope must not silently become one
    identity for every unconfigured deployment."""
    with pytest.raises(ValueError):
        derive_document_identity(b"x", corpus_scope_id="   ")


# ---------- doc type vocabulary ----------


def test_unknown_is_a_member_of_the_closed_doc_type_vocabulary() -> None:
    """The spec's "no reliable type is detected" value is a REAL member
    of the params table's type axis, not a sentinel outside it.

    It has to be: ``UNKNOWN_DOC_TYPE`` is what the durable accept records
    in ``upload_metadata_accepted.doc_type`` when the uploader named no
    override, so a value the closed vocabulary could not resolve would
    make the accept-time audit row unfilterable.
    """
    from fin.corpus.params import DOC_TYPES

    assert UNKNOWN_DOC_TYPE in DOC_TYPES
    assert UNKNOWN_DOC_TYPE == "unknown"


def test_unknown_resolves_on_the_params_table_deterministically() -> None:
    """``unknown`` is a real doc type, so every split that takes it must
    land on a fixed parameter row rather than raise."""
    from fin.corpus.params import lookup_chunk_params

    row = lookup_chunk_params("unknown", "body", 0)
    assert row is lookup_chunk_params("unknown", "body", 0)
    assert 0 < row.min_chars <= row.target_chars <= row.max_chars
