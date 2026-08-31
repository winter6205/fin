"""Eval dataset for the corpus capability slice (plan T10).

This package builds and validates the multi-format evaluation dataset
described by spec ``specs/corpus-retrieval.md`` §B1 (数据集规模 /
语料来源与许可) and SC27–SC30:

* **Tier 1 (primary, citable)** — the frozen 10-Q PDFs under
  ``data/corpus/pdf/``. These are the only samples that produce
  locator / citation evidence.
* **Tier 3 (derived)** — md / txt / html / docx / xlsx / pptx files
  exported by the deterministic exporters in
  :mod:`fin.corpus.dataset.exporters` from the *verbatim* frozen unit
  text of the tier-1 extracted tree (``data/corpus/extracted/``).
  Derived samples carry ``origin=derived`` and ``derived_from``
  (native document_id + version sha256); citation identity always
  points back to the native document — no second locator contract is
  created. No synthetic or unsourced sample is ever counted.

The dataset lives under ``data/corpus/dataset/`` and is rebuilt by one
command::

    uv run python -m fin.corpus.dataset build      # (re)build + write manifest
    uv run python -m fin.corpus.dataset verify     # rebuild in memory, compare hashes
    uv run python -m fin.corpus.dataset report     # print counts summary

Rebuilds are byte-identical (fixed templates, fixed byte order, no
timestamps), so the sample manifest hashes recorded in
``manifest.json`` match on every re-run (SC30: 重跑不一致样本数 = 0).

The default ``uv run pytest -q`` suite never reads the dataset bytes;
build logic and manifest validation are tested with small tmp-path
fixtures (``tests/corpus/test_dataset_build.py``), and the real
dataset contract is checked by the opt-in eval-marked test
(``tests/eval/test_dataset_contract.py``).
"""

from __future__ import annotations

from fin.corpus.dataset.build import (
    build_dataset,
    verify_dataset,
    DatasetVerifyReport,
)
from fin.corpus.dataset.manifest import (
    validate_manifest,
    compute_counts,
    is_countable,
    ValidationReport,
)

__all__ = [
    "build_dataset",
    "verify_dataset",
    "DatasetVerifyReport",
    "validate_manifest",
    "compute_counts",
    "is_countable",
    "ValidationReport",
]
