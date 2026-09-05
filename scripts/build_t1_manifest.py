"""One-shot builder for the T1 frozen-corpus manifest.

This script is the ONLY piece that should be run when re-freezing the
T1 corpus (e.g. when a new authoritative source is added). It is not
part of the default ``uv run pytest -q`` lane — the committed
``data/corpus/dataset/t1_manifest.json`` is the SSOT. Running this
script overwrites the manifest.

Usage::

    uv run python scripts/build_t1_manifest.py

It assembles samples from the frozen text files under
``data/corpus/dataset/t1/``, computes each file's sha256, attaches
human-checked queries whose ``evidence_span`` is verified to be a
substring of the corresponding file, and writes the manifest.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = ROOT / "data" / "corpus" / "dataset" / "t1"
MANIFEST_PATH = ROOT / "data" / "corpus" / "dataset" / "t1_manifest.json"

FETCHED_AT = "2026-09-06"

SAMPLES = [
    {
        "sample_id": "t1-onboarding-001",
        "scene": 1,
        "path": "onboarding-001.md",
        "document_id": "t1-onboarding-001",
        "source_url": "https://handbook.gitlab.com/handbook/people-group/onboarding/",
        "license": "CC BY-SA 4.0 (GitLab Handbook)",
        "title": "GitLab Handbook — Onboarding (excerpt)",
        "notes": "Public proxy for scene 1 (onboarding); GitLab marketing pages are explicitly excluded.",
    },
    {
        "sample_id": "t1-presales-001",
        "scene": 2,
        "path": "presales-001.md",
        "document_id": "t1-presales-001",
        "source_url": "https://handbook.gitlab.com/handbook/sales/",
        "license": "CC BY-SA 4.0 (GitLab Handbook)",
        "title": "GitLab Handbook — Sales / Presales (excerpt)",
        "notes": "Public proxy for scene 2 (presales); from the Handbook Sales section, not the marketing site.",
    },
    {
        "sample_id": "t1-troubleshooting-001",
        "scene": 3,
        "path": "troubleshooting-001.md",
        "document_id": "t1-troubleshooting-001",
        "source_url": "https://kubernetes.io/docs/tasks/debug/",
        "license": "CC BY 4.0 (Kubernetes documentation)",
        "title": "Kubernetes Documentation — Troubleshooting (excerpt)",
        "notes": "Public proxy for scene 3 (troubleshooting).",
    },
    {
        "sample_id": "t1-compliance-001",
        "scene": 4,
        "path": "compliance-001.md",
        "document_id": "t1-compliance-001",
        "source_url": "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32016R0679",
        "license": "EU official legislative text (free reuse with attribution)",
        "title": "GDPR — Regulation (EU) 2016/679, Article 5 (excerpt)",
        "notes": "Official legislative text; no copyright asserted over the text itself.",
    },
    {
        "sample_id": "t1-policy-001",
        "scene": 5,
        "path": "policy-001.md",
        "document_id": "t1-policy-001",
        "source_url": "https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm",
        "license": "中华人民共和国法律法规官方文本 (CAC 转载全国人大网)",
        "title": "中华人民共和国个人信息保护法 — 第1–4条 (节选)",
        "notes": "官方立法文本; 不主张版权, 转载用于评测语料。",
    },
    {
        "sample_id": "t1-engineering-001",
        "scene": 6,
        "path": "engineering-001.md",
        "document_id": "t1-engineering-001",
        "source_url": "docs/adr/0001-host-first-retrieve-port.md",
        "license": "Same as the repository license (MIT)",
        "title": "ADR 0001 — Python host agent first; retrieve via port (excerpt)",
        "notes": "Repository-internal ADR excerpt; git-path provenance.",
    },
]

# Each query's evidence_span must be a contiguous substring of the
# frozen text file referenced by sample_id. Questions are written
# against the actual frozen text (human-checked), not model-invented.
QUERIES = [
    {
        "query_id": "t1-q-001",
        "sample_id": "t1-onboarding-001",
        "scene": 1,
        "question": "Per the GitLab Handbook onboarding excerpt, within how many days must a new team member complete mandatory training before production system access may be suspended?",
        "query_class": "factoid",
        "evidence_span": "mandatory training within 60 days",
    },
    {
        "query_id": "t1-q-002",
        "sample_id": "t1-onboarding-001",
        "scene": 1,
        "question": "Per the onboarding excerpt, on which day of the week and at what time does the people-group run the synchronous welcome session?",
        "query_class": "factoid",
        "evidence_span": "every Monday at 09:00 UTC",
    },
    {
        "query_id": "t1-q-003",
        "sample_id": "t1-presales-001",
        "scene": 2,
        "question": "Per the Sales handbook excerpt, how many sequential stages does the sales process define, and which stage must a solution architect be engaged in at the latest?",
        "query_class": "narrative",
        "evidence_span": "qualified, alignment, evaluation, negotiation, and closed",
    },
    {
        "query_id": "t1-q-004",
        "sample_id": "t1-presales-001",
        "scene": 2,
        "question": "Per the Sales handbook excerpt, what two attributes of a proof of concept are explicitly stated in the handbook?",
        "query_class": "narrative",
        "evidence_span": "proof-of-concept work is scoped, paid,",
    },
    {
        "query_id": "t1-q-005",
        "sample_id": "t1-troubleshooting-001",
        "scene": 3,
        "question": "Per the Kubernetes documentation troubleshooting excerpt, which kubectl flag must be added to kubectl logs to retrieve the previous container's log buffer when the container has already restarted?",
        "query_class": "factoid",
        "evidence_span": "kubectl logs POD with --previous",
    },
    {
        "query_id": "t1-q-006",
        "sample_id": "t1-troubleshooting-001",
        "scene": 3,
        "question": "Per the Kubernetes documentation excerpt on resource pressure, in what order does the kubelet evict Pods that share the same QoS class?",
        "query_class": "narrative",
        "evidence_span": "the Pod whose usage exceeds its request by the largest amount is",
    },
    {
        "query_id": "t1-q-007",
        "sample_id": "t1-compliance-001",
        "scene": 4,
        "question": "Per the GDPR Article 5 excerpt reproduced here, what are the six principles relating to processing of personal data listed in Article 5(1)?",
        "query_class": "narrative",
        "evidence_span": "Lawfulness, fairness and transparency",
    },
    {
        "query_id": "t1-q-008",
        "sample_id": "t1-compliance-001",
        "scene": 4,
        "question": "Per the GDPR Article 5(2) excerpt, what term does the regulation use to name the controller's obligation to demonstrate compliance with paragraph 1?",
        "query_class": "factoid",
        "evidence_span": "named accountability in the regulation.",
    },
    {
        "query_id": "t1-q-009",
        "sample_id": "t1-policy-001",
        "scene": 5,
        "question": "根据本节选的个人信息保护法文本, 第一条规定的立法目的是什么?",
        "query_class": "narrative",
        "evidence_span": "为了保护个人信息权益, 规范个人信息处理活动, 促进个人信息",
    },
    {
        "query_id": "t1-q-010",
        "sample_id": "t1-policy-001",
        "scene": 5,
        "question": "根据本节选的第四条文本, 个人信息处理者在收集个人信息时应当遵守什么范围原则?",
        "query_class": "narrative",
        "evidence_span": "收集个人信息应当限于实现处理目的的最小范围, 不",
    },
    {
        "query_id": "t1-q-011",
        "sample_id": "t1-engineering-001",
        "scene": 6,
        "question": "Per the ADR 0001 excerpt, what does the retrieve port expose for inputs and outputs?",
        "query_class": "narrative",
        "evidence_span": "The retrieve port exposes `query` and",
    },
    {
        "query_id": "t1-q-012",
        "sample_id": "t1-engineering-001",
        "scene": 6,
        "question": "Per the ADR 0001 excerpt, what does the author identify as the interview gap that justifies shipping a host agent first?",
        "query_class": "narrative",
        "evidence_span": "The interview gap is orchestration",
    },
]


def _check_evidence_spans() -> None:
    by_id = {s["sample_id"]: s for s in SAMPLES}
    for q in QUERIES:
        sample = by_id[q["sample_id"]]
        text = (TEXT_DIR / sample["path"]).read_text(encoding="utf-8")
        if q["evidence_span"] not in text:
            sys.stderr.write(
                f"QUERY {q['query_id']}: evidence_span NOT FOUND in "
                f"{sample['path']!r}\n"
                f"  span: {q['evidence_span']!r}\n"
            )
            sys.exit(1)


def main() -> int:
    _check_evidence_spans()
    samples_out: list[dict] = []
    excluded: list[dict] = []
    per_scene: dict[int, int] = {}
    for s in SAMPLES:
        path = TEXT_DIR / s["path"]
        if not path.is_file():
            sys.stderr.write(f"missing frozen text: {path}\n")
            return 2
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        rec = {
            "sample_id": s["sample_id"],
            "scene": int(s["scene"]),
            "title": s["title"],
            "source": {
                "url": s["source_url"],
                "license": s["license"],
            },
            "fetched_at": FETCHED_AT,
            "sha256": sha,
            "path": s["path"],
            "document_id": s["document_id"],
        }
        if s.get("notes"):
            rec["notes"] = s["notes"]
        samples_out.append(rec)
        per_scene[int(s["scene"])] = per_scene.get(int(s["scene"]), 0) + 1
    counts = {
        "total": len(samples_out),
        "by_scene": {str(k): per_scene.get(k, 0) for k in range(1, 7)},
    }
    payload = {
        "schema_version": "1.0.0",
        "dataset_id": "fin-corpus-t1-public-v1",
        "t1_text_dir": "data/corpus/dataset/t1",
        "scenes": {
            "1": "onboarding",
            "2": "presales",
            "3": "troubleshooting",
            "4": "compliance",
            "5": "policy",
            "6": "engineering",
        },
        "notes": (
            "T1 frozen corpus (plan T1 / spec Focus 1). Each sample carries "
            "source.url + source.license + fetched_at + sha256; synthetic "
            "samples (none currently) are excluded from B1 counts. Sources: "
            "GitLab Handbook (CC BY-SA 4.0), Kubernetes docs (CC BY 4.0), "
            "EUR-Lex GDPR 2016/679 (official EU text), CAC/NPC PIPL "
            "(official PRC text), this repository ADR 0001 (MIT). "
            "Explicitly excluded per plan: GitLab marketing pricing pages "
            "(site ToS) and Google SRE book (CC BY-NC-ND)."
        ),
        "counts": counts,
        "excluded": excluded,
        "known_gaps": [],
        "samples": samples_out,
        "queries": QUERIES,
    }
    MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {MANIFEST_PATH} ({len(samples_out)} samples, {len(QUERIES)} queries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
