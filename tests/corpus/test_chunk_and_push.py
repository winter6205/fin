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


def test_cli_requires_explicit_opt_in():
    from fin.corpus.chunk_and_push import main
    with pytest.raises(SystemExit):
        main(["--manifest", str(MANIFEST), "--text-dir", str(TEXT)])
