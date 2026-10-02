# Issue 324 C1 — BuildKit reproducibility, measured on the full pinned build

Date: 2026-09-30. Operator decision (a): move to BuildKit, establish a new
baseline, keep `8e10185f…` as history, and prove it with two independent clean
builds. Every "Commands" row ran on this branch on this date.

**Outcome: the decision is not achievable as written on this build. The two full
clean builds are NOT byte-identical.** `8e10185f…` stays as history and no new
baseline is declared, because declaring one over a non-reproducible build is
exactly the substitution the issue forbids.

Branch: `worktree-issue-324-g4-reproducible`, base `670f65f` (the PR #321 squash
the issue names).

## What the probe promised, and what the full build delivered

A 2-layer probe over the same digest-pinned base — no apt, no pip, nothing
network-dependent — was used to isolate the builder from every pin variable:

| build path | two clean builds |
| --- | --- |
| legacy `docker build`, `SOURCE_DATE_EPOCH=1700000000` | **differ** (`a0f088b8…` / `ed2ae686…`) |
| BuildKit, `SOURCE_DATE_EPOCH` only | **differ** (`a043eaf9…` / `b5a43a2f…`) |
| BuildKit, `rewrite-timestamp=true` + `SOURCE_DATE_EPOCH` | **identical** (`f409c4cb…`; exported tar sha256 byte-identical `77154a0d…`) |

So the operator's caution that "merely installing buildx is not enough" is
correct, and the specific setting that made the probe reproducible is
`rewrite-timestamp=true`. BuildKit's log confirms it acted:
`#13 rewriting layers with source-date-epoch 1700000000 (2023-11-14 22:13:20 +0000 UTC)`.

**The full pinned build does not reproduce.** Two clean builds of the real
`scripts/g4_pipeline/Dockerfile`:

| | build 1 (`bk1`) | build 2 (`bk2`) |
| --- | --- | --- |
| image id | `sha256:f896868992d2…` | `sha256:8f0009ab766b…` |
| layers | 11 | 11 |
| `created` | `2023-11-14T22:13:20Z` | `2023-11-14T22:13:20Z` |

The `created` timestamps agree — `SOURCE_DATE_EPOCH` is working, which the
legacy builder never achieved. But 2 of the 11 layers differ:

```
5c5
< sha256:3e5e84f3793953fff1a08e57f6fd146a0f54b00292627c361d7f0718732e129f
> sha256:6f20e77cfb2e33e8f942569d8da78934e809a7514a3a3eca8af35e9d41d4e74b
8c8
< sha256:0cf76437b67cc7aebaeb5c8196634f94ff228b1cac2d4408532b3bafe12d7f04
> sha256:0f624597bfabc948ce46548ae07f6444ae57bd409027c2a2ad3bfdeff7c01e1b
```

Mapping the 11 layers (4 inherited base + 7 build steps) onto the Dockerfile,
the differing layers are step 4 and step 7:

- **step 4 — `COPY requirements.lock /opt/g4/requirements.lock`.** The lock file
  is a committed, immutable input. A COPY of immutable content must not vary.
- **step 7 — the snapshot step** (`pip freeze`, `dpkg-query`, python version,
  tesseract langs and version written under `/opt/g4`).

## What the differing content is NOT

Both differing layers were inspected rather than assumed:

| checked | result |
| --- | --- |
| `pip freeze` (104 packages), both images | **identical** — byte-for-byte, `diff` clean |
| `apt_status.txt` (188 packages), both images | **identical** — byte-for-byte, `diff` clean |
| `/opt/g4/*` mtimes, build 1 | all `2023-11-14 22:13:20.000000000 +0000` — rewritten |
| `created` in the image config, both | identical, `2023-11-14T22:13:20Z` |

So the resolution is the same in both builds and the declared timestamps are
honoured. What differs is layer-internal state neither of those checks covers —
`__pycache__` byte-code and similar build-time artifacts written by the
interpreter during the snapshot step, and layer metadata that
`rewrite-timestamp` does not normalise for a `COPY` of a file whose content it
considers unchanged but whose recorded metadata it does not.

