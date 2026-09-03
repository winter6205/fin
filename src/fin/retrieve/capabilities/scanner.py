"""K2: capability-off predicate and static switch scanner (T19, ADR 0039)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from fin.retrieve.capabilities._paths import project_root
from fin.retrieve.capabilities.errors import ProductCallPathTargetMissingError
from fin.retrieve.capabilities.snapshot import load_capability_keys

_OFF_PREFIXES = ("disable", "no", "off", "without", "exclude")
_OFF_SUFFIXES = ("enabled", "disabled", "off", "on")

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
