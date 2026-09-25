#!/usr/bin/env bash
# T2a measured run: execute driver.py in the pinned image UNDER the hard 4 GiB
# ceiling and tee the machine-readable result into results/.
#
# The ceiling is literal and load-bearing:
#     docker run --memory=4g --memory-swap=4g --cpus=4
# It is never raised, and the driver is never run on the host as a substitute,
# because a pass outside the ceiling is not the 4 GB measurement the ticket
# asks for. If a step fails under the ceiling, that failure IS the result and
# is recorded verbatim.
#
# Usage:
#   ./run_measured.sh <pdf-path> [extra driver args...]
#   <pdf-path> may be absolute or worktree-relative.
#
# Env overrides (names only; no secrets are read or printed anywhere):
#   G4_WORKTREE, G4_IMAGE, G4_MODEL_CACHE, G4_CEILING_MEMORY,
#   G4_CEILING_MEMORY_SWAP, G4_CEILING_CPUS
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="${G4_WORKTREE:-/home/winner/projects/fin-wt-g4-attachment}"
IMAGE="${G4_IMAGE:-fin-g4-pipeline:t2a}"
MODEL_CACHE="${G4_MODEL_CACHE:-$HOME/.cache/fin-g4-docling-models}"
CEIL_MEM="${G4_CEILING_MEMORY:-4g}"
CEIL_SWAP="${G4_CEILING_MEMORY_SWAP:-4g}"
CEIL_CPUS="${G4_CEILING_CPUS:-4}"
RESULTS="${HERE}/results"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <pdf-path> [driver args...]" >&2
    exit 64
fi
PDF_ARG="$1"
shift

