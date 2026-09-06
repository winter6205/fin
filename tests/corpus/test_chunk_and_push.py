from pathlib import Path

import pytest

from fin.corpus.chunk_and_push import push_t1_corpus
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.push_port import PushServiceUnavailableError

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data/corpus/dataset/t1_manifest.json"
TEXT = ROOT / "data/corpus/dataset/t1"


def test_t1_push_fake_is_idempotent_and_metadata_rich():
    port = FakePushPort()
    first = push_t1_corpus(manifest_path=MANIFEST, text_dir=TEXT, port=port)
    second = push_t1_corpus(manifest_path=MANIFEST, text_dir=TEXT, port=port)
    assert not first["failures"]
    assert first["blocks"] > 0
    assert first["written"] == first["blocks"]
    assert second["written"] == 0
    assert second["duplicates"] == first["blocks"]
    stored = next(iter(port._by_key.values()))
    assert {"scene", "license", "url"} <= stored["metadata"].keys()


def test_t1_push_reports_named_backend_failure():
    class Down:
        def push(self, records):
            raise PushServiceUnavailableError("down")

    report = push_t1_corpus(manifest_path=MANIFEST, text_dir=TEXT, port=Down())
    assert report["written"] == 0
    assert report["failures"]
    assert report["failures"][0]["code"] == "push_service_unavailable"


def test_t1_push_marks_unexpected_explicitly_in_failures(tmp_path: Path):
    """A push port that surfaces an unexpected exception (e.g. a programming
    bug — :class:`KeyError`) must NOT be silently swept under a
    ``t1_push_failed`` code. The chunk-and-push catch is narrowed to
    the expected family: a typed push error OR a
    :class:`ValueError` / :class:`OSError` (the same way the CLI's other
    command paths name them). Anything else is recorded with the
    ``unexpected_error`` code AND the exception class name so an
    operator can act on it instead of seeing a generic push failure."""

    class _Boom:
        def push(self, records):
            raise KeyError("simulated programming bug")

    report = push_t1_corpus(manifest_path=MANIFEST, text_dir=TEXT, port=_Boom())
    assert report["written"] == 0
    assert report["failures"], report
    failure = report["failures"][0]
    assert failure["code"] == "unexpected_error"
    assert "KeyError" in failure["error"], failure
    assert "sample_id" in failure


def test_cli_exits_nonzero_when_push_surfaces_unexpected_error():
    """SC21 / §B4: a push command that records an ``unexpected_error``
    failure must NOT exit 0 — the operator must see a non-zero exit
    code and a JSON report whose ``failures`` names the error class so
    they can debug. Today the broad ``except Exception`` would have
    silently swallowed the ``KeyError`` as ``t1_push_failed`` and the
    CLI would still exit 1; this test pins both the code and the
    non-zero exit invariant on the new ``unexpected_error`` path."""

    import io
    import json as _json

    from fin.corpus import chunk_and_push
    from fin.retrieve import fake_push_port as fake_port_module

    class _Boom:
        def push(self, records):
            raise KeyError("simulated programming bug")

    monkeypatch = pytest.MonkeyPatch()
    buf = io.StringIO()
    try:
        monkeypatch.setattr(fake_port_module, "FakePushPort", _Boom)
        monkeypatch.setattr("sys.stdout", buf)
        exit_code = chunk_and_push.main(
            [
                "--adapter",
                "fake",
                "--manifest",
                str(MANIFEST),
                "--text-dir",
                str(TEXT),
            ]
        )
    finally:
        monkeypatch.undo()

    assert exit_code != 0, buf.getvalue()
    report = _json.loads(buf.getvalue())
    assert report["failures"], report
    assert report["failures"][0]["code"] == "unexpected_error"
    assert "KeyError" in report["failures"][0]["error"]


def test_cli_requires_explicit_opt_in():
    from fin.corpus.chunk_and_push import main
    with pytest.raises(SystemExit):
        main(["--manifest", str(MANIFEST), "--text-dir", str(TEXT)])
