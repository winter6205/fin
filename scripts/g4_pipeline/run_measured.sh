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
# Derived from this script's own location so any checkout works.
WORKTREE="${G4_WORKTREE:-$(cd "${HERE}/../.." && pwd)}"
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

# Single schema for both the measured and the failed record: values the harness
# never read are emitted as JSON null by the serializer, never as "" or "false",
# and every string (paths, docker state errors) is escaped there rather than by
# printf. Usage: emit_meta <mode> <raw-jsonl-path-or-empty> <meta-path>.
emit_meta() {
    G4_MODE="$1" G4_RAW="$2" G4_META="$3" \
    G4_PDF="${PDF_HOST}" G4_OOM="${OOMKILLED:-}" G4_CEXIT="${STATE_EXIT:-}" \
    G4_REXIT="${RUN_EXIT:-}" G4_PEAK="${HOST_PEAK:-}" G4_SWAP_PEAK="${HOST_SWAP_PEAK:-}" \
    G4_STATE_ERR="${STATE_ERR:-}" G4_JSON_LINES="${JSON_LINES:-}" \
    G4_CEIL_MEM="${CEIL_MEM}" G4_CEIL_SWAP="${CEIL_SWAP}" G4_CEIL_CPUS="${CEIL_CPUS}" \
    G4_MEASURED="${RUN_DIR}/${BASE}.measured.jsonl" \
    G4_JSONL_COPY="${RESULTS}/${BASE}_${STAMP}.jsonl" \
        python3 - <<'PY'
import json, os, sys, time

env = os.environ


def as_int(value):
    # Only ASCII digits are a number. The module this harness measures
    # (fin.host.attachment_budgets) rejects a full-width digit as a confusable,
    # and text handed over by the shell is the same kind of input.
    text = (value or "").strip()
    digits = text[1:] if text.startswith("-") else text
    return int(text) if digits.isascii() and digits.isdigit() else None


def as_bool(value):
    return {"true": True, "false": False}.get((value or "").strip().lower())


# An empty State.Error means no error was captured, which serializes as null
# rather than as a placeholder string that would need decoding a second time.
state_error = (env.get("G4_STATE_ERR") or "").strip() or None

oom = as_bool(env.get("G4_OOM"))
cexit = as_int(env.get("G4_CEXIT"))
rexit = as_int(env.get("G4_REXIT"))
hpeak = as_int(env.get("G4_PEAK"))
hswap = as_int(env.get("G4_SWAP_PEAK"))
raw = env.get("G4_RAW") or ""
rec = json.loads(open(raw, encoding="utf-8").read().strip()) if raw else None
# Patching rec's own totals dict keeps the harness observations inside the
# measured JSONL artifact as well as the meta summary.
totals = rec.setdefault("totals", {}) if rec else {}

# The driver's reading of the cgroup counter owns oom_killed and
# oom_killed_source: overwriting them left two artifacts of one run disagreeing
# about the same verdict. Docker's opinion is recorded under its own name.
totals["oom_killed_docker_inspect"] = oom
totals["container_exit_code"] = cexit
totals["docker_run_exit_code"] = rexit
totals["host_side_cgroup_memory_peak_bytes"] = hpeak
totals["host_side_cgroup_memory_swap_peak_bytes"] = hswap
totals["ceiling"] = {"memory": env["G4_CEIL_MEM"], "memory_swap": env["G4_CEIL_SWAP"],
                     "cpus": env["G4_CEIL_CPUS"]}

if env["G4_MODE"] == "measured":
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    open(env["G4_MEASURED"], "w", encoding="utf-8").write(line)
    open(env["G4_JSONL_COPY"], "w", encoding="utf-8").write(line)

driver_keys = (
    "peak_rss_vmhwm_bytes", "cgroup_peak_bytes", "cgroup_memory_max_bytes",
    "cgroup_memory_swap_max_bytes", "cgroup_memory_swap_current_bytes",
    "proc_vmswap_bytes", "oom_kill_events_in_cgroup", "oom_killed",
    "oom_killed_docker_inspect", "container_exit_code", "docker_run_exit_code",
    "host_side_cgroup_memory_peak_bytes", "host_side_cgroup_memory_swap_peak_bytes",
    "total_latency_ms", "render_and_ocr_latency_ms", "docling_whole_doc_latency_ms",
    "page_latency_ms_stats", "regions_total", "pages_needing_ocr",
    "pages_with_ocr_applied", "pages_with_native_text", "native_text_chars_total",
    "ocr_text_chars_total", "docling_error", "docling_status", "docling_page_count",
    "slice_pass_error", "ceiling")
# These names also live in driver.py's build_totals, so the mirror is only
# honest while it is checked: a rename there must fail loudly here rather than
# summarise a real measurement as null. A failed run has no driver totals at all
# and is therefore exempt.
if rec:
    lost = [k for k in driver_keys if k not in totals]
    if lost:
        sys.exit("[run] FATAL: driver totals lack " + ", ".join(sorted(lost)))
file_info = rec.get("file", {}) if rec else {}
meta = {"pdf": file_info.get("name") or env["G4_PDF"],
        "pdf_bytes": file_info.get("bytes"),
        "pdf_sha256": file_info.get("sha256"),
        "page_count": rec.get("page_count") if rec else None,
        "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
meta.update({k: totals.get(k) for k in driver_keys})
# state_error and json_lines describe the harness rather than the container, so
# they exist only here; the rest are the driver's own totals names, projected
# onto the summary by the checked list above.
meta.update({"state_error": state_error,
             "json_lines": as_int(env.get("G4_JSON_LINES"))})
open(env["G4_META"], "w", encoding="utf-8").write(json.dumps(meta, indent=2) + "\n")
print("[run] summary: " + json.dumps(meta, separators=(",", ":")))
PY
}

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
    FETCH_EXIT=0
    docker run --rm \
        --memory="${CEIL_MEM}" --memory-swap="${CEIL_SWAP}" --cpus="${CEIL_CPUS}" \
        --user "$(id -u):$(id -g)" \
        --env HOME=/tmp --env HF_HOME=/models/hf --env HF_HUB_DISABLE_XET=1 \
        --env HF_HUB_DOWNLOAD_TIMEOUT=180 --env HF_HUB_ETAG_TIMEOUT=120 \
        --volume "${MODEL_CACHE}:/models" \
        --entrypoint /usr/local/bin/docling-tools \
        "${IMAGE}" models download --output-dir /models layout tableformer \
        > "${RESULTS}/model_fetch.log" 2>&1 || FETCH_EXIT=$?
    if [[ "${FETCH_EXIT}" != "0" ]]; then
        echo "[run] FATAL: model fetch exit=${FETCH_EXIT}, no measurement taken; log=${RESULTS}/model_fetch.log" >&2
        tail -20 "${RESULTS}/model_fetch.log" >&2 || true
        exit 1
    fi
    echo "[run] model fetch ok, log=${RESULTS}/model_fetch.log"
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
# Empty means "inspect answered nothing" and serialises as JSON null, so no
# placeholder string has to be decoded again on the other side of the boundary.
OOMKILLED=
STATE_EXIT=
STATE_ERR=
HOST_PEAK=
HOST_SWAP_PEAK=
if [[ -n "${CID}" ]]; then
    # An already-removed or never-created container makes inspect print nothing.
    # That answer must degrade to empty fields rather than abort the script here,
    # which would skip docker rm and the record: under this harness the failure
    # is the result.
    STATE_LINE="$(docker inspect \
        --format '{{if .State.OOMKilled}}true{{else}}false{{end}} {{.State.ExitCode}} {{.State.Error}}' \
        "${CTR}" 2>/dev/null || true)"
    read -r OOMKILLED STATE_EXIT STATE_ERR <<<"${STATE_LINE}" || true
    # Out-of-band cgroup peak from the host: survives even if the driver died
    # before it could print its line. Requires read permission on the cgroup.
    # Both leaves live in the same scope directory, so one probe walk covers
    # memory and swap and an unreadable scope leaves both null.
    for scope in "/sys/fs/cgroup/docker/${CID}" \
                 "/sys/fs/cgroup/system.slice/docker-${CID}.scope"; do
        if [[ -z "${HOST_PEAK}" && -r "${scope}/memory.peak" ]]; then
            HOST_PEAK="$(cat "${scope}/memory.peak")"
        fi
        if [[ -z "${HOST_SWAP_PEAK}" && -r "${scope}/memory.swap.peak" ]]; then
            HOST_SWAP_PEAK="$(cat "${scope}/memory.swap.peak")"
        fi
        if [[ -n "${HOST_PEAK}" && -n "${HOST_SWAP_PEAK}" ]]; then
            break
        fi
    done
fi
docker rm -f "${CTR}" >/dev/null 2>&1 || true

grep -E '^\{"schema":"fin\.g4' "${RUN_DIR}/stdout.jsonl" >"${RUN_DIR}/${BASE}.raw.jsonl" || true
N="$(wc -l <"${RUN_DIR}/${BASE}.raw.jsonl")"
JSON_LINES="${N}"
if [[ "${N}" != "1" ]]; then
    echo "[run] FAIL: expected exactly 1 JSON line on stdout, got ${N}" >&2
    # The wording a human reads stays explicit here; the record itself carries
    # these fields as null when inspect answered nothing.
    echo "[run] docker_run_exit=${RUN_EXIT} container_exit=${STATE_EXIT:-unknown} OOMKilled=${OOMKILLED:-unknown} state_error=${STATE_ERR:-none}" >&2
    echo "[run] last 40 stderr lines:" >&2
    tail -40 "${RUN_DIR}/stderr.log" >&2 || true
    emit_meta failed "" "${RESULTS}/${BASE}_${STAMP}.meta.json"
    exit 1
fi

emit_meta measured "${RUN_DIR}/${BASE}.raw.jsonl" "${RESULTS}/${BASE}_${STAMP}.meta.json"

echo "[run] measured JSON   : ${RUN_DIR}/${BASE}.measured.jsonl"
echo "[run] page bitmaps    : ${RUN_DIR}/pages"
