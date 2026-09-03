"""Eval drivers (T22+).

``fin.eval`` is the eval-path context: it orchestrates ``fin.corpus``
(dataset), ``fin.retrieve`` (capability configuration and the retrieval
seam) and ``fin.host`` (the citation gate) to produce quantified
reports. Nothing under ``fin.corpus`` / ``fin.retrieve`` / ``fin.host``
imports it back, so the dependency runs one way only.

It is also the only place capability switches are allowed to appear
(ADR 0039): the product call path carries none, and
``fin.retrieve.capabilities.scan_product_call_path`` deliberately does
not scan this package.
"""

from __future__ import annotations

__all__: list[str] = []
