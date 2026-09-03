"""In-place full rebuild of frozen units + blocks (plan T13).

Spec「已定稿、不再作为开放问题」: 解析规则或参数表升版本时**就地全量重建**，
不做双版本并存. ADR 0038 (fin-side obligation): 任意时点都能用同一规则版本与
参数表版本从权威源重建全部推送内容. This module is that obligation, as one
command:

.. code-block:: console

    uv run python -m fin.corpus.rebuild rebuild \
        [--target data/corpus/blocks] \
        [--manifest data/corpus/dataset/manifest.json] \
        [--corpus-dir data/corpus]

    uv run python -m fin.corpus.rebuild check [...]

Input source
------------
The dataset manifest samples (``data/corpus/dataset/manifest.json``) —
the same authoritative source the T11 full-dataset assertion consumes
(``tests/eval/test_chunking_contract.py``): primary PDFs are parsed
with the T6 entry point, primary ``html_live`` documents come from the
committed extracted tree, derived samples from their exported bytes.
Every countable sample is re-parsed and re-split with the current
:mod:`fin.corpus.params` table and current parse rules.

Product area
------------
``<target>`` (default ``data/corpus/blocks`` — never the dataset source
directory): one JSON file per sample carrying the sample's frozen
units, both block levels, and the per-child push payloads, plus
``_rebuild_manifest.json`` recording the rule versions the products
were built with and the sha256 of every product file.

No version coexistence (hard constraint)
----------------------------------------
The rebuild builds the whole new product set in a **staging directory**
beside the target and publishes it with an **atomic swap**; only after
the swap does :func:`validate_no_mixed_versions` scan the published
area and refuse (named :class:`MixedVersionStateError`) any artifact
whose parse / params / normalization version is not the current
constant. At every instant the target directory therefore holds either
the complete previous version or the complete new one — never a
half-new half-old mixture.

Reentrancy
----------
A run that dies anywhere before the swap leaves the staging area
partial and the previous product untouched; the next run discards the
staging area and starts over. A run that dies between the two renames
of the swap leaves the previous product parked under
``<target>.trash``; the next run's swap reclaims it. Reruns always
converge to the same final state (proven by
``tests/corpus/test_rebuild.py``).

Determinism (T12)
-----------------
Serialization is fixed-order JSON (``sort_keys``), sample processing
order is sorted by ``sample_id``, and no clock / locale / randomness
enters: two consecutive rebuilds are byte-identical (asserted by this
module's own double-run comparison in the tests).

Version-change signal (for T18)
-------------------------------
:func:`detect_version_changes` compares the versions recorded in the
committed ``_rebuild_manifest.json`` against the current constants and
returns a :class:`VersionChangeSignal` with ``parse_changed`` /
``params_changed`` / ``changed`` flags — the detectable "版本变更已发生"
signal T18's content-invalidation trigger consumes. No committed
record (empty product area) counts as ``changed``: everything is
stale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fin.corpus.chunking import split_unit
from fin.corpus.dataset.build import _load_registry, _load_units, project_root
from fin.corpus.dataset.manifest import is_countable
from fin.corpus.errors import ManifestShaMismatchError
from fin.corpus.normalization import NORMALIZATION_RULE_VERSION
from fin.corpus.params import PARAMS_TABLE_VERSION
from fin.corpus.parsers import PARSE_RULE_VERSION, parse_document
from fin.corpus.parsers._common import ParseResult, ParsedUnit, build_unit

REBUILD_MANIFEST_NAME: Final[str] = "_rebuild_manifest.json"
STAGING_SUFFIX: Final[str] = ".staging"
TRASH_SUFFIX: Final[str] = ".trash"
REBUILD_RECORD_SCHEMA_VERSION: Final[str] = "1"


class MixedVersionStateError(RuntimeError):
    """The product area holds artifacts of more than one rule/params
    version (or any artifact whose versions are not the current
    constants). Hard-constraint violation: 新旧版本不得并存 — the offending
    area must be rebuilt (or restored) before it may be consumed."""

    code = "mixed_version_state"


def default_paths() -> tuple[Path, Path]:
    """``(target_dir, dataset_manifest_path)`` under the project root."""
    root = project_root()
    return (
        root / "data" / "corpus" / "blocks",
        root / "data" / "corpus" / "dataset" / "manifest.json",
    )


def _dump(payload: object) -> bytes:
    """Fixed-order JSON: the serialization T12's byte comparison relies on."""
    return (
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Version-change signal (T18 consumes this)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionChangeSignal:
    """Detectable "a version change has happened" state (plan T13
    acceptance item 5; consumed by T18's content-invalidation trigger).

    ``changed`` is True when either version moved, or when no committed
    record exists (an empty product area means everything is stale).
    """

    has_record: bool
    recorded_parse_rule_version: str | None
    recorded_params_table_version: str | None
    current_parse_rule_version: str
    current_params_table_version: str
    parse_changed: bool
    params_changed: bool

    @property
    def changed(self) -> bool:
        return self.parse_changed or self.params_changed


def detect_version_changes(target_dir: Path) -> VersionChangeSignal:
    """Compare the committed rebuild record's versions with the current
    constants. Missing record → ``has_record=False`` and
    ``changed=True``."""
    record_path = Path(target_dir) / REBUILD_MANIFEST_NAME
    if not record_path.is_file():
        return VersionChangeSignal(
            has_record=False,
            recorded_parse_rule_version=None,
            recorded_params_table_version=None,
            current_parse_rule_version=PARSE_RULE_VERSION,
            current_params_table_version=PARAMS_TABLE_VERSION,
            parse_changed=True,
            params_changed=True,
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    recorded_parse = str(record.get("parse_rule_version", ""))
    recorded_params = str(record.get("params_table_version", ""))
    return VersionChangeSignal(
        has_record=True,
        recorded_parse_rule_version=recorded_parse or None,
        recorded_params_table_version=recorded_params or None,
        current_parse_rule_version=PARSE_RULE_VERSION,
        current_params_table_version=PARAMS_TABLE_VERSION,
        parse_changed=recorded_parse != PARSE_RULE_VERSION,
        params_changed=recorded_params != PARAMS_TABLE_VERSION,
    )


# ---------------------------------------------------------------------------
# No-coexistence validator
# ---------------------------------------------------------------------------


def validate_no_mixed_versions(
    target_dir: Path,
    *,
    parse_rule_version: str | None = None,
    params_table_version: str | None = None,
) -> list[str]:
    """Scan every product file under ``target_dir`` and return the
    version-consistency violations (empty list = clean).

    A violation is any JSON object whose ``parse_rule_version`` /
    ``params_table_version`` differs from the current constants, or a
    product file missing from / drifting against the rebuild record's
    sha256 list. The rebuild entry point refuses to finish on a
    non-empty list (named :class:`MixedVersionStateError`), so the
    published final state can never carry two versions.
    """
    parse_v = parse_rule_version if parse_rule_version is not None else PARSE_RULE_VERSION
    params_v = (
        params_table_version
        if params_table_version is not None
        else PARAMS_TABLE_VERSION
    )
    target_dir = Path(target_dir)
    violations: list[str] = []
    record_hashes: dict[str, str] = {}

    record_path = target_dir / REBUILD_MANIFEST_NAME
    if not record_path.is_file():
        return [f"{target_dir}: no {REBUILD_MANIFEST_NAME} — not a committed product area"]

    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("parse_rule_version") != parse_v:
        violations.append(
            f"{REBUILD_MANIFEST_NAME}: parse_rule_version "
            f"{record.get('parse_rule_version')!r} != current {parse_v!r}"
        )
    if record.get("params_table_version") != params_v:
        violations.append(
            f"{REBUILD_MANIFEST_NAME}: params_table_version "
            f"{record.get('params_table_version')!r} != current {params_v!r}"
        )
    if record.get("normalization_rule_version") != NORMALIZATION_RULE_VERSION:
        violations.append(
            f"{REBUILD_MANIFEST_NAME}: normalization_rule_version "
            f"{record.get('normalization_rule_version')!r} != current "
            f"{NORMALIZATION_RULE_VERSION!r}"
        )
    samples = record.get("samples")
    if isinstance(samples, dict):
        record_hashes = {str(k): str(v) for k, v in samples.items()}
    else:
        violations.append(f"{REBUILD_MANIFEST_NAME}: 'samples' hash list missing")

    expected_files = {f"{sid}.json" for sid in record_hashes}
    actual_files = {p.name for p in target_dir.glob("*.json")} - {REBUILD_MANIFEST_NAME}

    for name in sorted(expected_files - actual_files):
        violations.append(f"{name}: product file listed in the record but missing")

    for path in sorted(target_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rel = path.name
        if rel == REBUILD_MANIFEST_NAME:
            continue
        _scan_node(payload, rel, parse_v, params_v, violations)
        if rel[: -len(".json")] in record_hashes:
            digest = _sha256_bytes(path.read_bytes())
            if digest != record_hashes[rel[: -len(".json")]]:
                violations.append(
                    f"{rel}: product file sha256 {digest} != recorded "
                    f"{record_hashes[rel[: -len('.json')]]}"
                )
        else:
            violations.append(f"{rel}: product file not listed in the rebuild record")
    return violations


def _scan_node(
    node: object, where: str, parse_v: str, params_v: str, violations: list[str]
) -> None:
    """Recursively flag any dict carrying a stale version field."""
    if isinstance(node, dict):
        if "parse_rule_version" in node and node["parse_rule_version"] != parse_v:
            violations.append(
                f"{where}: parse_rule_version {node['parse_rule_version']!r} "
                f"!= current {parse_v!r} (mixed-version state)"
            )
        if "params_table_version" in node and node["params_table_version"] != params_v:
            violations.append(
                f"{where}: params_table_version {node['params_table_version']!r} "
                f"!= current {params_v!r} (mixed-version state)"
            )
        for value in node.values():
            _scan_node(value, where, parse_v, params_v, violations)
    elif isinstance(node, list):
        for item in node:
            _scan_node(item, where, parse_v, params_v, violations)


# ---------------------------------------------------------------------------
# Rebuild pipeline
# ---------------------------------------------------------------------------


@dataclass
class RebuildReport:
    """What one rebuild did — carries the version-change signal too."""

    target_dir: Path
    parse_rule_version: str
    params_table_version: str
    normalization_rule_version: str
    parse_changed: bool
    params_changed: bool
    previous_parse_rule_version: str | None
    previous_params_table_version: str | None
    samples: int
    units: int
    parents: int
    children: int

    @property
    def changed(self) -> bool:
        return self.parse_changed or self.params_changed


def _block_record(block: object) -> dict:
    return {
        "kind": block.kind,
        "block_id": block.block_id,
        "text": block.text,
        "char_count": block.char_count,
        "ordinal": block.ordinal,
        "parent_ordinal": block.parent_ordinal,
        "parent_block_id": block.parent_block_id,
    }


def _unit_record(unit_chunking) -> dict:
    return {
        "locator": unit_chunking.locator.to_dict(),
        "unit_text": unit_chunking.unit_text,
        "fmt": unit_chunking.fmt,
        "doc_type": unit_chunking.doc_type,
        "section": unit_chunking.section,
        "density": unit_chunking.density,
        "source_sha256": unit_chunking.source_sha256,
        "parse_rule_version": unit_chunking.parse_rule_version,
        "params_table_version": unit_chunking.params_table_version,
        "normalization_rule_version": NORMALIZATION_RULE_VERSION,
        "parents": [_block_record(p) for p in unit_chunking.parents],
        "children": [_block_record(c) for c in unit_chunking.children],
        "payloads": [p.to_dict() for p in unit_chunking.payloads()],
    }


def _rebuild_sample(
    sample: dict,
    *,
    dataset_dir: Path,
    corpus_dir: Path | None,
    units_by_doc: dict | None,
) -> dict:
    """Parse + split ONE countable sample with the current rules.

    Returns ``{"sample_id", "bytes", "units", "parents", "children"}``
    where ``bytes`` is the serialized product file content.
    """
    sample_id = str(sample["sample_id"])
    doc_type = str(sample.get("doc_type", "mixed"))
    origin = str(sample.get("origin", ""))
    sha256 = str(sample["sha256"])

    parsed_units: list[ParsedUnit] = []
    if origin == "primary":
        kind = str(sample.get("source_kind", "pdf"))
        document_id = str(sample["document_id"])
        if kind == "pdf":
            if corpus_dir is None:
                raise ValueError(
                    f"{sample_id}: primary pdf sample but no corpus_dir given"
                )
            repo_root = Path(corpus_dir).parent.parent
            result: ParseResult = parse_document(
                repo_root / str(sample["path"]),
                fmt="pdf",
                document_id=document_id,
                version=sha256,
            )
            parsed_units = list(result.units)
        else:
            # html_live: the committed extracted tree is the frozen-unit
            # SSOT (same discipline as the T11 chunk contract / build).
            for unit in (units_by_doc or {}).get(document_id, []):
                if not unit.text.strip():
                    continue
                parsed_units.append(
                    build_unit(
                        fmt="html",
                        source_sha256=sha256,
                        document_id=document_id,
                        version=sha256,
                        unit=unit.unit,
                        text=unit.text,
                    )
                )
    else:
        derived_path = (
            Path(dataset_dir)
            / "documents"
            / str(sample["format"])
            / Path(str(sample["path"])).name
        )
        result = parse_document(
            derived_path.read_bytes(),
            fmt=str(sample["format"]),
            document_id=sample_id,
            version=sha256,
        )
        parsed_units = list(result.units)

    unit_records = []
    n_parents = 0
    n_children = 0
    for unit in parsed_units:
        chunking = split_unit(unit, doc_type=doc_type)
        n_parents += len(chunking.parents)
        n_children += len(chunking.children)
        unit_records.append(_unit_record(chunking))

    record = {
        "sample_id": sample_id,
        "format": str(sample.get("format", "")),
        "doc_type": doc_type,
        "origin": origin,
        "sha256": sha256,
        "parse_rule_version": PARSE_RULE_VERSION,
        "params_table_version": PARAMS_TABLE_VERSION,
        "normalization_rule_version": NORMALIZATION_RULE_VERSION,
        "units": unit_records,
    }
    return {
        "sample_id": sample_id,
        "bytes": _dump(record),
        "units": len(unit_records),
        "parents": n_parents,
        "children": n_children,
    }


def _atomic_swap(staging: Path, target: Path) -> None:
    """Publish ``staging`` as ``target`` without ever exposing a mixed
    state: the old product is parked aside with one rename, the staging
    area takes its place with a second rename. A crash between the two
    renames leaves the old product under ``<target>.trash``; the next
    run's swap reclaims it."""
    trash = target.with_name(target.name + TRASH_SUFFIX)
    if trash.exists():
        shutil.rmtree(trash)
    if target.exists():
        target.rename(trash)
    staging.rename(target)
    if trash.exists():
        shutil.rmtree(trash)


def rebuild(
    target_dir: Path,
    manifest_path: Path,
    *,
    corpus_dir: Path | None = None,
) -> RebuildReport:
    """Rebuild ALL frozen units + blocks of every countable dataset
    sample in place, with the current rule + params-table versions.

    Raises :class:`MixedVersionStateError` if the published product
    area fails the no-coexistence validation (cannot happen for a fresh
    staging area — the check is the hard constraint's witness, not its
    only guarantee) and :class:`ValueError` when the manifest carries
    no countable sample or a primary sample has no ``corpus_dir``.
    """
    target_dir = Path(target_dir)
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dataset_dir = manifest_path.parent

    samples = [
        sample
        for sample in manifest.get("documents", [])
        if is_countable(sample)[0]
    ]
    if not samples:
        raise ValueError(
            f"dataset manifest {manifest_path} carries no countable samples; "
            "nothing to rebuild"
        )

    primary_samples = [s for s in samples if s.get("origin") == "primary"]
    units_by_doc: dict | None = None
    if primary_samples:
        if corpus_dir is None:
            raise ValueError(
                "manifest contains primary samples; pass corpus_dir "
                "(the data/corpus directory holding manifest.json + extracted/)"
            )
        registry, _ = _load_registry(corpus_dir)
        units_by_doc = _load_units(corpus_dir / "extracted", registry)
        for sample in primary_samples:
            document_id = str(sample.get("document_id", ""))
            if document_id not in registry:
                raise ManifestShaMismatchError(
                    f"primary sample {sample['sample_id']!r}: document_id "
                    f"{document_id!r} not in the corpus registry"
                )
            if str(sample["sha256"]) != str(registry[document_id]["version"]):
                raise ManifestShaMismatchError(
                    f"primary sample {sample['sample_id']!r}: sha256 "
                    f"{sample['sha256']} != corpus registry version "
                    f"{registry[document_id]['version']}"
                )

    signal = detect_version_changes(target_dir)

    # Build the full new product set beside the target; the previous
    # complete product stays published until the atomic swap.
    staging = target_dir.with_name(target_dir.name + STAGING_SUFFIX)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    totals = {"units": 0, "parents": 0, "children": 0}
    file_hashes: dict[str, str] = {}
    for sample in sorted(samples, key=lambda s: str(s["sample_id"])):
        built = _rebuild_sample(
            sample,
            dataset_dir=dataset_dir,
            corpus_dir=corpus_dir,
            units_by_doc=units_by_doc,
        )
        filename = f"{built['sample_id']}.json"
        (staging / filename).write_bytes(built["bytes"])
        file_hashes[built["sample_id"]] = _sha256_bytes(built["bytes"])
        totals["units"] += built["units"]
        totals["parents"] += built["parents"]
        totals["children"] += built["children"]

    (staging / REBUILD_MANIFEST_NAME).write_bytes(
        _dump(
            {
                "schema_version": REBUILD_RECORD_SCHEMA_VERSION,
                "parse_rule_version": PARSE_RULE_VERSION,
                "params_table_version": PARAMS_TABLE_VERSION,
                "normalization_rule_version": NORMALIZATION_RULE_VERSION,
                "dataset_manifest": str(manifest_path),
                "samples": dict(sorted(file_hashes.items())),
            }
        )
    )

    # Pre-swap witness: a fresh staging area must already be consistent.
    violations = validate_no_mixed_versions(staging)
    if violations:
        raise MixedVersionStateError(
            "staging product area failed the no-coexistence validation: "
            + "; ".join(violations[:10])
        )

    _atomic_swap(staging, target_dir)

    # Post-swap hard constraint: the published final state carries no
    # version other than the current one — ever.
    violations = validate_no_mixed_versions(target_dir)
    if violations:
        raise MixedVersionStateError(
            "rebuilt product area failed the no-coexistence validation: "
            + "; ".join(violations[:10])
        )

    return RebuildReport(
        target_dir=target_dir,
        parse_rule_version=PARSE_RULE_VERSION,
        params_table_version=PARAMS_TABLE_VERSION,
        normalization_rule_version=NORMALIZATION_RULE_VERSION,
        parse_changed=signal.parse_changed,
        params_changed=signal.params_changed,
        previous_parse_rule_version=signal.recorded_parse_rule_version,
        previous_params_table_version=signal.recorded_params_table_version,
        samples=len(samples),
        units=totals["units"],
        parents=totals["parents"],
        children=totals["children"],
    )


# ---------------------------------------------------------------------------
# CLI: `uv run python -m fin.corpus.rebuild {rebuild,check}`
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fin.corpus.rebuild",
        description=(
            "In-place full rebuild of frozen units + blocks on a "
            "parse-rule / params-table version change (plan T13; "
            "spec: 就地全量重建，不做双版本并存)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        (
            "rebuild",
            "rebuild every countable sample into the product area "
            "(staging + atomic swap + no-coexistence validation)",
        ),
        (
            "check",
            "detect version change (previous vs. current constants) and "
            "validate the product area for mixed-version states",
        ),
    ]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "--target",
            default=None,
            help="product directory (default: data/corpus/blocks)",
        )
        cmd.add_argument(
            "--manifest",
            default=None,
            help="dataset manifest path "
            "(default: data/corpus/dataset/manifest.json)",
        )
        cmd.add_argument(
            "--corpus-dir",
            default=None,
            help="corpus directory for primary samples "
            "(default: data/corpus)",
        )
    args = parser.parse_args(argv)

    d_target, d_manifest = default_paths()
    d_corpus = d_target.parent
    target = Path(args.target) if args.target else d_target
    manifest_path = Path(args.manifest) if args.manifest else d_manifest
    corpus_dir = Path(args.corpus_dir) if args.corpus_dir else d_corpus

    try:
        if args.command == "rebuild":
            report = rebuild(target, manifest_path, corpus_dir=corpus_dir)
            print(f"rebuild: target={report.target_dir}")
            print(
                f"versions: parse={report.parse_rule_version} "
                f"params={report.params_table_version} "
                f"normalization={report.normalization_rule_version}"
            )
            print(
                f"samples={report.samples} units={report.units} "
                f"parents={report.parents} children={report.children}"
            )
            print(
                f"version change: parse_changed={report.parse_changed} "
                f"params_changed={report.params_changed} "
                f"changed={report.changed}"
            )
            return 0

        signal = detect_version_changes(target)
        print(
            f"versions: recorded=(parse={signal.recorded_parse_rule_version}, "
            f"params={signal.recorded_params_table_version}) "
            f"current=(parse={signal.current_parse_rule_version}, "
            f"params={signal.current_params_table_version}) "
            f"has_record={signal.has_record}"
        )
        print(
            f"version change: parse_changed={signal.parse_changed} "
            f"params_changed={signal.params_changed} changed={signal.changed}"
        )
        violations = validate_no_mixed_versions(target)
        print(f"violations={len(violations)}")
        for v in violations:
            print(f"  PROBLEM {v}")
        return 0 if not violations else 1
    except MixedVersionStateError as exc:
        print(f"MIXED VERSION STATE: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
