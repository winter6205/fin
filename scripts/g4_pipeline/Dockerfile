# T2a pinned candidate-pipeline image: Docling's standard PDF pipeline with the
# Heron layout model, CPU-only torch and Tesseract OCR, per docs/adr/0089. Not
# the fin application image -- `docling` stays out of pyproject.toml (T2d owns
# that decision). Nothing from src/ is installed; the worktree is mounted
# read-only at run time, and the 4 GiB ceiling is enforced there by
# run_measured.sh, never by relaxing this file.
#
# Every build input that determines the image bytes is pinned mechanically
# (issue 324 C1): base image by digest, apt by an immutable snapshot mirror plus
# exact versions, Python by a hash-locked requirements.lock. The declared
# expectation lives in image-pin.json and is checked by pin.py, which build.sh
# runs. PIN_MANIFEST.md explains the pin; it does not constitute it.

# linux/amd64 manifest of the python 3.12 slim image. The tag moved Debian
# bookworm -> trixie under an earlier revision of this pin without any edit
# here, which is the clearest single piece of evidence that a tag is not a pin.
FROM python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b

# The base ships a floating /etc/apt/sources.list.d/debian.sources that even
# carried a commented snapshot.debian.org URI while still resolving against
# deb.debian.org. Pin it to the timestamp apt-packages.lock records.
ARG APT_SNAPSHOT=20260929T083940Z

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=8 \
    PIP_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # A reproducibility input, not a preference: it stops the RUN steps below
    # from writing .pyc into the image, at a per-run cost instead. The whole
    # .pyc account, tradeoff included, is at the --no-compile comment below.
    # Keep every model download inside the mounted cache volume, never in an
    # image layer, so the Heron weights are fetched once on the host and reused.
    HF_HOME=/models/hf \
    DOCLING_ARTIFACTS_PATH=/models

# ---- native tooling: CPU Tesseract + opencv/pypdfium2 runtime libs --------
# The tessdata packages are mandatory, not cosmetic: docs/adr/0054 freezes fin's
# OCR default to `chi_sim+eng` (src/fin/corpus/ocr.py: DEFAULT_TESSERACT_LANG).
#
# These 8 versions are pinned to the resolutions in apt-packages.lock, and
# pin.py checks this install line against that lock in three directions: every
# token must be version-pinned, every pinned name must be in [requested] at the
# same version, and every [requested] entry must appear here. Pinning only the
# 8 does not pin the layer -- their ~180 transitive dependencies are in the
# image too -- so the lock records the full installed set as measured. That set
# is a record, not a gate: the snapshot mirror and the 8 exact versions are what
# bind the layer.
RUN printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/%s\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/%s\nSuites: trixie-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' \
        "${APT_SNAPSHOT}" "${APT_SNAPSHOT}" > /etc/apt/sources.list.d/debian.sources \
    && rm -f /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20250419 \
        curl=8.14.1-2+deb13u5 \
        libgl1=1.7.0-1+b2 \
        libglib2.0-0=2.84.4-3~deb13u5 \
        libgomp1=14.2.0-19 \
        tesseract-ocr=5.5.0-1+b1 \
        tesseract-ocr-eng=1:4.1.0-2 \
        tesseract-ocr-chi-sim=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/* /var/cache/fontconfig/* /var/cache/ldconfig/aux-cache \
    && rm -f /var/log/apt/history.log /var/log/apt/term.log /var/log/dpkg.log
# The cleanup above is a reproducibility input, not housekeeping. Every path
# purged here was measured to differ between two clean builds of otherwise
# identical committed inputs, so it is generated state, not pinned state.
# fontconfig and ldconfig rebuild their caches on first use and dpkg reopens
# its logs on the next package operation, so nothing at run time needs them
# baked in. The snapshot step below still records the *resolved* package set
# via dpkg-query, which reads dpkg's database and is unaffected.

# Fail the build (not the measurement) if a required tessdata file is missing.
# Checked through the tesseract CLI itself so the exact tessdata prefix of the
# base image does not matter.
RUN tesseract --version \
    && tesseract --list-langs \
    && tesseract --list-langs 2>&1 | grep -qx 'eng' \
    && tesseract --list-langs 2>&1 | grep -qx 'chi_sim'

# ---- the complete Python resolution, hash-locked ---------------------------
# The previous two `pip install` calls named 4 versions and let pip resolve the
# other ~100, which is exactly the L7 drift. --require-hashes makes pip refuse
# any artifact whose sha256 is not in the lock.
#
# The CPU index stays in the lock as --extra-index-url because torch and
# torchvision are +cpu local-version builds served only there; PyPI has no +cpu
# build, so no CUDA wheel can be selected.
#
# download-r2.pytorch.org, which serves those two wheels, is unreachable from a
# build container on this host (measured: 8/8 connect timeouts from inside,
# HTTP 200 from the host), so a build that resolves torch from the index fails
# here. Build hosts that hit that pass --build-arg PIP_FIND_LINKS=<URL or
# container-visible directory>; pip
# reads the variable itself, so the install line below is unchanged. The
# sha256 of each wheel is already committed in requirements.lock, so an artifact
# taken from a local directory is admitted only if it matches the pin exactly.
# See PIN_MANIFEST.md section 5.
ARG PIP_FIND_LINKS
COPY requirements.lock /opt/g4/requirements.lock
# --no-compile suppresses the .pyc bytecode pip would otherwise write while
# installing, and PYTHONDONTWRITEBYTECODE (in the ENV above) keeps the two RUN
# steps that import from writing it either. Those files embed the source path
# and the mtime of the module they were compiled from, so they are derived from
# the install rather than pinned by it and can only ever drift. Measured, not
# assumed: across a pair of clean builds of the pre-repair tree, diff id 7 --
# this install -- was byte-identical (52ac419b81c4…) while diff ids 8 and 9,
# the steps that run Python, differed; that is what the 1,099 .pyc under
# site-packages (of 1,327 in the image) and the 787 that differed between those
# two builds were.
#
# Nothing at run time needs them baked in -- Python regenerates them on first
# import -- but that is not free, and the cost is not in the build. The ENV
# above is permanent, and run_measured.sh starts a fresh container per run
# without overriding it, so no cache survives between runs: the modules behind
# those 1,327 files are recompiled from source on every run, and that compile
# lands inside the wall_clock_ms the driver reports. Build-time determinism is
# bought with a per-run first-import cost inside the measured number. Treat the
# reported wall clock as including it, and do not read a latency change as
# caused by the .pyc change alone -- the image changed size at the same time.
RUN pip install --no-cache-dir --no-compile --require-hashes -r /opt/g4/requirements.lock

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
    && dpkg-query -W -f='${Package}=${Version}\n' > /opt/g4/apt_status.txt \
    && python -c "import sys; print(sys.version)" > /opt/g4/python_version.txt \
    && tesseract --list-langs > /opt/g4/tesseract_langs.txt 2>&1 \
    && tesseract --version > /opt/g4/tesseract_version.txt 2>&1

# driver.py is NOT baked: it lives in the worktree, which run_measured.sh mounts
# read-only. Baking a second copy would create a stale source of truth.
WORKDIR /worktree
ENTRYPOINT ["/bin/bash"]
