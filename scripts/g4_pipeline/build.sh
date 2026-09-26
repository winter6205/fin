#!/usr/bin/env bash
# One-command, idempotent build of the T2a pinned candidate-pipeline image.
#
# The 4 GiB target device is honoured for the BUILD steps as well as the
# measurement run: docker build is given --memory/--memory-swap/--cpuset-cpus
# so a layer that cannot fit in 4 GiB fails here rather than producing a
# runtime that only works on a bigger box. (The legacy builder has no --cpus
# flag; --cpuset-cpus=0-3 pins it to the same 4 cores.)
#
# Re-running is cheap: unchanged layers are cache hits and the tag is simply
# re-pointed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${G4_IMAGE:-fin-g4-pipeline:t2a}"
CEILING_MEMORY="${G4_BUILD_MEMORY:-4g}"
CEILING_MEMORY_SWAP="${G4_BUILD_MEMORY_SWAP:-4g}"
CEILING_CPUSET="${G4_BUILD_CPUSET:-0-3}"

echo "[build] image=${IMAGE} ceiling: --memory=${CEILING_MEMORY} --memory-swap=${CEILING_MEMORY_SWAP} --cpuset-cpus=${CEILING_CPUSET}"

docker build \
    --memory="${CEILING_MEMORY}" \
    --memory-swap="${CEILING_MEMORY_SWAP}" \
    --cpuset-cpus="${CEILING_CPUSET}" \
    --tag "${IMAGE}" \
    --file "${HERE}/Dockerfile" \
    "${HERE}"

# Post-build verification of the OCR language contract (ADR 0054: fin's
# TesseractEngine default is `chi_sim+eng`, the OS must ship both tessdata).
# The Dockerfile already fails the build on a missing language, so this is the
# out-of-build confirmation on the image that actually got tagged: one boot,
# recorded once, asserted from the record rather than re-queried.
echo "[build] verifying tessdata inside the built image"
docker run --rm --entrypoint /usr/bin/tesseract "${IMAGE}" --list-langs \
    | tee "${HERE}/results/image_tesseract_langs.txt"
grep -qx 'eng' "${HERE}/results/image_tesseract_langs.txt"
grep -qx 'chi_sim' "${HERE}/results/image_tesseract_langs.txt"

# Record the baked resolved-version manifest and the image identity so
# PIN_MANIFEST.md can cite the artifact rather than a guess.
docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/pip_freeze.txt \
    > "${HERE}/results/image_pip_freeze.txt"
docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/python_version.txt \
    > "${HERE}/results/image_python_version.txt"
# Tesseract is an apt package, so it is absent from pip freeze and its version
# has no artifact of its own otherwise. PIN_MANIFEST.md cites 5.5.0, and a pin
# the record cites has to be readable from a committed file, not from memory.
docker run --rm --entrypoint /bin/cat "${IMAGE}" /opt/g4/tesseract_version.txt \
    > "${HERE}/results/image_tesseract_version.txt"
docker image inspect "${IMAGE}" \
    --format 'image_id={{.Id}}{{"\n"}}created={{.Created}}{{"\n"}}size_bytes={{.Size}}{{"\n"}}arch={{.Architecture}}{{"\n"}}repo_digests={{.RepoDigests}}' \
    > "${HERE}/results/image_identity.txt"

echo "[build] ok -> ${IMAGE}"
