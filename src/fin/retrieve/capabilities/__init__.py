"""Capability-key snapshot and the product-path static check (T19, ADR 0039).

Retrieval capabilities are on by default. fin's product call path carries no
parameter that turns one off; the switches exist only on the eval path, where
ablation needs them. This package is what makes that statement checkable rather
than aspirational, and it holds the two halves of spec §E item 25 verbatim:

**K1 — the key snapshot.** ``data/retrieval/capability_keys.json`` is fin's
local copy of an enumeration the retrieval service owns (§B4). It must exist,
be non-empty, and hold exactly :data:`CAPABILITY_COUNT` entries; more, fewer,
or empty all fail. :func:`assert_snapshot_matches_published` is the single
explicit sync point: it re-reads the published row out of
``specs/corpus-retrieval.md`` and refuses to agree when the two sides differ,
so an enumeration change on the retrieval side lands as a named drift failure
here instead of quietly leaving the snapshot stale.

**K2 — the capability-off predicate.** A parameter key counts as
"capability-off" when it matches one of three forms (``<X>`` is any snapshot
key):

1. prefix — ``^(disable|no|off|without|exclude)_<X>$``, any value;
2. suffix — ``^<X>_(enabled|disabled|off|on)$`` with a falsy value;
3. bare — the key is exactly ``<X>`` with a falsy value.

:func:`capability_switch_hits` applies the predicate to a runtime mapping;
:func:`scan_source_for_capability_switches` applies the same predicate
statically to source text (keyword arguments, function parameters, dict
literal keys, and ``d["k"] = v`` assignments), and
:func:`scan_product_call_path` runs it over :data:`PRODUCT_CALL_PATH_FILES`.
The acceptance assertion is that the scan returns nothing.

A value the static pass cannot read as a literal is *undecided*, and undecided
is reported. The check cannot prove ``retrieve(rerank=flag)`` leaves the
capability on, and "cannot prove" is the one answer that must not silently
become "fine".
"""

from fin.retrieve.capabilities._paths import (
    CAPABILITY_COUNT,
    CAPABILITY_SNAPSHOT_PATH,
    SPEC_ENUMERATION_ROW_LABEL,
    project_root,
    published_spec_path,
)
from fin.retrieve.capabilities.errors import (
    CapabilityConfigurationError,
    CapabilityConfigurationIncompleteError,
    CapabilityEnumerationDriftError,
    CapabilityEnumerationRowMissingError,
    CapabilityError,
    CapabilitySnapshotCountError,
    CapabilitySnapshotMalformedError,
    CapabilitySnapshotMissingError,
    ProductCallPathTargetMissingError,
    UnknownCapabilityError,
)
from fin.retrieve.capabilities.scanner import (
    PRODUCT_CALL_PATH_FILES,
    SwitchHit,
    capability_switch_hits,
    classify_capability_key,
    scan_product_call_path,
    scan_source_for_capability_switches,
)
from fin.retrieve.capabilities.snapshot import (
    all_on_configuration,
    assert_snapshot_matches_published,
    load_capability_keys,
    published_capability_keys,
    validate_capability_configuration,
)

__all__ = [
    "CAPABILITY_COUNT",
    "CAPABILITY_SNAPSHOT_PATH",
    "PRODUCT_CALL_PATH_FILES",
    "SPEC_ENUMERATION_ROW_LABEL",
    "CapabilityConfigurationError",
    "CapabilityConfigurationIncompleteError",
    "CapabilityEnumerationDriftError",
    "CapabilityEnumerationRowMissingError",
    "CapabilityError",
    "CapabilitySnapshotCountError",
    "CapabilitySnapshotMalformedError",
    "CapabilitySnapshotMissingError",
    "ProductCallPathTargetMissingError",
    "SwitchHit",
    "UnknownCapabilityError",
    "all_on_configuration",
    "assert_snapshot_matches_published",
    "capability_switch_hits",
    "classify_capability_key",
    "load_capability_keys",
    "project_root",
    "published_capability_keys",
    "published_spec_path",
    "scan_product_call_path",
    "scan_source_for_capability_switches",
    "validate_capability_configuration",
]
