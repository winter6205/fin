"""Smoke test for the fin v1 package skeleton.

Confirms that the three capability-slice packages (`corpus`, `retrieve`,
`host`) are importable. T1 only establishes the importable skeleton; later
tickets fill these packages with concrete capability.
"""

from __future__ import annotations

import fin
import fin.corpus
import fin.host
import fin.retrieve


def test_fin_package_imports() -> None:
    assert fin.__name__ == "fin"


def test_fin_corpus_package_imports() -> None:
    assert fin.corpus.__name__ == "fin.corpus"


def test_fin_retrieve_package_imports() -> None:
    assert fin.retrieve.__name__ == "fin.retrieve"


def test_fin_host_package_imports() -> None:
    assert fin.host.__name__ == "fin.host"
