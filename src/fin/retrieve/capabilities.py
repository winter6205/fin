"""Capability-key snapshot and the product-path static check (T19, ADR 0039).

Retrieval capabilities are on by default. fin's product call path carries no
parameter that turns one off; the switches exist only on the eval path, where
ablation needs them. This module is what makes that statement checkable rather
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

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CAPABILITY_COUNT = 8

_OFF_PREFIXES = ("disable", "no", "off", "without", "exclude")
_OFF_SUFFIXES = ("enabled", "disabled", "off", "on")

SPEC_ENUMERATION_ROW_LABEL = "消融的 8 个能力项"

#: The product call path, relative to the project root. Everything fin runs to
#: answer a user question and everything that configures it: the port contract,
#: the tool wrapper, the MCP adapter that talks to the retrieval service, the
#: host tool that binds them, and the factory that builds the product adapter.
#: The eval path is deliberately absent — that is where switches are allowed.
PRODUCT_CALL_PATH_FILES: tuple[str, ...] = (
    "src/fin/retrieve/port.py",
    "src/fin/retrieve/tool.py",
    "src/fin/retrieve/mcp_adapter.py",
    "src/fin/host/graph.py",
    "src/fin/host/compile.py",
)


def project_root() -> Path:
    """Repository root (``src/fin/retrieve/capabilities.py`` → three up)."""
    return Path(__file__).resolve().parents[3]


CAPABILITY_SNAPSHOT_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "retrieval"
    / "capability_keys.json"
)


def published_spec_path() -> Path:
    """The spec carrying the retrieval service's published enumeration."""
    return project_root() / "specs" / "corpus-retrieval.md"


# ----- typed failures -----


class CapabilityError(Exception):
    """Base for capability-snapshot and static-check failures.

    ``code`` is stable and assertable; subclasses narrow it.
    """

    code = "capability_error"


class CapabilitySnapshotMissingError(CapabilityError):
    """The snapshot file is not there at all."""

    code = "capability_snapshot_missing"


class CapabilitySnapshotMalformedError(CapabilityError):
    """The snapshot is unreadable, wrongly shaped, blank, or duplicated."""

    code = "capability_snapshot_malformed"


class CapabilitySnapshotCountError(CapabilityError):
    """The snapshot holds a number of entries other than eight."""

    code = "capability_snapshot_count_mismatch"


class CapabilityEnumerationRowMissingError(CapabilityError):
    """The spec no longer publishes the enumeration row the snapshot syncs to."""

    code = "capability_enumeration_row_missing"


class CapabilityEnumerationDriftError(CapabilityError):
    """Snapshot and published enumeration disagree; both sides are printed."""

    code = "capability_enumeration_drift"


class CapabilityConfigurationError(CapabilityError):
    """An eval capability configuration is incomplete or names an unknown key."""

    code = "capability_configuration_invalid"


class CapabilityConfigurationIncompleteError(CapabilityConfigurationError):
    """The configuration does not decide every snapshot key."""

    code = "capability_configuration_incomplete"


class UnknownCapabilityError(CapabilityConfigurationError):
    """The configuration names a key that is not in the snapshot."""

    code = "unknown_capability"


class ProductCallPathTargetMissingError(CapabilityError):
    """A file the static check is supposed to scan is not there.

    Scanning nothing would report zero hits, which reads exactly like a clean
    product path. This turns that silence into a failure.
    """

    code = "product_call_path_target_missing"


# ----- K1: the snapshot -----


def _validate_keys(keys: Sequence[Any], *, origin: str) -> tuple[str, ...]:
    for entry in keys:
        if not isinstance(entry, str) or not entry.strip():
            raise CapabilitySnapshotMalformedError(
                f"{origin}: capability key is not a non-blank string: {entry!r}"
            )
        if entry.strip() != entry:
            raise CapabilitySnapshotMalformedError(
                f"{origin}: capability key has surrounding whitespace: {entry!r}"
            )
    duplicates = sorted({key for key in keys if list(keys).count(key) > 1})
    if duplicates:
        raise CapabilitySnapshotMalformedError(
            f"{origin}: duplicate capability keys: {duplicates}"
        )
    if len(keys) != CAPABILITY_COUNT:
        raise CapabilitySnapshotCountError(
            f"{origin}: capability snapshot must hold exactly "
            f"{CAPABILITY_COUNT} entries, found {len(keys)}: {list(keys)}"
        )
    return tuple(keys)


