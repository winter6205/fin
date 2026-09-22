"""Search-mode test pins that must NOT come from ``src/fin``.

Shared by the four host lanes that assert the retired-spelling contract
(``test_search_mode_before_port``, ``test_assigned_mode_reaches_port``,
``test_named_failure_tier``, ``test_search_mode_usage_copy_keys``), so the
list has one home the lanes cannot drift between.

The retired letters are spelled HERE because nothing in the product keeps
them: :mod:`fin.retrieve.tiers` holds only the lasting closed set, so a
letter imported from ``src/fin`` would mean some seam still records the old
vocabulary — which is exactly what these tests exist to disprove. The pin
therefore lives on the test side of the boundary.
"""

from __future__ import annotations

RETIRED_LETTERS: tuple[str, ...] = ("A", "B", "C", "D")
