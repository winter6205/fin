#!/usr/bin/env bash
# One-command, idempotent build of the T2a pinned candidate-pipeline image.
#
# The build path is `docker buildx` on the BuildKit builder declared in
# image-pin.json (C1, operator option (a): see
# docs/validation/2026-09-30-g4-buildkit-reproducibility.md). The legacy
# builder stamps a wall-clock `created` and per-build mtimes into every image,
# so two clean builds of byte-identical inputs could never share an image id.
# BuildKit with `rewrite-timestamp=true` plus SOURCE_DATE_EPOCH normalises both
# at export: measured in this session, two clean builds of a 2-layer probe and
# of a COPY + `pip install --require-hashes --no-compile` + snapshot probe all
# produced identical manifest digests.
#
# The 4 GiB target ceiling for the BUILD steps is enforced where `docker build`
# used to enforce --memory/--memory-swap/--cpuset-cpus: on the builder itself,
# via `--driver-opt memory=4g` at `docker buildx create`. It bounds the whole
# BuildKit process tree, which is stricter than per-step. The legacy CPU
# affinity (--cpuset-cpus=0-3) does not carry onto the buildx path; that drop is
# recorded as data in image-pin.json's builder block (cpuset_cpus), not here in
# prose. `pin.py check-builder` below refuses to build on a builder whose
# identity or memory ceiling does not match the pin.
#
# This script is orchestration only. Every rule that can fail about the declared
# inputs lives in pin.py, where a hermetic test can reach it without docker;
# inlining those rules here is what let build.sh RECORD the pin without ever
# CHECKING it.
#
# Exit codes: 0 built and verified. 65 (EX_DATAERR) the declared inputs are
# invalid and the build was not attempted. 75 (EX_TEMPFAIL) the build ran but
# the image it produced is not the declared one. Both name themselves on stderr.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${G4_IMAGE:-fin-g4-pipeline:t2a}"
RESULTS="${HERE}/results"

# Snapshot write. Committed artifacts are moved into place only after the
# command that fills them succeeds: a `>` redirection truncates first, so a
# failed `docker run` would leave image_identity.txt empty -- and that file is
# one side of the L7 reconciliation, so a failed build would destroy the
# evidence the next build needs to detect drift.
snapshot() {
    local destination="$1"
    shift
    local temporary
    temporary="$(mktemp "${destination}.XXXXXX")"
    if ! "$@" > "${temporary}"; then
        rm -f "${temporary}"
        echo "[build] FATAL: could not write snapshot ${destination##*/}" >&2
        return 1
    fi
    mv "${temporary}" "${destination}"
}

# Declared inputs first, so an invalid pin fails before a 4 GiB build that
# would produce an image nobody can compare against.
echo "[build] validating declared inputs"
if ! python3 "${HERE}/pin.py" self-check --pipeline-dir "${HERE}"; then
    echo "[build] FATAL: the declared build inputs do not satisfy the pin; not building" >&2
    exit 65
fi

# The builder itself is a declared input. Its identity rules live in pin.py
# (`check-builder`), where a hermetic test reaches them without docker, a build
# or a network; this script's only job here is to feed pin.py the live host's
# answers and stop on a refusal. pin.py reads image-pin.json, so build.sh no
# longer parses that file itself -- there is now one JSON reader.
#
# What this proves is narrow and worth stating exactly. It proves the host can
# run the declared reproducible build path (buildx on the declared BuildKit
# builder, under the declared memory ceiling), so that the image-identity check
# below is comparing like with like. The declared digest is the one two
# independent clean builds agreed on (see docs/validation/2026-10-02-c1-pin-policy.md);
# the L7 digest `8e10185f…` predates this builder and is kept as history only.
# The check still refuses (EX_TEMPFAIL) any image that is not the declared one.