def load_capability_keys(path: Path | None = None) -> tuple[str, ...]:
    """Return the 8 snapshot keys in published order.

    Raises a named :class:`CapabilityError` when the file is absent,
    unreadable, wrongly shaped, blank, duplicated, or not exactly eight long.
    """
    snapshot_path = Path(path) if path is not None else CAPABILITY_SNAPSHOT_PATH
    try:
        raw = snapshot_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CapabilitySnapshotMissingError(
            f"capability snapshot not found: {snapshot_path}"
        ) from exc
    except OSError as exc:
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot unreadable: {snapshot_path}: {exc}"
        ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot is not JSON: {snapshot_path}: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot must be a JSON object: {snapshot_path}"
        )
    keys = document.get("keys")
    if not isinstance(keys, list):
        raise CapabilitySnapshotMalformedError(
            f"capability snapshot has no `keys` list: {snapshot_path}"
        )
    return _validate_keys(keys, origin=str(snapshot_path))


_BACKTICKED = re.compile(r"`([^`]+)`")


def published_capability_keys(spec_path: Path | None = None) -> tuple[str, ...]:
    """Return the enumeration the retrieval service publishes, read from §B4.

    The published row is a Markdown table row whose first cell is
    :data:`SPEC_ENUMERATION_ROW_LABEL`; the keys are its backticked tokens, in
    order. Reading the enumeration back out of the spec (rather than restating
    it in code) is what makes the sync point single.
    """
    path = Path(spec_path) if spec_path is not None else published_spec_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CapabilityEnumerationRowMissingError(
            f"cannot read the published enumeration from {path}: {exc}"
        ) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == SPEC_ENUMERATION_ROW_LABEL:
            keys = _BACKTICKED.findall("|".join(cells[1:]))
            return _validate_keys(keys, origin=f"{path} 「{cells[0]}」")
    raise CapabilityEnumerationRowMissingError(
        f"{path} publishes no 「{SPEC_ENUMERATION_ROW_LABEL}」 table row; "
        "the capability snapshot has nothing to sync against"
    )


def assert_snapshot_matches_published(
    *,
    snapshot_path: Path | None = None,
    spec_path: Path | None = None,
) -> tuple[str, ...]:
    """The sync point: snapshot must equal the published enumeration.

    Returns the agreed keys, or raises :class:`CapabilityEnumerationDriftError`
    printing both sides — "they differ" alone would not say which one moved.
    """
    snapshot = load_capability_keys(path=snapshot_path)
    published = published_capability_keys(spec_path=spec_path)
    if snapshot != published:
        raise CapabilityEnumerationDriftError(
            "capability enumeration drift: "
            f"snapshot={list(snapshot)} published={list(published)}"
        )
    return snapshot


def all_on_configuration(
    keys: Sequence[str] | None = None,
) -> dict[str, bool]:
    """The eval-path "all on" configuration: every snapshot key set to ``True``.

    This is the baseline the ablation matrix (T22) varies and the configuration
    SC26 compares the product path against.
    """
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    return {key: True for key in resolved}


def validate_capability_configuration(
    configuration: Mapping[str, Any],
    *,
    keys: Sequence[str] | None = None,
) -> dict[str, bool]:
    """Return ``configuration`` normalized to booleans over the 8 snapshot keys.

    An eval configuration has to decide every capability explicitly: silently
    defaulting a missing key would make an ablation run's meaning depend on
    what the caller forgot.
    """
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    unknown = sorted(set(configuration) - set(resolved))
    if unknown:
        raise UnknownCapabilityError(
            f"capability configuration names keys outside the snapshot: {unknown}"
        )
    missing = [key for key in resolved if key not in configuration]
    if missing:
        raise CapabilityConfigurationIncompleteError(
            f"capability configuration does not decide: {missing}"
        )
    return {key: bool(configuration[key]) for key in resolved}


# ----- K2: the capability-off predicate -----


@dataclass(frozen=True)
class SwitchHit:
    """One parameter key that counts as capability-off under K2."""

    key: str
    form: str
    capability: str
    location: str = ""


