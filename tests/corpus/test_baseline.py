"""Tests for the cross-repo baseline-section verifier (plan T4 / spec B6).

The contract (spec ``specs/corpus-retrieval.md`` §B6, SC41):

* Both specs' "跨仓对齐基线" section bodies must be byte-identical.
* Section extraction rule: start AFTER the ``## 跨仓对齐基线`` heading
  line (the heading itself is NOT part of the hashed text), stop before
  the next ``## `` heading line, strip leading/trailing blank lines.
* The command accepts two file paths at runtime (never hard-coded),
  computes sha256 of each extracted section, exits 0 when equal; on
  mismatch exits nonzero AND prints both hashes (not just "mismatch").
* Missing file / missing section are named failures (nonzero exit +
  explicit stderr message), not silent skips.
* The command reads no network and starts no service processes — the
  module is stdlib-only (``hashlib`` / ``pathlib`` / ``argparse``).

The entry command is ``uv run python -m fin.corpus.baseline <fin_spec>
<retrieval_spec>``; ``main(argv)`` is the testable in-process form.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from fin.corpus.baseline import (
    BaselineFileMissingError,
    BaselineSectionMissingError,
    extract_baseline_section,
    main,
    section_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIN_SPEC = REPO_ROOT / "specs" / "corpus-retrieval.md"


def _expected_section_sha(text: str) -> str:
    """Independent sha256 of the B6 section, computed in the test itself.

    Deliberately re-implements the slicing rule here (not by calling the
    production helper's own hash) so a regression in the extraction rule
    cannot pass by comparing the function against itself.
    """
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("## 跨仓对齐基线")
    )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    body = lines[start + 1 : end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return hashlib.sha256("\n".join(body).encode("utf-8")).hexdigest()


# ----- extraction rule (B6, verbatim) -----


def test_extract_section_excludes_heading_and_stops_at_next_h2() -> None:
    doc = (
        "preamble\n"
        "\n"
        "## 跨仓对齐基线（共享章节，两仓逐字一致）\n"
        "\n"
        "### B1 数字口径\n"
        "body line\n"
        "\n"
        "## Boundaries\n"
        "- Does\n"
    )
    section = extract_baseline_section(doc)
    # Heading line itself excluded; body starts after it.
    assert section.splitlines()[0] == "### B1 数字口径"
    # Truncated at the next ``## `` heading line.
    assert "Boundaries" not in section
    assert section.splitlines()[-1] == "body line"
    # Leading/trailing blank lines stripped.
    assert not section.startswith("\n")
    assert not section.endswith("\n")


def test_extract_section_tolerates_h3_inside_section() -> None:
    doc = "## 跨仓对齐基线\n\n### B1\nalpha\n\n### B2\nbeta\n\n## Next\n"
    section = extract_baseline_section(doc)
    assert "alpha" in section
    assert "beta" in section
    assert "Next" not in section


def test_missing_heading_raises_named_error() -> None:
    with pytest.raises(BaselineSectionMissingError):
        extract_baseline_section("## Some Other Section\nno baseline here\n")


# ----- command-level acceptance (real spec as the positive fixture) -----


def test_real_spec_section_matches_identical_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real fin spec section vs. its untouched copy: exit 0."""
    other = tmp_path / "retrieval-stack.md"
    other.write_text(FIN_SPEC.read_text(encoding="utf-8"), encoding="utf-8")

    code = main([str(FIN_SPEC), str(other)])

    assert code == 0
    out = capsys.readouterr().out
    expected = _expected_section_sha(FIN_SPEC.read_text(encoding="utf-8"))
    assert expected in out


def test_changed_copy_mismatch_prints_both_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One deliberately changed character: nonzero exit, both hashes out."""
    original = FIN_SPEC.read_text(encoding="utf-8")
    section = extract_baseline_section(original)
    assert "并" in section
    tampered_section = section.replace("并", "幷", 1)
    assert tampered_section != section

    copy_path = tmp_path / "retrieval-stack.md"
    copy_path.write_text(original.replace(section, tampered_section), encoding="utf-8")

    code = main([str(FIN_SPEC), str(copy_path)])

    assert code != 0
    out = capsys.readouterr().out
    fin_hash = _expected_section_sha(original)
    copy_hash = hashlib.sha256(
        extract_baseline_section(copy_path.read_text(encoding="utf-8")).encode("utf-8")
    )
    # BOTH hashes are printed, not just "mismatch".
    assert fin_hash in out
    assert copy_hash.hexdigest() in out


def test_missing_side_file_is_named_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Command: nonzero exit + explicit error, not a silent skip/traceback."""
    missing = tmp_path / "does-not-exist.md"

    code = main([str(FIN_SPEC), str(missing)])

    assert code == 2
    captured = capsys.readouterr()
    assert "does-not-exist.md" in captured.err
    assert "not found" in captured.err
    # The typed error is the programmatic surface of the same failure.
    with pytest.raises(BaselineFileMissingError):
        section_sha256(missing)


def test_section_missing_is_named_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Command: nonzero exit + explicit error when the heading is absent."""
    no_section = tmp_path / "no-baseline.md"
    no_section.write_text("## Only Heading\nbody\n", encoding="utf-8")

    code = main([str(FIN_SPEC), str(no_section)])

    assert code == 3
    captured = capsys.readouterr()
    assert "跨仓对齐基线" in captured.err
    # The typed error is the programmatic surface of the same failure.
    with pytest.raises(BaselineSectionMissingError):
        section_sha256(no_section)


# ----- the one-command entry, as an operator would run it -----


def test_module_entry_subprocess_identical_copy(tmp_path: Path) -> None:
    """``python -m fin.corpus.baseline <a> <b>`` exits 0 on equal sections."""
    other = tmp_path / "retrieval-stack.md"
    other.write_text(FIN_SPEC.read_text(encoding="utf-8"), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "fin.corpus.baseline", str(FIN_SPEC), str(other)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert _expected_section_sha(FIN_SPEC.read_text(encoding="utf-8")) in proc.stdout


def test_module_entry_subprocess_mismatch(tmp_path: Path) -> None:
    original = FIN_SPEC.read_text(encoding="utf-8")
    section = extract_baseline_section(original)
    tampered = original.replace(section, section.replace("并", "幷", 1), 1)
    copy_path = tmp_path / "retrieval-stack.md"
    copy_path.write_text(tampered, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "fin.corpus.baseline", str(FIN_SPEC), str(copy_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode != 0
    fin_hash = _expected_section_sha(original)
    assert fin_hash in proc.stdout