# The live engine version, or empty when the daemon cannot be asked. Empty is
# not "any version": an unanswered question is a gap, and pin.py refuses on it.
LIVE_ENGINE="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
# "buildx" when the buildx CLI plugin answers, "docker-build" when it does not.
if docker buildx version >/dev/null 2>&1; then
    LIVE_MODE="buildx"
else
    LIVE_MODE="docker-build"
fi

# The builder name and the memory ceiling are read through pin.py, the single
# reader of the declaration; a value pin.py cannot read exits 65.
BUILDER_NAME="$(python3 "${HERE}/pin.py" get --pipeline-dir "${HERE}" --path builder.buildx_builder)" || exit 65
MEMORY_CEILING="$(python3 "${HERE}/pin.py" get --pipeline-dir "${HERE}" --path builder.memory_ceiling)" || exit 65

# `docker buildx inspect <declared builder>` output, or empty when the builder
# does not exist on this host. Empty must fail, not waive -- pin.py check-builder
# refuses on it, so an absent builder never reaches the 4 GiB build.
INSPECT_TMP="$(mktemp)"
if ! docker buildx inspect "${BUILDER_NAME}" >"${INSPECT_TMP}" 2>/dev/null; then
    : >"${INSPECT_TMP}"
fi

echo "[build] verifying the builder against the declaration"
if ! python3 "${HERE}/pin.py" check-builder \
        --pipeline-dir "${HERE}" \
        --live-engine "${LIVE_ENGINE}" \
        --live-mode "${LIVE_MODE}" \
        --inspect-file "${INSPECT_TMP}"; then
    rm -f "${INSPECT_TMP}"
    echo "[build] FATAL: the host's builder is not the declared one; not building" >&2
    exit 65
fi
rm -f "${INSPECT_TMP}"

EPOCH="$(python3 "${HERE}/pin.py" get --pipeline-dir "${HERE}" --path builder.source_date_epoch)" || exit 65
# Clean by default: reproducibility is proved by --no-cache builds, and a layer
# cache can hide a step that no longer reproduces. G4_BUILD_USE_CACHE=1 opts in
# to cached rebuilds once a baseline has been agreed.
CACHE_FLAGS=(--no-cache)
if [[ "${G4_BUILD_USE_CACHE:-0}" == "1" ]]; then
    CACHE_FLAGS=()
fi
# torch/torchvision are served from download-r2.pytorch.org, which is reachable
# from this host's network namespace but was measured unreachable from the
# default container network (docs/validation/2026-09-30-g4-buildkit-
# reproducibility.md). G4_PIP_FIND_LINKS points at a local directory whose
# wheels are admitted only because their sha256 matches requirements.lock; the
# pip line itself stays exactly as the pin contract requires.
BUILD_ARG_FLAGS=()
if [[ -n "${G4_PIP_FIND_LINKS:-}" ]]; then
    BUILD_ARG_FLAGS+=(--build-arg "PIP_FIND_LINKS=${G4_PIP_FIND_LINKS}")
fi

echo "[build] image=${IMAGE} builder=${BUILDER_NAME} ceiling: memory=${MEMORY_CEILING} (builder driver-opt) SOURCE_DATE_EPOCH=${EPOCH} rewrite-timestamp=true"

SOURCE_DATE_EPOCH="${EPOCH}" docker buildx build \
    --builder "${BUILDER_NAME}" \
    "${CACHE_FLAGS[@]}" \
    --provenance=false --sbom=false \
    --output "type=docker,rewrite-timestamp=true,name=${IMAGE}" \
    "${BUILD_ARG_FLAGS[@]}" \
    --file "${HERE}/Dockerfile" \
    "${HERE}"

# Post-build verification of the OCR language contract (ADR 0054: fin's
# TesseractEngine default is `chi_sim+eng`, the OS must ship both tessdata).
# The Dockerfile already fails the build on a missing language, so this is the
# out-of-build confirmation on the image that actually got tagged: one boot,
# recorded once, asserted from the record rather than re-queried.
echo "[build] verifying tessdata inside the built image"
snapshot "${RESULTS}/image_tesseract_langs.txt" \
    docker run --rm --entrypoint /usr/bin/tesseract "${IMAGE}" --list-langs