**This is not isolated further here.** The remaining work is "make the snapshot
step and the COPY emit identical layers", which is a Dockerfile change plus
another two full builds (each ~20 minutes under the 4 GiB ceiling). It is left
open rather than closed with a re-baseline, because a baseline declared over a
build that does not reproduce would be a weaker contract than the one the
operator asked to keep.

## The 4 GiB ceiling survives the builder change

`docker buildx build` has no `--memory` or `--cpus` flag (verified:
`docker buildx build --help` lists neither; the resource flags that exist are
`--shm-size`, `--ulimit`, `--cgroup-parent`). The ceiling is instead applied to
the BuildKit worker itself, which is stricter than the legacy arrangement —
the whole BuildKit process tree, not just each build step's containers:

```
docker buildx create --name g4repro --driver docker-container \
    --driver-opt memory=4g,network=host --use
docker inspect buildx_buildkit_g4repro0 --format 'mem={{.HostConfig.Memory}} net={{.HostConfig.NetworkMode}}'
→ mem=4294967296 net=host
```

4,294,967,296 bytes — the same 4 GiB the issue requires, enforced on the
builder. Both full builds above ran under it.

`network=host` is also load-bearing for a second reason: the BuildKit container
has no route to `registry-1.docker.io` on its own
(`dial tcp 198.18.5.197:443: i/o timeout`), so a `FROM …@sha256:` cannot
resolve without it.

## A host-level obstacle the build path has to work around

`download-r2.pytorch.org`, which serves `torch` and `torchvision`, is reachable
from the host and unreachable from a build container:

| probe | result |
| --- | --- |
| host, `curl` to the torch wheel | HTTP 200 in 56 s |
| container, same URL, 45 s connect timeout | `curl: (28) Connection timed out` |
| first full build | `ERROR … exit code: 1` after **8/8** `ConnectTimeoutError` on that host only |

Every other network dependency in the build succeeded: the apt snapshot mirror
and all 104 PyPI wheels. Only that one CDN fails from container network.

`Dockerfile` therefore accepts `--build-arg PIP_FIND_LINKS=<dir>`, which pip
reads itself, so the install line stays exactly
`pip install --no-cache-dir --require-hashes -r /opt/g4/requirements.lock` and
the pin contract suite's single-line rule is untouched. The vendored wheels were
verified against the committed lock before use:

```
7417d8c565f219d3455654cb431c6d892a3eb40246055e14d645422de13b9ea1  torch-2.9.1+cpu-…whl
48ff40694812292f1f75cddb517f9eef224a2e8388798a0aa191e27bf159fc5d  torchvision-0.24.1+cpu-…whl
```

both equal to the `--hash=sha256:` values in `requirements.lock`. That is the
pin working on a real download: the artifact was admitted because its digest
matches the declaration, not because it came from a convenient place.

The wheels are deliberately NOT committed. They are 179 MB of upstream
artifacts whose digests are already pinned, and vendoring them into
`scripts/g4_pipeline/` made the model-fetch suite's `copytree` fixture copy
179 MB per test — ten cases filled a 3.9 GB tmpfs. They are injected with
`--build-arg` instead, and `.dockerignore` keeps them out of the build context.

## What is left for the operator

1. **The builder switch (option (a)) is executed; its acceptance is not.** The
   reproducible path — `docker buildx build` with `rewrite-timestamp=true` plus
   `SOURCE_DATE_EPOCH` on the declared builder — is now the only path `build.sh`
   runs, and probe-level determinism holds on it. What is *not* met is option
   (a)'s target: two clean builds of the full pinned Dockerfile are not
   byte-identical, so no new baseline is declared. This round produced the
   measurement that (a) alone is not sufficient and named the two layers that
   still differ. The remaining operator choice is whether to close that gap with
   a Dockerfile change to the two differing layers, not whether to switch builder.
2. **`8e10185f…` remains declared and unmatched.** `build.sh` still exits 75 on
   every build, which is correct: the pipeline cannot prove it built the image
   it declared. No re-baselining was performed.
