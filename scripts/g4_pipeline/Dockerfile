# T2a pinned candidate-pipeline image (fin G4 attachment measurement harness).
#
# Purpose: reproduce *the* runtime that plans/thread-attachment-source-read.md
# T2a must measure, per docs/adr/0089: Docling's standard PDF pipeline with the
# Heron layout model, CPU-only torch, Tesseract OCR on CPU, and no local VLM /
# no separate picture-classification model.
#
# This image is deliberately NOT the fin application image: `docling` stays out
# of pyproject.toml / uv.lock (T2d owns the dependency decision, see
# PIN_MANIFEST.md). Nothing from src/ is installed here; the worktree is
# mounted read-only at run time.
#
# The 4 GiB target is enforced at `docker run` time by run_measured.sh
# (`--memory=4g --memory-swap=4g --cpus=4`), never by relaxing this file.

FROM python:3.12-slim

# ---- versions -------------------------------------------------------------
# docling is pinned exactly. torch/torchvision are pinned to the CPU-only
# wheel index because the 4 GB target has no usable discrete GPU; every other
# transitive version is whatever pip resolves and is recorded verbatim from
# `pip freeze` (see /opt/g4/pip_freeze.txt baked into the image).
ARG DOCLING_VERSION=2.130.0
ARG TORCH_VERSION=2.9.1+cpu
ARG TORCHVISION_VERSION=0.24.1+cpu
ARG PYTESSERACT_VERSION=0.3.13

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=8 \
    PIP_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    # Keep every model download inside the mounted cache volume, never in an
    # image layer, so the Heron weights are fetched once on the host and reused.
    HF_HOME=/models/hf \
    DOCLING_ARTIFACTS_PATH=/models

# ---- native tooling: CPU Tesseract + opencv/pypdfium2 runtime libs --------
# tesseract-ocr-eng + tesseract-ocr-chi-sim are mandatory, not cosmetic:
# docs/adr/0054 freezes fin's OCR default language string to `chi_sim+eng`
# (src/fin/corpus/ocr.py: DEFAULT_TESSERACT_LANG), so "the OS must ship those
# tessdata files" for the measured OCR path to be the real one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

# Fail the build (not the measurement) if a required tessdata file is missing.
# Checked through the tesseract CLI itself so the exact tessdata prefix of the
# base image does not matter.
RUN tesseract --version \
    && tesseract --list-langs \
    && tesseract --list-langs 2>&1 | grep -qx 'eng' \
    && tesseract --list-langs 2>&1 | grep -qx 'chi_sim'

# ---- CPU torch first, so no CUDA build can be resolved afterwards ---------
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}" \
        "torchvision==${TORCHVISION_VERSION}"

# ---- the pinned candidate pipeline + the repo's OCR front-end -------------
# `--extra-index-url` stays pointed at the CPU wheel index so a stray torch
# requirement can never pull a multi-GB CUDA wheel back in.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        "docling==${DOCLING_VERSION}" \
        "pytesseract==${PYTESSERACT_VERSION}"

# Guard: the whole point of the index choice. If torch is a CUDA build the
# 4 GB measurement is meaningless, so break the build instead.
RUN python - <<'PY'
import torch
print("torch", torch.__version__)
assert "cpu" in torch.__version__.lower() or torch.version.cuda is None, torch.__version__
assert not torch.cuda.is_available(), "CUDA visible in the 4 GB CPU target image"
PY

# ---- bake the resolved manifest + the harness sources ---------------------
RUN mkdir -p /opt/g4 /models /worktree \
    && pip freeze > /opt/g4/pip_freeze.txt \
    && python -c "import sys; print(sys.version)" > /opt/g4/python_version.txt \
    && tesseract --list-langs > /opt/g4/tesseract_langs.txt 2>&1 \
    && tesseract --version > /opt/g4/tesseract_version.txt 2>&1

# driver.py is NOT baked: it lives in the worktree, which run_measured.sh mounts
# read-only. Baking a second copy would create a stale source of truth.
WORKDIR /worktree
ENTRYPOINT ["/bin/bash"]
