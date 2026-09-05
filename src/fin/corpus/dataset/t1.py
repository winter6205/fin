"""T1 public-source eval corpus (plan T1 / spec Focus 1).

T1 is the **provenance-only** slice that pairs the live retrieve wave
(spec ``live-retrieve-eval-wave`` Focus 1) with the scene coverage
defined by ``docs/wayfinder/next-wave-workspace-kb-agent-map.md`` T1:

* Scene 1 — onboarding (GitLab Handbook CC BY-SA 4.0)
* Scene 2 — presales (GitLab Handbook CC BY-SA 4.0)
* Scene 3 — troubleshooting (Kubernetes docs CC BY 4.0)
* Scene 4 — compliance (EUR-Lex GDPR 2016/679)
* Scene 5 — policy (CAC/NPC PIPL 中华人民共和国个人信息保护法)
* Scene 6 — engineering (this repo ``docs/adr/`` / ``specs/``)

Counting discipline (spec SC6 / SC7)
------------------------------------

The manifest at ``data/corpus/dataset/t1_manifest.json`` lists every
sample + every per-scene eval query. A sample is **countable toward
B1** iff it:

* declares ``scene`` in :data:`T1_SCENES`;
* is **not** flagged ``synthetic``;
* carries a non-empty ``source.url`` and ``source.license``;
* carries a non-empty ``fetched_at`` (ISO date string);
* carries a 64-hex ``sha256`` of the frozen text file under
  :data:`T1_TEXT_DIR_REL`.

Samples failing any of these are surfaced in the verify report and
must NOT contribute to the per-scene query set. The counting gate is
:func:`is_t1_countable`; the verify gate is :func:`verify_t1_manifest`.

Per-scene queries
-----------------

Each scene that has frozen text MUST carry at least
:data:`T1_MIN_QUERY_PER_SCENE` human-checked question. The
``evidence_span`` field is a contiguous substring of the frozen text
file referenced by the sample, so the verify gate can prove the
question is answerable from the committed text.

Storage layout
--------------

The frozen text lives under ``data/corpus/dataset/t1/`` (one file per
sample, plain markdown). The manifest lives at
``data/corpus/dataset/t1_manifest.json``. Both are committed to git;
no network is required at runtime or at test time.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

T1_SCHEMA_VERSION = "1.0.0"
T1_DATASET_ID = "fin-corpus-t1-public-v1"

# Mirror the scene list from the map (1–6). Scene 7 (经营分析) is out of scope.
T1_SCENES: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
T1_MIN_QUERY_PER_SCENE = 1

# Repo-relative path of the frozen-text directory. Used by the CLI
# (so ``uv run python -m fin.corpus.dataset verify-t1`` finds the
# committed files without flags).
T1_TEXT_DIR_REL = "data/corpus/dataset/t1"
T1_MANIFEST_REL = "data/corpus/dataset/t1_manifest.json"

# Gap code recorded in ``known_gaps`` when a scene has no countable
# sample (so verify does not invent questions for empty scenes).
T1_GAP_SCENE_MISSING_FROZEN_TEXT = "t1_scene_missing_frozen_text"
T1_GAP_LICENSE_OR_SOURCE_INCOMPLETE = "t1_sample_license_or_source_incomplete"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------
# Counting gate
# --------------------------------------------------------------------------


def is_t1_countable(sample: dict) -> tuple[bool, str]:
    """Decide whether one T1 sample counts toward the B1 floors.

    Returns ``(countable, reason)``; ``reason`` is empty when countable.
    The reason strings are stable so verify / report tooling can group
    on them.
    """
    try:
        scene = int(sample.get("scene", 0))
    except (TypeError, ValueError):
        return False, "unknown_scene"
    if scene not in T1_SCENES:
        return False, "unknown_scene"
    if sample.get("synthetic") is True:
        return False, "synthetic"
    source = sample.get("source") or {}
    if not isinstance(source, dict):
        return False, "missing_source"
    if not str(source.get("url", "")).strip():
        return False, "missing_source"
    if not str(source.get("license", "")).strip():
        return False, "missing_license"
    if not str(sample.get("fetched_at", "")).strip():
        return False, "missing_fetched_at"
    sha = str(sample.get("sha256", ""))
    if not _SHA256_RE.match(sha):
        return False, "missing_sha256"
    if not str(sample.get("path", "")).strip():
        return False, "missing_path"
    if not str(sample.get("document_id", "")).strip():
        return False, "missing_document_id"
    return True, ""


# --------------------------------------------------------------------------
# Dataclasses (lightweight: callers can also pass plain dicts)
# --------------------------------------------------------------------------


@dataclass
class T1Sample:
    sample_id: str
    scene: int
    source_url: str
    license: str
    fetched_at: str
    path: str
    document_id: str
    title: str = ""
    notes: str = ""
    synthetic: bool = False
    synthetic_origin: str = ""


@dataclass
class T1Query:
    query_id: str
    sample_id: str
    scene: int
    question: str
    evidence_span: str
    query_class: str = "narrative"


@dataclass
class T1VerifyReport:
    ok: bool
    problems: list[str] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    per_scene_counts: dict[str, int] = field(default_factory=dict)

    @property
    def problem_count(self) -> int:
        return len(self.problems)

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)


# --------------------------------------------------------------------------
# Manifest load / build
# --------------------------------------------------------------------------


def load_t1_manifest(path: Path) -> dict:
    """Load the committed T1 manifest (read-only)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_t1_manifest_from_dir(
    *,
    samples: list[T1Sample],
    queries: list[T1Query],
    text_dir: Path,
    known_gaps: list[dict] | None = None,
) -> dict:
    """Build an in-memory T1 manifest.

    Reads the frozen text file referenced by each sample, computes the
    sha256, and assembles a dict with the same shape as the committed
    manifest. ``known_gaps`` defaults to an empty list. The caller is
    responsible for writing the manifest to disk.
    """
    text_dir = Path(text_dir)
    sample_records: list[dict] = []
    for s in samples:
        path = text_dir / s.path
        if not path.is_file():
            raise FileNotFoundError(
                f"T1 sample {s.sample_id!r}: frozen text missing at {path}"
            )
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        rec: dict = {
            "sample_id": s.sample_id,
            "scene": int(s.scene),
            "source": {
                "url": s.source_url,
                "license": s.license,
            },
            "fetched_at": s.fetched_at,
            "sha256": sha,
            "path": s.path,
            "document_id": s.document_id,
        }
        if s.title:
            rec["title"] = s.title
        if s.notes:
            rec["notes"] = s.notes
        if s.synthetic:
            rec["synthetic"] = True
            if s.synthetic_origin:
                rec["synthetic_origin"] = s.synthetic_origin
        sample_records.append(rec)
    query_records: list[dict] = []
    for q in queries:
        query_records.append(
            {
                "query_id": q.query_id,
                "sample_id": q.sample_id,
                "scene": int(q.scene),
                "question": q.question,
                "query_class": q.query_class,
                "evidence_span": q.evidence_span,
            }
        )
    counts, excluded = _compute_counts(sample_records)
    return {
        "schema_version": T1_SCHEMA_VERSION,
        "dataset_id": T1_DATASET_ID,
        "samples": sample_records,
        "queries": query_records,
        "counts": counts,
        "excluded": excluded,
        "known_gaps": list(known_gaps or []),
    }