grep -qx 'eng' "${RESULTS}/image_tesseract_langs.txt"
grep -qx 'chi_sim' "${RESULTS}/image_tesseract_langs.txt"

# Record the baked resolved-version manifests and the image identity so
# PIN_MANIFEST.md and apt-packages.lock can cite artifacts rather than guesses.
snapshot "${RESULTS}/image_pip_freeze.txt" \
    docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/pip_freeze.txt
snapshot "${RESULTS}/image_apt_status.txt" \
    docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/apt_status.txt
snapshot "${RESULTS}/image_python_version.txt" \
    docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/python_version.txt
# Tesseract is an apt package, so it is absent from pip freeze and its version
# has no artifact of its own otherwise. A pin the record cites has to be
# readable from a committed file, not from memory.
snapshot "${RESULTS}/image_tesseract_version.txt" \
    docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/tesseract_version.txt
snapshot "${RESULTS}/image_identity.txt" \
    docker image inspect "${IMAGE}" \
    --format 'image_id={{.Id}}{{"\n"}}created={{.Created}}{{"\n"}}size_bytes={{.Size}}{{"\n"}}arch={{.Architecture}}'

# The model weights are host-mounted, so the image digest does not cover them.
# Verifying them here is cheap and catches the failure the image id cannot: a
# host whose cache drifted from the declared revisions.
#
# This runs BEFORE the identity check, and that order is load-bearing: on a
# builder that cannot reproduce a fixed digest, `compare-image-id` fails every
# build, so a gate placed after it never executes on any real run. The model
# content hashes would then be asserted only by a hand-run command.
# run_measured.sh's own fetch preamble is the natural home for this too, but
# that file is outside this change's write surface -- see PIN_MANIFEST.md §5 and
# the validation record for the exact change it needs.
MODEL_CACHE="${G4_MODEL_CACHE:-$HOME/.cache/fin-g4-docling-models}"
if [[ -d "${MODEL_CACHE}" ]]; then
    echo "[build] verifying model artifacts against the declared revisions"
    if ! python3 "${HERE}/pin.py" verify-models \
            --pin "${HERE}/models.lock" \
            --cache-root "${MODEL_CACHE}"; then
        echo "[build] FATAL: the model cache at ${MODEL_CACHE} does not match models.lock" >&2
        exit 75
    fi
else
    # A missing cache is a real state, not an absent check: continuing would
    # skip the model gate entirely on any host that has not run a measurement
    # yet, and "the check did not run" must not read as "the check passed".
    # The weights are fetched by run_measured.sh, bound to the revisions in
    # models.lock and verified per run (PIN_MANIFEST.md §5); a cold host stops
    # here rather than proceeding unverified.
    echo "[build] FATAL: no model cache at ${MODEL_CACHE}; the model gate cannot run, so the pin is unverified. Populate it (run_measured.sh fetches into it) or set G4_MODEL_CACHE" >&2
    exit 75
fi

# The drift check. The expectation comes from image-pin.json, which no build
# step writes -- comparing the identity against the snapshot this same run just
# produced would be true by construction. Non-zero here leaves the freshly
# written snapshot in place on purpose: the operator needs the actual id to
# decide what changed, and destroying it would hide the evidence.
echo "[build] verifying the built image against the declared identity"
if ! python3 "${HERE}/pin.py" compare-image-id \
        --pin "${HERE}/image-pin.json" \
        --observed "${RESULTS}/image_identity.txt"; then
    echo "[build] FATAL: the image built as ${IMAGE} is not the declared one; actual id is in ${RESULTS}/image_identity.txt" >&2
    exit 75
fi

echo "[build] ok -> ${IMAGE}"
