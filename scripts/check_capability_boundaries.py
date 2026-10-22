#!/usr/bin/env python3
"""Capability boundary checks.

The two hard-boundary checks in ``AGENTS.md`` rule 14 run first and keep
their current meaning. The capability checks then assert that a capability
package owns its own behaviour without reaching into the transport layer,
the application boundary or the graph composition root, and that the
capability-to-capability edges stay inside the recorded set.

The capability checks read the SOURCE imports of a capability package. They
do not resolve the module graph: a package that imports a shared host
module may still pull the composition root in transitively, and that is a
separate question from whether the capability names it.

An import is judged by what it BINDS, not by how it is spelled.
``from fin.host import http`` reaches the transport just like
``import fin.host.http`` does, a bare ``import fin.host.compile`` binds the
graph-building entry points as well, the whole host package re-exports
the composition root, and every name of a from-import list is judged,
not only the first one.

A recorded edge is a deliberate dependency, not an accident. Adding one is
a boundary change: record it here and in the reliability map at the same
time. The set below is the state the P0 reorganisation produced; every
edge in it is real and has a reason.

Exit code 0 means every check passed. Exit code 1 means at least one
violation was found and each one is printed.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]

# The capability packages the boundary rule governs. The application
# boundary is not one of them: it owns composition and lifetime, so it
# reaches the graph composition root on purpose (plan T3).
HOST_CAPABILITIES = ("attachment", "evidence", "grounded", "question", "session", "task")
WEB_CAPABILITIES = ("clarification", "evidence", "task", "trace")

# A capability package must not import these modules at all. Matched by
# prefix, so ``fin.host.application.composition`` is caught too.
HOST_FORBIDDEN_MODULES = ("fin.host.http", "fin.host.application", "fin.host.graph")

# A capability package must not import the composition root's graph-building
# entry points. Its plain configuration constants stay importable.
HOST_FORBIDDEN_COMPILE_NAMES = (
    "compile_graph",
    "compile_for_runtime",
    "compile_for_runtime_dev",
    "compile_runtime_handle",
)

# The host submodules a ``from fin.host import X`` names. ``X`` is then the
# module ``fin.host.X`` and binds everything that module holds.
HOST_KNOWN_SUBMODULES = (
    "http",
    "application",
    "graph",
    "compile",
) + HOST_CAPABILITIES

# The recorded capability-to-capability edges. Each entry is
# (source capability, target capability, why the dependency exists).
HOST_ALLOWED_EDGES = (
    (
        "attachment",
        "evidence",
        "the visual observation reader returns the evidence capability's "
        "visual observation type",
    ),
    (
        "evidence",
        "attachment",
        "the structured evidence drain worker reuses the attachment "
        "capability's periodic drain worker base",
    ),
    (
        "evidence",
        "grounded",
        "evidence admission and the evidence contract name the grounded "
        "capability's citation gate types",
    ),
    (
        "grounded",
        "question",
        "track and retrieval-mode decision name the question capability's "
        "JEV choice types",
    ),
    (
        "question",
        "task",
        "document choice admission names the task capability's runtime "
        "state types",
    ),
)

# The recorded web capability-to-capability edges.
WEB_ALLOWED_EDGES = (
    ("task", "trace", "the task panel renders the trace panel view"),
    (
        "clarification",
        "task",
        "the document choice surface names the task capability's runtime "
        "client",
    ),
)

# The one known corpus-to-host line: a function-local delayed import of
# fin.host.fin_home. It is a known pending item, not an approved exception.
KNOWN_CORPUS_HOST_LINES = ("src/fin/corpus/catalog.py:77",)


class _Import(NamedTuple):
    """One import statement of a capability module.

    ``module`` is the imported module path, already resolved for relative
    imports. ``names`` holds the imported names and is empty when the
    statement binds the whole module.
    """

    lineno: int
    module: str
    names: tuple[str, ...]


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _check_corpus_isolation(violations: list[str], root: Path) -> None:
    """corpus must not import host or eval, except the one known line."""
    pattern = re.compile(r"^\s*(from|import)\s+fin\.(host|eval)\b")
    for path in _python_files(root / "src" / "fin" / "corpus"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if not pattern.match(line):
                continue
            marker = f"{_relative(path, root)}:{lineno}"
            if marker in KNOWN_CORPUS_HOST_LINES:
                continue
            violations.append(
                f"corpus isolation: {marker} imports fin.host/fin.eval "
                f"({line.strip()})"
            )


def _check_no_eval_dependency(violations: list[str], root: Path) -> None:
    """host and retrieve must not import eval."""
    pattern = re.compile(r"^\s*(from|import)\s+fin\.eval\b")
    for base in (root / "src" / "fin" / "host", root / "src" / "fin" / "retrieve"):
        for path in _python_files(base):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.match(line):
                    violations.append(
                        f"eval isolation: {_relative(path, root)}:{lineno} imports "
                        f"fin.eval"
                    )


def _package_of(path: Path, src: Path) -> str:
    """The dotted package path of the module living at ``path``."""
    parts = path.relative_to(src).with_suffix("").parts[:-1]
    return ".".join(parts)


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """Resolve a relative import against the importing module's package."""
    parts = package.split(".") if package else []
    if level > 1:
        parts = parts[: max(0, len(parts) - (level - 1))]
    if module:
        parts = [*parts, *module.split(".")]
    return ".".join(parts)