3. **The two differing layers are located but not fixed.** The next step is a
   Dockerfile change to the snapshot step (writing fewer build-time artifacts,
   or normalising them) plus two more full builds to prove it.

## Commands

| Command | Result |
| --- | --- |
| `docker buildx version` | `github.com/docker/buildx v0.30.1 9e66234aa13328a5e75b75aa5574e1ca6d6d9c01` |
| `docker buildx create --name g4repro --driver docker-container --driver-opt memory=4g,network=host --use` | exit 0 |
| `docker inspect buildx_buildkit_g4repro0 --format 'mem={{.HostConfig.Memory}} net={{.HostConfig.NetworkMode}}'` | `mem=4294967296 net=host` |
| `docker buildx build --no-cache --provenance=false --output type=docker,rewrite-timestamp=true,name=fin-g4-pipeline:bk1 --build-arg PIP_FIND_LINKS=… ` (full pinned Dockerfile) | exit 0, `f896868992d2…` |
| same, `name=…:bk2`, ~20 min later | exit 0, `8f0009ab766b…` |
| `docker image inspect … --format '{{range .RootFS.Layers}}…'` on both, `diff` | 9 of 11 layers identical; steps 4 and 7 differ |
| `docker run … cat /opt/g4/pip_freeze.txt` on both, `diff` | identical, 104 packages |
| `docker run … cat /opt/g4/apt_status.txt` on both, `diff` | identical |
| `docker run … stat /opt/g4/*` on build 1 | every mtime `2023-11-14 22:13:20.000000000 +0000` |

## Test evidence

| suite | result |
| --- | --- |
| `test_g4_pipeline_pin_contract.py` | 65 passed |
| `test_g4_pipeline_gap_contract.py`, `test_attachment_layout_harness.py`, `test_attachment_budgets.py` | 288 passed |
| `test_g4_pipeline_model_fetch_binding.py` | 18 passed |
| `test_g4_pipeline_artifact_hash_contract.py` | 11 passed |
| `test_g4_pipeline_builder_identity_contract.py` | 19 passed |
| all seven together | **401 passed** |

Non-vacuity, measured rather than asserted:

| mutation | result |
| --- | --- |
| remove the `verify-models` cache gate from `run_measured.sh` | **5 failed**, 13 passed |
| remove `revision=` from the `snapshot_download` call | **2 failed**, 16 passed, including `test_the_fetch_program_never_falls_back_to_a_default_revision` |

`pyproject.toml` and `uv.lock` are unchanged. `uv sync --extra ocr` was run so
the real Tesseract gold executes rather than skipping, which changed no
declaration and no committed file.

## Leftovers

- **L1 — full-build byte-identity, open.** Option (a)'s builder switch was
  executed this round; its acceptance — two clean builds of the full pinned
  Dockerfile giving one image id — was not met. The two differing layers are
  named above; closing it needs a Dockerfile change and two more full builds.
- **L2 — the `--revision` fetch binding, closed this round.** `docling-tools
  models download` has no `--revision` for any model kind (measured from its
  `--help`), and `layout_heron_default` resolves to the string `main`, so the
  fetch now goes through `huggingface_hub.snapshot_download(revision=…)` with
  the revisions read out of `models.lock` through `pin.py`'s own parser.
  `verify-models` gates every run, not only after a fetch.
- **L3 — per-run hashing of every reported artifact, closed this round.**
  `driver.py` hashed only paths containing `layout`, so
  `tableformer_accurate.safetensors` (212,758,388 B) was reported with a size
  and no digest. An unmeasured digest is now `sha256: null` plus a named
  `sha256_error`, never a zero.
- **L4 — builder identity, closed as a declared input.** `image-pin.json`
  carries the engine and mode, and `build.sh` compares them before building.
  Note this records the builder the measurements were taken on; it is not a
  claim that the build reproduces anything, which it still does not.
- **L5 — the dedicated 4 GB device, still outstanding and unchanged.** That is
  C2, recorded in `2026-09-29-g4-324-c2-pipeline-gold.md`. Nothing in this round
  discharges it.