def _compute_counts(samples: list[dict]) -> tuple[dict, list[dict]]:
    by_scene: dict[str, int] = {str(s): 0 for s in T1_SCENES}
    total = 0
    excluded: list[dict] = []
    for s in samples:
        ok, reason = is_t1_countable(s)
        if not ok:
            excluded.append({"sample_id": s.get("sample_id", "<unnamed>"), "reason": reason})
            continue
        total += 1
        scene_key = str(int(s["scene"]))
        by_scene[scene_key] = by_scene.get(scene_key, 0) + 1
    counts = {"total": total, "by_scene": by_scene}
    return counts, excluded


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------


def verify_t1_manifest(manifest: dict, *, text_dir: Path) -> T1VerifyReport:
    """Verify a T1 manifest against on-disk frozen text.

    Checks:

    * schema_version + dataset_id;
    * every countable sample's sha256 matches the on-disk frozen text;
    * every countable sample carries the required provenance fields
      (delegated to :func:`is_t1_countable`);
    * every query's ``sample_id`` points at a known sample and the
      ``evidence_span`` is a substring of the referenced frozen text;
    * each scene with at least one countable sample has
      ``>= T1_MIN_QUERY_PER_SCENE`` query.

    Returns :class:`T1VerifyReport`. ``report.ok`` is True iff no
    problem surfaced.
    """
    problems: list[str] = []
    mismatches: list[dict] = []

    if manifest.get("schema_version") != T1_SCHEMA_VERSION:
        problems.append(
            f"schema_version {manifest.get('schema_version')!r} != "
            f"{T1_SCHEMA_VERSION!r}"
        )
    if manifest.get("dataset_id") != T1_DATASET_ID:
        problems.append(
            f"dataset_id {manifest.get('dataset_id')!r} != {T1_DATASET_ID!r}"
        )

    samples = manifest.get("samples") or []
    queries = manifest.get("queries") or []

    if not isinstance(samples, list):
        problems.append("'samples' must be a list")
        return T1VerifyReport(ok=False, problems=problems)

    text_dir = Path(text_dir)
    seen_ids: set[str] = set()
    per_scene_counts: dict[str, int] = {str(s): 0 for s in T1_SCENES}
    excluded: list[dict] = []
    sample_by_id: dict[str, dict] = {}
    for s in samples:
        if not isinstance(s, dict):
            problems.append("samples entry is not an object")
            continue
        sid = str(s.get("sample_id", ""))
        if not sid:
            problems.append("sample missing sample_id")
            continue
        if sid in seen_ids:
            problems.append(f"{sid}: duplicate sample_id")
            continue
        seen_ids.add(sid)
        sample_by_id[sid] = s
        ok, reason = is_t1_countable(s)
        if not ok:
            excluded.append({"sample_id": sid, "reason": reason})
            continue
        scene_key = str(int(s["scene"]))
        per_scene_counts[scene_key] = per_scene_counts.get(scene_key, 0) + 1
        # Verify on-disk sha256.
        path = text_dir / s["path"]
        if not path.is_file():
            problems.append(
                f"{sid}: frozen text missing at {path} "
                f"(gap code {T1_GAP_SCENE_MISSING_FROZEN_TEXT})"
            )
            continue
        disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if disk_sha != s["sha256"]:
            mismatches.append(
                {
                    "sample_id": sid,
                    "kind": "sha256",
                    "committed": s["sha256"],
                    "on_disk": disk_sha,
                }
            )

    # Query gates.
    if not isinstance(queries, list):
        problems.append("'queries' must be a list")
    else:
        per_scene_queries: dict[str, int] = {str(s): 0 for s in T1_SCENES}
        seen_qids: set[str] = set()
        for q in queries:
            if not isinstance(q, dict):
                problems.append("queries entry is not an object")
                continue
            qid = str(q.get("query_id", ""))
            if not qid:
                problems.append("query missing query_id")
                continue
            if qid in seen_qids:
                problems.append(f"{qid}: duplicate query_id")
                continue
            seen_qids.add(qid)
            sid = str(q.get("sample_id", ""))
            if sid not in sample_by_id:
                problems.append(f"{qid}: references unknown sample {sid!r}")
                continue
            sample = sample_by_id[sid]
            ok, _ = is_t1_countable(sample)
            if not ok:
                problems.append(
                    f"{qid}: sample {sid} is not countable; cannot ask questions on it"
                )
                continue
            scene_key = str(int(q.get("scene", sample["scene"])))
            per_scene_queries[scene_key] = per_scene_queries.get(scene_key, 0) + 1
            # Verify evidence_span is a substring of the frozen text.
            path = text_dir / sample["path"]
            text = path.read_text(encoding="utf-8")
            span = str(q.get("evidence_span", ""))
            if span and span not in text:
                problems.append(
                    f"{qid}: evidence_span not found in frozen text {sample['path']}"
                )
        # Scene coverage: each scene with countable samples must have
        # at least one query; missing scenes go into known_gaps but
        # also surface as a problem so the B1-pass path is loud.
        for scene_str, n in per_scene_counts.items():
            if n <= 0:
                continue
            if per_scene_queries.get(scene_str, 0) < T1_MIN_QUERY_PER_SCENE:
                problems.append(
                    f"scene {scene_str}: {n} countable samples but "
                    f"{per_scene_queries.get(scene_str, 0)} queries "
                    f"(need >= {T1_MIN_QUERY_PER_SCENE})"
                )

    ok = not problems and not mismatches
    return T1VerifyReport(
        ok=ok,
        problems=problems,
        mismatches=mismatches,
        excluded=excluded,
        per_scene_counts=per_scene_counts,
    )