PDF_HOST="${PDF_ARG}"
[[ "${PDF_HOST}" = /* ]] || PDF_HOST="${WORKTREE}/${PDF_ARG}"
if [[ ! -f "${PDF_HOST}" ]]; then
    echo "[run] PDF not found: ${PDF_HOST}" >&2
    exit 66
fi

# Inside the container the worktree is mounted read-only at /worktree, so the
# driver is always addressed through the mount.
if [[ "${PDF_HOST}" == "${WORKTREE}"/* ]]; then
    PDF_IN_CONTAINER="/worktree/${PDF_HOST#"${WORKTREE}/"}"
else
    PDF_IN_CONTAINER="${PDF_HOST}"
fi

BASE="$(basename "${PDF_HOST}" .pdf)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${RESULTS}/${BASE}_${STAMP}"
CTR="fin-g4-t2a-${BASE}-$$"
mkdir -p "${RUN_DIR}/pages" "${MODEL_CACHE}/hf" "${MODEL_CACHE}/torch" "${RESULTS}"

# One-time, cached model fetch (constraint: the Heron weights download once into
# the mounted host cache and are reused). Docling 2.130 will NOT auto-download
# into an explicit artifacts_path - it raises FileNotFoundError pointing at this
# exact command - so the harness runs it here, idempotently.
# HF_HUB_DISABLE_XET=1 is required on this host: the hf-xet transfer endpoint
# times out through the WSL proxy, while plain HTTPS downloads work. The raised
# HF_HUB_*_TIMEOUT values are needed too - the default 10 s request timeout
# produced IncompleteSnapshotError on the 203 MB TableFormer weights here.
# The guard checks the weights themselves, not just the repo directory, because
# a partial snapshot (configs only) is exactly the failure that broke the first
# measured run.
if [[ ! -f "${MODEL_CACHE}/docling-project--docling-layout-heron/model.safetensors" ||
      ! -f "${MODEL_CACHE}/docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors" ]]; then
    echo "[run] artifacts cache incomplete -> fetching layout + tableformer into ${MODEL_CACHE}"
    docker run --rm \
        --memory="${CEIL_MEM}" --memory-swap="${CEIL_SWAP}" --cpus="${CEIL_CPUS}" \
        --user "$(id -u):$(id -g)" \
        --env HOME=/tmp --env HF_HOME=/models/hf --env HF_HUB_DISABLE_XET=1 \
        --env HF_HUB_DOWNLOAD_TIMEOUT=180 --env HF_HUB_ETAG_TIMEOUT=120 \
        --volume "${MODEL_CACHE}:/models" \
        --entrypoint /usr/local/bin/docling-tools \
        "${IMAGE}" models download --output-dir /models layout tableformer \
        > "${RESULTS}/model_fetch.log" 2>&1
    echo "[run] model fetch exit=$? log=${RESULTS}/model_fetch.log"
    ls -1 "${MODEL_CACHE}" | sed 's/^/[run] artifacts: /'
fi

DOCKER_CMD=(
    docker run --name "${CTR}"
    --memory="${CEIL_MEM}" --memory-swap="${CEIL_SWAP}" --cpus="${CEIL_CPUS}"
    --user "$(id -u):$(id -g)"
    --env HOME=/tmp
    --env HF_HOME=/models/hf
    --env TORCH_HOME=/models/torch
    --env DOCLING_ARTIFACTS_PATH=/models
    --env HF_HUB_DISABLE_TELEMETRY=1
    --env HF_HUB_DISABLE_XET=1
    --env PYTHONPATH=/worktree/src
    --volume "${WORKTREE}:/worktree:ro"
    --volume "${RUN_DIR}:/out"
    --volume "${MODEL_CACHE}:/models"
    --entrypoint python
    "${IMAGE}"
    /worktree/scripts/g4_pipeline/driver.py
    --pdf "${PDF_IN_CONTAINER}"
    --artifacts /models
    --write-images --image-dir /out/pages
    "$@"
)

echo "[run] PDF (host)      : ${PDF_HOST}  ($(stat -c %s "${PDF_HOST}") bytes)"
echo "[run] PDF (container) : ${PDF_IN_CONTAINER}"
echo "[run] model cache     : ${MODEL_CACHE} -> /models"
echo "[run] output dir      : ${RUN_DIR}"
echo "[run] ceiling flags   : --memory=${CEIL_MEM} --memory-swap=${CEIL_SWAP} --cpus=${CEIL_CPUS}"
echo "[run] verbatim command:"
printf '  %q' "${DOCKER_CMD[@]}"
printf '\n\n'

# Foreground run; stdout is the single JSON line, stderr the human log.
RUN_EXIT=0
"${DOCKER_CMD[@]}" >"${RUN_DIR}/stdout.jsonl" 2>"${RUN_DIR}/stderr.log" || RUN_EXIT=$?
cat "${RUN_DIR}/stderr.log"

CID="$(docker inspect --format '{{.Id}}' "${CTR}" 2>/dev/null || true)"
OOMKILLED=unknown
STATE_EXIT=unknown
STATE_ERR=none
HOST_PEAK=
HOST_SWAP_PEAK=
if [[ -n "${CID}" ]]; then
    read -r OOMKILLED STATE_EXIT STATE_ERR < <(docker inspect \
        --format '{{if .State.OOMKilled}}true{{else}}false{{end}} {{.State.ExitCode}} {{.State.Error}}' "${CTR}")
    # Out-of-band cgroup peak from the host: survives even if the driver died
    # before it could print its line. Requires read permission on the cgroup.
    for f in "/sys/fs/cgroup/docker/${CID}/memory.peak" \
             "/sys/fs/cgroup/system.slice/docker-${CID}.scope/memory.peak"; do
        [[ -r ${f} ]] && HOST_PEAK="$(cat "${f}")" && break
    done
    for f in "/sys/fs/cgroup/docker/${CID}/memory.swap.peak" \
             "/sys/fs/cgroup/system.slice/docker-${CID}.scope/memory.swap.peak"; do
        [[ -r ${f} ]] && HOST_SWAP_PEAK="$(cat "${f}")" && break
    done
fi
docker rm -f "${CTR}" >/dev/null 2>&1 || true

grep -E '^\{"schema":"fin\.g4' "${RUN_DIR}/stdout.jsonl" >"${RUN_DIR}/${BASE}.raw.jsonl" || true
N="$(wc -l <"${RUN_DIR}/${BASE}.raw.jsonl")"
if [[ "${N}" != "1" ]]; then
    echo "[run] FAIL: expected exactly 1 JSON line on stdout, got ${N}" >&2
    echo "[run] docker_run_exit=${RUN_EXIT} container_exit=${STATE_EXIT} OOMKilled=${OOMKILLED} state_error=${STATE_ERR}" >&2
    echo "[run] last 40 stderr lines:" >&2
    tail -40 "${RUN_DIR}/stderr.log" >&2 || true
    printf '{"pdf":"%s","docker_run_exit":%s,"container_exit":"%s","oom_killed":"%s","state_error":"%s","json_lines":%s,"host_cgroup_memory_peak_bytes":"%s"}\n' \
        "${PDF_HOST}" "${RUN_EXIT}" "${STATE_EXIT}" "${OOMKILLED}" "${STATE_ERR}" "${N}" "${HOST_PEAK}" \
        >"${RESULTS}/${BASE}_${STAMP}.meta.json"
    exit 1
fi

python3 - "${RUN_DIR}/${BASE}.raw.jsonl" "${RUN_DIR}/${BASE}.measured.jsonl" \
    "${RESULTS}/${BASE}_${STAMP}.jsonl" "${RESULTS}/${BASE}_${STAMP}.meta.json" \
    "${OOMKILLED}" "${STATE_EXIT}" "${RUN_EXIT}" "${HOST_PEAK}" "${HOST_SWAP_PEAK}" \
    "${CEIL_MEM}" "${CEIL_SWAP}" "${CEIL_CPUS}" <<'PY'
import json, sys
(raw, measured, jsonl_copy, meta, oom, cexit, rexit, hpeak, hswap, cmem, cswap, ccpus) = sys.argv[1:13]
rec = json.loads(open(raw, encoding="utf-8").read().strip())
oom_b = {"true": True, "false": False}.get(oom.lower())
t = rec.setdefault("totals", {})
t["oom_killed_docker_inspect"] = oom_b
if oom_b is not None:
    t["oom_killed"] = oom_b
    t["oom_killed_source"] = "docker inspect .State.OOMKilled (cgroup counter kept alongside)"
t["container_exit_code"] = int(cexit) if cexit.isdigit() else cexit
t["docker_run_exit_code"] = int(rexit)
t["host_side_cgroup_memory_peak_bytes"] = int(hpeak) if hpeak.isdigit() else None
t["host_side_cgroup_memory_swap_peak_bytes"] = int(hswap) if hswap.isdigit() else None
t["ceiling"] = {"memory": cmem, "memory_swap": cswap, "cpus": ccpus}
line = json.dumps(rec, separators=(",", ":")) + "\n"
open(measured, "w", encoding="utf-8").write(line)
open(jsonl_copy, "w", encoding="utf-8").write(line)
summary = {k: t.get(k) for k in (
    "peak_rss_vmhwm_bytes", "cgroup_peak_bytes", "cgroup_memory_max_bytes",
    "cgroup_memory_swap_max_bytes", "cgroup_memory_swap_current_bytes",
    "proc_vmswap_bytes", "oom_kill_events_in_cgroup", "oom_killed",
    "oom_killed_docker_inspect", "container_exit_code", "docker_run_exit_code",
    "host_side_cgroup_memory_peak_bytes", "host_side_cgroup_memory_swap_peak_bytes",
    "total_latency_ms", "render_and_ocr_latency_ms", "docling_whole_doc_latency_ms",
    "page_latency_ms_stats", "regions_total", "pages_needing_ocr",
    "pages_with_ocr_applied", "pages_with_native_text", "native_text_chars_total",
    "ocr_text_chars_total", "docling_error", "docling_status", "docling_page_count", "slice_pass_error", "ceiling")}
summary = {"pdf": rec["file"]["name"], "pdf_bytes": rec["file"]["bytes"],
           "pdf_sha256": rec["file"]["sha256"], "page_count": rec["page_count"],
           "measured_at_utc": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
           **summary}
open(meta, "w", encoding="utf-8").write(json.dumps(summary, indent=2) + "\n")
print("[run] summary: " + json.dumps(summary, separators=(",", ":")))
PY

echo "[run] measured JSON   : ${RUN_DIR}/${BASE}.measured.jsonl"
echo "[run] page bitmaps    : ${RUN_DIR}/pages"