def _host_imports(path: Path, src: Path) -> list[_Import]:
    """Return the imports of one capability module.

    Raises ``SyntaxError`` when the file does not parse; the caller
    reports that as a violation rather than skipping the file.
    """
    tree = ast.parse(path.read_text())
    package = _package_of(path, src)
    found: list[_Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(_Import(node.lineno, alias.name, ()))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                module = _resolve_relative(package, node.level, node.module)
            else:
                module = node.module or ""
            found.append(
                _Import(node.lineno, module, tuple(alias.name for alias in node.names))
            )
    return sorted(found, key=lambda item: (item.lineno, item.module, item.names))


def _bindings(imp: _Import) -> list[tuple[str, str]]:
    """The (module, name) pairs an import binds.

    The name is empty when the statement binds the whole module.
    ``from fin.host import http`` binds the module ``fin.host.http``,
    so it reads the same as ``import fin.host.http``.
    """
    if not imp.names:
        return [(imp.module, "")]
    if imp.module == "fin.host":
        bound: list[tuple[str, str]] = []
        for name in imp.names:
            if name in HOST_KNOWN_SUBMODULES:
                bound.append((f"fin.host.{name}", ""))
            else:
                bound.append((imp.module, name))
        return bound
    if "*" in imp.names:
        return [(imp.module, "")]
    return [(imp.module, name) for name in imp.names]


def _binding_violations(
    capability: str,
    path: Path,
    root: Path,
    lineno: int,
    bound: list[tuple[str, str]],
) -> list[str]:
    """The boundary violations the bindings of one import cause."""
    found: list[str] = []
    for module, name in bound:
        for forbidden in HOST_FORBIDDEN_MODULES:
            if module == forbidden or module.startswith(f"{forbidden}."):
                found.append(
                    f"capability {capability} reaches the transport, the "
                    f"application boundary or the graph composition root: "
                    f"{_relative(path, root)}:{lineno} imports {module}"
                )
                break
        if module == "fin.host" and name == "":
            # The whole host package is bound: it re-exports the graph
            # composition root, so the root is reachable through it.
            found.append(
                f"capability {capability} reaches the transport, the "
                f"application boundary or the graph composition root: "
                f"{_relative(path, root)}:{lineno} imports fin.host"
            )
            continue
        if module == "fin.host" and name in HOST_FORBIDDEN_COMPILE_NAMES:
            # A name the host package re-exports from the composition root.
            found.append(
                f"capability {capability} reaches the graph composition "
                f"root: {_relative(path, root)}:{lineno} imports {name}"
            )
            continue
        if module != "fin.host.compile":
            continue
        if name == "":
            found.append(
                f"capability {capability} reaches the graph composition "
                f"root: {_relative(path, root)}:{lineno} imports "
                f"fin.host.compile"
            )
        elif name in HOST_FORBIDDEN_COMPILE_NAMES:
            found.append(
                f"capability {capability} reaches the graph composition "
                f"root: {_relative(path, root)}:{lineno} imports {name}"
            )
    return found


def _edge_target(module: str, capability: str) -> str | None:
    """The other capability a module path reaches, if it reaches one."""
    for other in HOST_CAPABILITIES:
        if other == capability:
            continue
        if module == f"fin.host.{other}" or module.startswith(f"fin.host.{other}."):
            return other
    return None


def _check_host_capabilities(violations: list[str], root: Path) -> None:
    observed: set[tuple[str, str]] = set()
    src = root / "src"
    host = src / "fin" / "host"
    for capability in HOST_CAPABILITIES:
        package = host / capability
        if not package.is_dir():
            violations.append(f"host capability package missing: {package}")
            continue
        for path in _python_files(package):
            try:
                imports = _host_imports(path, src)
            except SyntaxError as exc:
                violations.append(
                    f"capability {capability} package could not be parsed: "
                    f"{_relative(path, root)}: {exc}"
                )
                continue
            for imp in imports:
                bound = _bindings(imp)
                violations.extend(
                    _binding_violations(capability, path, root, imp.lineno, bound)
                )
                for module, _ in bound:
                    other = _edge_target(module, capability)
                    if other is not None:
                        observed.add((capability, other))
    allowed = {(source, target) for source, target, _ in HOST_ALLOWED_EDGES}
    for edge in sorted(observed - allowed):
        violations.append(
            f"unrecorded capability edge: {edge[0]} -> {edge[1]}. Record it in "
            f"scripts/check_capability_boundaries.py and in the reliability map."
        )
    for edge in sorted(allowed - observed):
        violations.append(
            f"recorded capability edge is gone: {edge[0]} -> {edge[1]}. "
            f"Remove it from the recorded set or restore the dependency."
        )


def _web_imports(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    text = path.read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        match = re.search(
            r"""(?:from|import)\s+['"]([^'"]+)['"]""", line
        )
        if match is None:
            continue
        found.append((lineno, match.group(1)))
    return found


def _web_resolve(importer: Path, specifier: str) -> Path | None:
    """Resolve a relative web specifier to an absolute path."""
    if not specifier.startswith("."):
        return None
    target = Path(
        os.path.normpath(str(importer.parent / specifier))
    ).resolve()
    return target


def _web_capability_of(target: Path, root: Path) -> str | None:
    """The capability a resolved web path belongs to, if any."""
    try:
        relative = target.relative_to((root / "web" / "src").resolve())
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) >= 2 and parts[0] == "capabilities":
        return parts[1]
    return None


def _check_web_capabilities(violations: list[str], root: Path) -> None:
    observed: set[tuple[str, str]] = set()
    web = root / "web" / "src" / "capabilities"
    shared_pages = (root / "web" / "src" / "pages").resolve()
    for capability in WEB_CAPABILITIES:
        package = web / capability
        if not package.is_dir():
            violations.append(f"web capability directory missing: {package}")
            continue
        for path in sorted(
            p for p in package.rglob("*") if p.suffix in {".ts", ".tsx"}
        ):
            for lineno, specifier in _web_imports(path):
                target = _web_resolve(path, specifier)
                if target is None:
                    continue
                try:
                    target.relative_to(shared_pages)
                except ValueError:
                    pass
                else:
                    violations.append(
                        f"web capability {capability} reaches the page directory: "
                        f"{_relative(path, root)}:{lineno} imports {specifier}"
                    )
                other = _web_capability_of(target, root)
                if other is not None and other != capability:
                    observed.add((capability, other))
    allowed = {(source, target) for source, target, _ in WEB_ALLOWED_EDGES}
    for edge in sorted(observed - allowed):
        violations.append(
            f"unrecorded web capability edge: {edge[0]} -> {edge[1]}. Record it "
            f"in scripts/check_capability_boundaries.py and in the reliability map."
        )
    for edge in sorted(allowed - observed):
        violations.append(
            f"recorded web capability edge is gone: {edge[0]} -> {edge[1]}. "
            f"Remove it from the recorded set or restore the dependency."
        )


def check_all(root: Path) -> list[str]:
    """Run every check against ``root`` and return the violations found."""
    violations: list[str] = []
    _check_corpus_isolation(violations, root)
    _check_no_eval_dependency(violations, root)
    _check_host_capabilities(violations, root)
    _check_web_capabilities(violations, root)
    return violations


def main() -> int:
    violations = check_all(ROOT)
    if violations:
        print("capability boundary violations:")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("capability boundaries: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