def _compiled_forms(keys: Sequence[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    alternation = "|".join(re.escape(key) for key in keys)
    prefix = re.compile(
        rf"^(?:{'|'.join(_OFF_PREFIXES)})_(?P<cap>{alternation})$"
    )
    suffix = re.compile(
        rf"^(?P<cap>{alternation})_(?:{'|'.join(_OFF_SUFFIXES)})$"
    )
    return prefix, suffix


def classify_capability_key(
    key: str,
    *,
    value_is_falsy: bool,
    keys: Sequence[str] | None = None,
) -> SwitchHit | None:
    """Apply K2 to a single key. ``None`` means "not a capability-off key".

    ``value_is_falsy`` decides forms 2 and 3; form 1 (the prefix form) is a hit
    whatever the value, because naming a parameter ``disable_rerank`` already
    puts the switch on the call path.
    """
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    prefix_re, suffix_re = _compiled_forms(resolved)
    match = prefix_re.match(key)
    if match:
        return SwitchHit(key=key, form="prefix", capability=match.group("cap"))
    if not value_is_falsy:
        return None
    match = suffix_re.match(key)
    if match:
        return SwitchHit(key=key, form="suffix", capability=match.group("cap"))
    if key in resolved:
        return SwitchHit(key=key, form="bare", capability=key)
    return None


def capability_switch_hits(
    params: Mapping[str, Any],
    *,
    keys: Sequence[str] | None = None,
) -> tuple[SwitchHit, ...]:
    """Apply K2 to a runtime parameter mapping."""
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    hits = []
    for key, value in params.items():
        hit = classify_capability_key(
            key, value_is_falsy=not bool(value), keys=resolved
        )
        if hit is not None:
            hits.append(hit)
    return tuple(hits)


# ----- K2 applied statically -----


_UNDECIDED = object()


def _literal_value(node: ast.AST | None) -> Any:
    """Return the literal ``node`` denotes, or ``_UNDECIDED``."""
    if node is None:
        return _UNDECIDED
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _UNDECIDED


def _is_falsy(node: ast.AST | None) -> bool:
    """Falsy, or not provably truthy.

    An unreadable value is treated as falsy so a switch-shaped key with a
    computed value is reported instead of waved through.
    """
    value = _literal_value(node)
    if value is _UNDECIDED:
        return True
    return not bool(value)


def _iter_candidate_keys(tree: ast.AST):
    """Yield ``(key, value_node, lineno)`` for every parameter-ish key."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg is not None:
                    yield keyword.arg, keyword.value, keyword.lineno
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = list(args.posonlyargs) + list(args.args)
            padded_defaults: list[ast.expr | None] = [None] * (
                len(positional) - len(args.defaults)
            ) + list(args.defaults)
            for arg, default in zip(positional, padded_defaults):
                yield arg.arg, default, arg.lineno
            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                yield arg.arg, default, arg.lineno
        elif isinstance(node, ast.Dict):
            for key_node, value_node in zip(node.keys, node.values):
                if isinstance(key_node, ast.Constant) and isinstance(
                    key_node.value, str
                ):
                    yield key_node.value, value_node, key_node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                ):
                    yield target.slice.value, node.value, node.lineno


def scan_source_for_capability_switches(
    source: str,
    *,
    filename: str,
    keys: Sequence[str] | None = None,
) -> tuple[SwitchHit, ...]:
    """Apply K2 statically to one Python source text."""
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    tree = ast.parse(source, filename=filename)
    hits: list[SwitchHit] = []
    for key, value_node, lineno in _iter_candidate_keys(tree):
        hit = classify_capability_key(
            key, value_is_falsy=_is_falsy(value_node), keys=resolved
        )
        if hit is not None:
            hits.append(
                SwitchHit(
                    key=hit.key,
                    form=hit.form,
                    capability=hit.capability,
                    location=f"{filename}:{lineno}",
                )
            )
    return tuple(hits)


def scan_product_call_path(
    root: Path | None = None,
    *,
    targets: Iterable[str] | None = None,
    keys: Sequence[str] | None = None,
) -> tuple[SwitchHit, ...]:
    """Run the K2 static check over the product call path.

    The acceptance assertion is that this returns an empty tuple. A scan target
    that cannot be read is a named failure rather than a skipped file.
    """
    base = Path(root) if root is not None else project_root()
    resolved_targets = (
        tuple(targets) if targets is not None else PRODUCT_CALL_PATH_FILES
    )
    resolved = tuple(keys) if keys is not None else load_capability_keys()
    hits: list[SwitchHit] = []
    for relative in resolved_targets:
        path = base / relative
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProductCallPathTargetMissingError(
                f"product call path target unreadable: {path}: {exc}"
            ) from exc
        hits.extend(
            scan_source_for_capability_switches(
                source, filename=relative, keys=resolved
            )
        )
    return tuple(hits)


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
