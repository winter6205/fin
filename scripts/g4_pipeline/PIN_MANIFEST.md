# T2a PIN MANIFEST — pinned candidate PDF pipeline + 4 GiB measurement harness

Ticket: `T2a` (measurement / decision ticket). This file records the pin and the
harness that measures against it: the image, the package resolutions, the model
artifact hashes, and the 4 GiB ceiling commands. It is not the only place those
versions appear, and it does not claim to be. Where each one is authoritative:

| where | what it is the source of |
|---|---|
| `image-pin.json` (`expected_image_id`) | **the expected image identity**, declared outside the build output |
| `requirements.lock` | what the build installs, and the bytes of each artifact |
| `apt-packages.lock` | the OS packages the build requests and the versions it actually installed |
| `Dockerfile` | the base image digest and the apt snapshot the lock is anchored to |
| `results/image_pip_freeze.txt` | what pip actually resolved inside the built image |
| per-run `pipeline_identity` in `results/*.json` | the classes, flags, device and thread counts a real run used, read out of the live objects |
| `docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md` | the decision the measurements fed, and the commands that ran |
| this file | why the pin is what it is. Since C1 it is **not** a source of the pin itself — the rows above are |

`pyproject.toml` and `uv.lock` are deliberately **untouched**: docling is **not**
a declared dependency of the repo yet, and adding it is T2d's runtime-dependency
decision. Everything else T2a changed sits outside this directory — the
uploaded-byte and page distribution script, the vision probe, the gold fixtures
under `data/corpus/`, `src/fin/host/attachment_budgets.py` with its tests, the
`.env.example` bounds, the `docs/context-map.md` entry that places the attachment
capability, and the validation record — and is enumerated in the document above.

Everything below was resolved *by the build*, not written from memory: version strings come
from `pip freeze` executed inside the built image (`results/image_pip_freeze.txt`), model
hashes from `sha256sum` on the host cache, and artifact provenance from the HuggingFace Hub
download metadata that the image wrote itself.

---

## 1. Image identity

**This section used to contradict the two files it names as its own source.**
It recorded a legacy image id and `docling-parse 7.21.0`, while
`results/image_identity.txt` recorded `0f278c22…` and
`results/image_pip_freeze.txt` recorded `docling-parse 7.22.1`. Neither
authority is prose; both are committed artifacts. That is L7 in
`docs/validation/2026-09-29-g4-t5-unit-citation-legacy-removal.md`, and it is
the defect the tracked change task C1 set out to remove. That legacy digest is
`8e10185f…`, and it is unreachable by any current builder; *Builder
reproducibility* below is where it is recorded as history, because a value
this section still calls the expectation would be a live claim.

**Reconciliation (C1).** Three values, and they are not the same kind of thing:

| value | where it lives | what it is |
|---|---|---|
| `sha256:b1b5270c…` | `image-pin.json` → `expected_image_id` | the **declared expectation** — what a rebuild must reproduce, adopted by the operator 2026-10-02 |
| `sha256:0f278c22…` | `results/image_identity.txt` | the **observed identity** of a rebuild that drifted |
| `python:3.12-slim@sha256:44ff437b…` | `Dockerfile` | the **digest-pinned base** the pin is now anchored to |

`b1b5270c…` is the expectation, and it is **not** `0f278c22…`: the tracked change forbids
silently replacing the expected digest, and `0f278c22…` is the very artifact
whose drift was the open defect, so adopting it would launder the bug into the
contract. `b1b5270c…` was adopted instead because two independent clean builds
of this Dockerfile produced it identically — see *Builder reproducibility* below
for the measurements. The expected value and the committed snapshot still
**disagree**, so `build.sh` still **fails**, with exit 75: that disagreement is
now the recorded `0f278c22…` rebuild of the pre-repair tree, not an undecided
baseline. The honest state, not a defect in the verifier.

| field | value |
|---|---|
| tag | `fin-g4-pipeline:r1` |
| declared expected image id | `sha256:b1b5270c…` (in `image-pin.json`) |
| observed image id of the last build | `sha256:0f278c22…` (in `results/image_identity.txt`, the pre-repair rebuild) |
| **agreement** | **none — the check still fails, but on a recorded drift, not an undecided baseline** |
| arch | `amd64` |
| base image | `python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b` |
| builder | buildx builder declared in `image-pin.json` (name `g4repro`, driver docker-container, BuildKit v0.32.2, `memory="4g"`, `network="host"`); `build.sh` compares it against the live host before building. Formerly the Docker CLI 29.1.3 **legacy** builder — retired by the operator's option (a) decision, history below. |
| builder reproducibility | the declared buildx path **reproduces** the full pinned image: two independent clean builds since the `--no-compile` fix agreed on `b1b5270c…`, 11 of 11 layers (see §1a) |
| Python | `3.12.14 (main, Sep 19 2026, 01:04:57) [GCC 14.2.0]` |

`size_bytes` and `created` are no longer tabulated here. Both are wall-clock
values the legacy builder writes into the image config, so neither is
reproducible and neither is a property of the pin; they live only in
`results/image_identity.txt`.

> **`repo_digests` in `results/image_identity.txt` is not registry provenance.**
> An earlier `build.sh` recorded `docker image inspect .RepoDigests`, and for a
> locally built, never-pushed image that field echoes the image ID back. It was
> dropped rather than quoted, because treating it as a registry digest would
> make a local build look attested. This image is never pushed.

### Builder reproducibility — the measured blocker

The legacy builder **cannot** produce the byte-identical image id the issue
asks for, from any amount of input pinning. Two clean `--no-cache` builds of a
deliberately trivial 2-layer Dockerfile over the same digest-pinned base:

| | build 1 | build 2 |
|---|---|---|
| image id | `sha256:a0f088b8…` | `sha256:ed2ae686…` |
| `created` | `2026-09-29T20:55:47.408482569+08:00` | `2026-09-29T20:56:57.577821291+08:00` |
| base layers (4) | byte-identical | byte-identical |
| `RUN` layer diffID | `sha256:6901e9d2…` | `sha256:08c4005e…` |

The cause is isolated, not inferred: the 4 base layers hash identically in both
builds, the `RUN` layer's **file contents are byte-identical** (`diff -r` clean)
and only its mtimes differ by exactly the build gap, and the image config differs
in `created`, in each `history[].created`, and in `rootfs.diff_ids` — with both
configs measuring exactly 6025 bytes, i.e. the same bytes with a different
timestamp substituted. `SOURCE_DATE_EPOCH=1700000000` changes nothing: two
builds with it set produced `sha256:1b0668ab…` and `sha256:09e027d4…`.

So the timestamp is written into both the layer tar and the config, and the
config is what the image ID hashes. This is a property of the builder, not of
the pin. Everything the pin *can* control — base digest, OS packages, Python
resolution, model revisions — is controlled and checked. The one thing it cannot
control is the builder's clock, which is why C1 is blocked rather than complete.

**Follow-up, 2026-09-30: the operator chose option (a) — buildx/BuildKit.** The
record (`docs/validation/2026-09-30-g4-buildkit-reproducibility.md`) measured
that BuildKit with `rewrite-timestamp=true` plus `SOURCE_DATE_EPOCH` makes clean
probe builds byte-identical, while the *full* build then in use still differed
in 2 of 11 layers; the differing layer content was traced to pip's build-time
`.pyc` files and suppressed with `--no-compile` on the install line. Re-verified
on this branch the same day: two clean buildx builds of a 2-layer probe gave the
same manifest digest, and two clean buildx builds of a probe shaped like the
real steps (COPY lock, `pip install --no-cache-dir --no-compile
--require-hashes`, timestamp-free snapshot writes) also gave one shared digest.
A deliberately non-reproducible variant (a snapshot file recording `stat`
mtimes) diverged in exactly that layer, confirming the mechanism reads the
build clock only through file *contents* now. `build.sh` therefore builds
through the declared buildx builder. What is NOT closed: two clean builds of
the full pinned Dockerfile on this path have not been run since `--no-compile`,
so no new baseline is declared and `8e10185f…` (a legacy-builder artifact)
remains declared and unmatched — the build fails the identity check by design
until the operator accepts a baseline proved by those two full builds.

**Superseded, 2026-10-02.** That last paragraph is the 2026-09-30 state. The
two full builds have since been run: they agreed on `b1b5270c…`, and the
operator adopted it as `expected_image_id`, so §1 is the current statement and
this subsection is history. The legacy digest stays unreachable either way.

**Builder provenance is mechanical.** the tracked change asks for package, OS, base,
builder, model and source provenance to be checkable "without prose-only
pins". All six are now declared in `image-pin.json` / lock files and verified
before or after each build. The builder block names engine, buildx builder,
driver, BuildKit version, memory ceiling, network and SOURCE_DATE_EPOCH, and
`pin.py`'s `check-builder` (fed by `build.sh` with the live host's engine, mode,
and `buildx inspect` output) refuses to build on a host where any of
them differs (exit 65). Earlier text here described this as prose-only; that
gap was closed by the builder-identity contract in
`tests/host/test_g4_pipeline_builder_identity_contract.py`.
Declaring the engine is mechanical work this task did not complete, and it is
listed as such in the validation record rather than left to look covered.

## 2. System packages (apt)

Mechanically pinned in **`apt-packages.lock`**, not by this prose. The previous
version of this section said `python:3.12-slim` *bookworm*; the digest-pinned
base is Debian 13 **trixie**, and the tag had moved between the two without any
edit here. That single drift is why the base is now pinned by digest.

| field | value | where |
|---|---|---|
| mirror | `snapshot.debian.org` @ `20260929T083940Z` | `apt-packages.lock` `[mirror]` |
| requested (8) | `ca-certificates`, `curl`, `libgl1`, `libglib2.0-0`, `libgomp1`, `tesseract-ocr`, `tesseract-ocr-eng`, `tesseract-ocr-chi-sim` | `[requested]`, each `=version` |
| installed (188) | every package in the image, dependencies included | `[installed]` |

The 8 requested packages are pinned to exact versions in the `Dockerfile`, and
`pin.py` closes that loop: it rejects an install line carrying any unpinned
token, any pinned name absent from `[requested]` or present at a different
version, and any `[requested]` entry missing from the line. Their
~180 transitive dependencies are **not** in the Dockerfile, because pinning each
would make the build unreadable and apt would still resolve them; instead the
full installed set is committed, so what the build actually installed stays
comparable without a container. That set is a record rather than a gate.
Trixie renamed `libglib2.0-0` to `libglib2.0-0t64` and kept
the old name as a virtual provide, so `pin.py` resolves that suffix rather than
reporting a pinned package as missing.

Tesseract is still recorded by measurement as well as by pin:
`tesseract --version` is baked into the image at `/opt/g4/tesseract_version.txt`
and copied out to `results/image_tesseract_version.txt`, so the `tesseract 5.5.0`
cited by the validation record is readable from a committed artifact (leptonica
1.84.1, AVX2/FMA/SSE4.1 and OpenMP 201511 found).

OCR language guard (runs at build time and aborts the build on failure):

```
$ tesseract --list-langs        # inside the image
List of available languages in "/usr/share/tesseract-ocr/5/tessdata/" (3):
chi_sim
eng
osd
```

`eng` and `chi_sim` are both present, which is what
`fin.corpus.ocr.DEFAULT_TESSERACT_LANG = "chi_sim+eng"` requires.

## 3. Python pins

**The resolution is mechanically pinned in `requirements.lock`, not by the table
this section used to carry.** All 104 packages are `==`-pinned with a
`--hash=sha256:` for every artifact pip may select, and the Dockerfile installs
that one file under `--require-hashes`. Previously 4 packages carried an explicit
`==` and ~100 were labelled "transitive" — meaning unpinned, which is precisely
how `docling-parse` moved 7.21.0 → 7.22.1, `Faker` 40.39.0 → 40.40.0 and `regex`
2026.9.10 → 2026.9.29 between two builds. Under `--require-hashes` a wheel that
changes upstream now **fails the build** instead of silently changing the image.

The table below is retained as a reading aid for the packages the measurement
actually depends on, not as the pin. `docling-parse` is corrected to `7.22.1`,
the value in `results/image_pip_freeze.txt` and `requirements.lock`; the `7.21.0`
above was the prose drift L7 recorded.

| package | resolved version | why the measurement needs it |
|---|---|---|
| docling | `2.130.0` | the measured pipeline |
| docling-slim | `2.130.0` | docling meta-package |
| docling-core | `2.99.0` | docling core |
| docling-ibm-models | `4.0.3` | docling models |
| docling-parse | `7.22.1` | `ThreadedDoclingParseDocumentBackend`, the selected PDF backend |
| torch | `2.9.1+cpu` | CPU-only layout engine (`+cpu` local version = CPU index) |
| torchvision | `0.24.1+cpu` | torch vision ops |
| transformers | `5.17.0` | model loading |
| opencv-python | `5.0.0.93` | image ops for the render path |
| pypdfium2 | `5.13.0` | the render/text backend used by the driver |
| pytesseract | `0.3.13` | fin's OCR front-end |
| numpy | `2.5.2` | numeric base |
| scipy | `1.18.1` | scipy ops |
| pillow | `12.3.0` | image I/O |
| huggingface_hub | `1.33.0` | artifact download |
| hf-xet | `1.6.0` | transport **disabled** at run time, see §7 |
| safetensors | `0.8.0` | weight format |
| accelerate | `1.15.0` | device placement |
| rapidocr | `3.9.2` | present but *unused* (fin's OCR path is Tesseract) |
| onnxruntime | **absent** | confirmed: neither the lock nor `pip freeze` has it ⇒ Docling's ONNX engine override for heron is not usable, so layout runs on the **torch** engine |

The CPU wheel index is declared in the lock as `--extra-index-url`, and torch and
torchvision are pinned to their `+cpu` local versions. PyPI publishes no `+cpu`
build, so no CUDA wheel can be selected — a separate build-time guard also asserts
`not torch.cuda.is_available()` and aborts the build otherwise.

`requirements.lock` is generated from `results/image_pip_freeze.txt` by
`gen_requirements_lock.py`; regenerate both together and commit them in one
commit. Full 104-line resolution: `results/image_pip_freeze.txt` (also baked
in-image at `/opt/g4/pip_freeze.txt`).

## 4. Pipeline actually exercised (no VLM, no local model beyond layout+table)

```
docling.document_converter.DocumentConverter
  format_options={InputFormat.PDF: PdfFormatOption(
      pipeline_options=PdfPipelineOptions(...),
      backend=ThreadedDoclingParseDocumentBackend)}
  -> StandardPdfPipeline
     LayoutObjectDetectionOptions.from_preset("layout_heron_default")   # repo_id docling-project/docling-layout-heron
     TableStructureOptions(do_table_structure=True)                     # docling-project/docling-models v2.3.0, "accurate"
     AcceleratorOptions(device="cpu", num_threads=4)
     do_ocr=False                     # OCR is fin's own per-page Tesseract path, measured separately
     do_picture_classification=False  # forbidden by ADR 0089
     do_picture_description=False     # forbidden by ADR 0089
     do_code_enrichment=False         # forbidden by ADR 0089
     do_formula_enrichment=False      # forbidden by ADR 0089
```

The concrete class names, model spec (`name`/`repo_id`/`revision`), engine overrides, all
`do_*` flags, device and thread counts are re-emitted per run inside
`pipeline_identity` of the measurement JSON — they are read out of the objects, not asserted
from this document.

## 4a. What is NOT in the image digest

An image id covers the layers and the config. It does **not** cover anything
mounted at run time, so a reproducible image id is necessary but not sufficient
to say a measurement is reproducible. These inputs are named here so a reader
does not assume the digest covers them:

| input | how it reaches the run | why it is outside the image |
|---|---|---|
| **Model weights** (Heron, TableFormer — §5) | host dir `~/.cache/fin-g4-docling-models` → `/models` | fetched by `huggingface_hub.snapshot_download(revision=…)` on the host, revision read from `models.lock` (§5), never baked; `HF_HOME=/models/hf` points out of the image |
| **Source revision** | worktree → `/worktree:ro`, `PYTHONPATH=/worktree/src` | `driver.py` and all of `src/fin` are mounted read-only; the image contains no `fin` code |
| **Gold PDF bytes** | `--volume .../results/<run>:/out` and the caller's PDF path | inputs to the measurement, not to the build |
| **Harness scripts** (`driver.py`, `run_measured.sh`) | the same worktree mount | deliberately not baked, to avoid a second stale copy |
| **Host cgroup state** | `--memory/--memory-swap/--cpus` at `docker run` | §6 |

The gold-set fixture bytes are committed under `data/corpus/` and are covered by
their own manifest hashes, not by the image id.

**Closed (83b53ba).** The driver's `artifact_identity` now hashes **every
reported artifact**, not only paths containing `layout`: an over-1 MB file such
as `tableformer_accurate.safetensors` (212,758,388 B ≤ the 2 GiB hash ceiling)
is read and gets its own `sha256` in `heron_artifact.files[]`, so a swap made
*after* the build gate and *during* a measurement shows up as a digest
mismatch in that run's record rather than passing unseen. An artifact whose
digest could not be measured is emitted as `sha256: null` plus a named
`sha256_error` — never a silent zero or an absent key. The build-time gate is
unchanged: `pin.py verify-models` still hashes each payload itself and both
`build.sh` (a cache must be present or the build stops with exit 75) and
`run_measured.sh` (an unbound or mismatched cache stops the run with exit 75)
check the cache against `models.lock` before any measurement.

## 5. Heron / TableFormer artifact pins (host-mounted cache, never baked into the image)

Host cache root: `<host-model-cache>` (a local directory such as `$HOME/.cache/fin-g4-docling-models`) → mounted read-write at
`/models` (`DOCLING_ARTIFACTS_PATH=/models`, `HF_HOME=/models/hf`).
Measured size of the cache after a complete fetch: **701,236,013 bytes** (`du -sb`),
**70 files**, `670M` on disk (`du -sh`).

| artifact | HF repo @ revision | resolved commit | file | bytes | sha256 |
|---|---|---|---|---|---|
| layout **Heron** (torch engine) | `docling-project/docling-layout-heron` @ `main` | `8f39ad3c0b4c58e9c2d2c84a38465abf757272d8` | `docling-project--docling-layout-heron/model.safetensors` | `171658996` | `00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c` |
| TableFormer **accurate** | `docling-project/docling-models` @ `v2.3.0` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23` | `.../model_artifacts/tableformer/accurate/tableformer_accurate.safetensors` | `212758388` | `2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9` |
| TableFormer **fast** (fetched as a pair by `docling-tools models download tableformer`; not used — the preset selects `accurate`) | `docling-project/docling-models` @ `v2.3.0` | `fc0f2d45e2218ea24bce5045f58a389aed16dc23` | `.../tableformer/fast/tableformer_fast.safetensors` | `145453276` | `3119563aab5a7c96fda4d621119b63fd8806272b86c30936d15507616422f718` |
| heron **onnx** (present in cache, **inactive** — no onnxruntime installed) | `docling-project/docling-layout-heron-onnx` @ `main` | `40bde044036bb181c130ddf6c51792187268748f` | `.cache/.../model.onnx` | `171220471` | `59c81a3a2923042d85034ffc487f8f47e4854117e879aef89b2b9f728fb4922a` |

**Honest HuggingFace verification.** Provenance was not assumed. For every artifact the
image's own HF-Hub download metadata was read back
(`<artifact>/.cache/huggingface/download/<file>.metadata`, line 1 = commit hash,
line 2 = Hub ETag), and the Hub ETag for these LFS files is the SHA-256 of the payload:

```
docling-project--docling-layout-heron/.cache/huggingface/download/model.safetensors.metadata
  8f39ad3c0b4c58e9c2d2c84a38465abf757272d8
  00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c   <- ETag
docling-project--docling-models/.cache/.../tableformer_accurate.safetensors.metadata
  fc0f2d45e2218ea24bce5045f58a389aed16dc23
  2a7d6c924b3cd12fb99a09280ca9c33a89c5d60b93253617d2e088c1a40374d9   <- ETag
```

Both ETags equal the independently computed `sha256sum` of the local file, so the weights
really came from `huggingface.co` and were not silently satisfied from a mirror or a
pre-existing directory. The driver additionally re-hashes every reported artifact
over 1 MB on every run, layout and TableFormer alike (`heron_artifact` in the
JSON) — the hash above is that measured value.

### Model revisions are pinned and the fetch is bound to them

The revisions above are now mechanical, in **`models.lock`**: every artifact is
declared at a 40-character commit, and `pin.py parse_models_lock` **rejects**
`main`, `master`, `HEAD` and `latest` outright. Heron moves from the branch it
was recorded against (`revision_was = main`) to
`8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`.

`pin.py verify-models --pin models.lock --cache-root <dir>` checks the fetched
weights against that file, reading the HuggingFace download metadata the fetch
itself wrote (line 1 = commit, line 2 = Hub ETag, which for LFS files is the
payload's SHA-256). A missing or empty metadata file is reported as a failure,
never as a match — the same `-1`-is-not-`null` discipline the driver applies to
cgroup readings. `build.sh` runs it on every build and **refuses to proceed when
no model cache is present** (exit 75): a cold host stops at the gate rather than
building unverified.

**The fetch is bound to a revision (83b53ba).** `run_measured.sh` reads the
revisions out of `models.lock` through `pin.py`'s own parser and fetches each
`repo@commit` with `huggingface_hub.snapshot_download(revision=…)`. This
replaces the earlier `docling-tools models download`, which has no `--revision`
for any model kind and so landed a fresh host on whatever `main` pointed at
today rather than on `8f39ad3c…`. The plan is a function of the declaration:
editing a pin changes the fetch, and nothing on the command line can drift it
back to a branch. After the fetch, and on **every** run whether or not a fetch
happened, `verify-models` gates the cache against `models.lock`; a mismatch stops
the run with exit 75 before any measurement.

Fetch command as it stands today (idempotent preamble in `run_measured.sh`, one
`snapshot_download` per distinct `repo@revision` read from `models.lock`, run
**under the same ceiling**; the payload verification above is what enforces the
pin, since `snapshot_download` checks the Hub ETag but the harness re-checks it
against `models.lock`):

```
docker run --rm --memory=4g --memory-swap=4g --cpus=4 --user 1000:1000 --env HOME=/tmp \
  --env HF_HOME=/models/hf --env DOCLING_ARTIFACTS_PATH=/models \
  --env HF_HUB_DISABLE_XET=1 --env HF_HUB_DOWNLOAD_TIMEOUT=180 --env HF_HUB_ETAG_TIMEOUT=120 \
  --volume <host-model-cache>:/models \
  --entrypoint /usr/local/bin/python fin-g4-pipeline:t2a - \
  docling-project/docling-layout-heron@8f39ad3c0b4c58e9c2d2c84a38465abf757272d8:/models/docling-project--docling-layout-heron \
  docling-project/docling-models@fc0f2d45e2218ea24bce5045f58a389aed16dc23:/models/docling-project--docling-models \
  <<'PY'
from huggingface_hub import snapshot_download
import sys
for spec in sys.argv[1:]:
    repo, rest = spec.split("@", 1)
    revision, local_dir = rest.split(":", 1)
    snapshot_download(repo_id=repo, revision=revision, local_dir=local_dir)
PY
```

## 6. Hard 4 GB ceiling — verbatim commands

Build (the ceiling is enforced on the BuildKit builder itself, so any step that
cannot fit in 4 GiB fails here rather than misleadingly succeeding):

```
docker buildx create --name g4repro --driver docker-container \
  --driver-opt memory=4g,network=host --use
SOURCE_DATE_EPOCH=1700000000 docker buildx build \
  --builder g4repro --no-cache --provenance=false --sbom=false \
  --output type=docker,rewrite-timestamp=true,name=fin-g4-pipeline:t2a \
  --file scripts/g4_pipeline/Dockerfile scripts/g4_pipeline
```

(`build.sh` runs the second command with the builder name, epoch and flags read
from `image-pin.json`, after gating the live builder against that declaration.
The legacy `docker build --memory/--cpuset-cpus` form is retired: `docker
buildx build` has no per-build memory flag — the ceiling moves to the builder's
driver-opt, which bounds the whole BuildKit process tree, and `network=host` is
load-bearing because the BuildKit container otherwise cannot resolve the
digest-pinned base. The former note "`docker buildx` is not installed" is out
of date as of the option (a) decision.)

Run (the real, literal per-measurement command shape; `run_measured.sh` prints the fully
expanded instance for every run):

```
docker run \
  --name fin-g4-t2a-<pdf>-<pid> \
  --memory=4g \
  --memory-swap=4g \
  --cpus=4 \
  --user 1000:1000 \
  --env HOME=/tmp \
  --env HF_HOME=/models/hf \
  --env TORCH_HOME=/models/torch \
  --env DOCLING_ARTIFACTS_PATH=/models \
  --env HF_HUB_DISABLE_TELEMETRY=1 \
  --env HF_HUB_DISABLE_XET=1 \
  --env PYTHONPATH=/worktree/src \
  --volume <fin-checkout>:/worktree:ro \
  --volume <...>/scripts/g4_pipeline/results/<run>:/out \
  --volume <host-model-cache>:/models \
  --entrypoint python fin-g4-pipeline:t2a \
  /worktree/scripts/g4_pipeline/driver.py --pdf <pdf> --artifacts /models --write-images --image-dir /out/pages
```

Ceiling is **never** raised, removed, or bypassed by falling back to host execution. The
`--memory` value is a cgroup v2 `memory.max` of exactly `4294967296` bytes, verified from
*inside* the container by the driver (`cgroup_memory_max_bytes` in every JSON line).

### Known ceiling caveat on this host (reported, not worked around)

```
WARNING: Your kernel does not support swap limit capabilities or the cgroup is not mounted.
         Memory limited without swap.
```

The WSL2 kernel in use does not enforce a container swap limit, so `--memory-swap=4g` is
accepted but **not applied**: `memory.swap.max` reads `max` (the driver records that as
`cgroup_memory_swap_max_bytes: -1`). Swap *usage* is still accounted, and it is nonzero where
it matters. `memory.peak` counts memory charged to the cgroup, not the bytes the kernel moved
out, so on this host the 4 GiB-relevant footprint of a run is its `memory.peak` plus its swap,
and `VmSwap` has to be read rather than assumed away. Sixteen of the twenty metas tracked under
`results/` do report `proc_vmswap_bytes = 0` and `cgroup_memory_swap_current_bytes = 0` — the
nine `2023_FY_GOOG.pdf` runs (`…T164419Z`, `…T170133Z`, `…T170956Z`, `…T184410Z`, `…T195408Z`,
`…T204930Z`, `…T220516Z`, `…T231119Z`, `…T000155Z`), the five `g4-mixed-chart-001` runs
(`…T174114Z`, `…T204952Z`, `…T220537Z`, `…T231154Z`, `…T000242Z`) and both `g4-scanned-001`
runs (`…T185551Z`, `…T205348Z`), all of them one to three
pages. The scanned pair is the whole-tree case worth naming: its 63 to 68 minutes of
render-plus-OCR work runs mostly in a Tesseract child process, and
`memory.swap.current` still read 0 at the end of each run. The three non-PDF
rejection metas (`g4-image-chart-001.png…T195253Z`, `…T231351Z`, `…T001448Z`) each record
`null` for both because they measured nothing.
The load-bearing run is the exception:
`2023_Q1_MSFT_20260925T171056Z.meta.json`, the 72-page conversion, reports
`proc_vmswap_bytes = 182738944` with `cgroup_memory_swap_current_bytes = 275591168`. Both are
end-of-run readings rather than peaks — the driver has no swap counter to peak on here — so the
byte total that conversion put under the ceiling is at least resident `VmHWM` 3,324,112,896 plus
182,738,944 swapped. That is why the validation record computes its margin from
resident-plus-swap and why no cap here may be read off `memory.peak` alone. A host that enforces
the swap half of the ceiling should re-run this harness before the caps are frozen.

## 7. Environment findings that constrain reproducibility

1. **Docling 2.130.0 does not auto-download weights when `artifacts_path` is set.** With
   `DOCLING_ARTIFACTS_PATH=/models` and an empty cache the layout model raises
   `FileNotFoundError: Model 'docling-project/docling-layout-heron' not found in artifacts_path`.
   The harness therefore carries the idempotent revision-bound fetch preamble
   in §5 (`huggingface_hub.snapshot_download`, read from `models.lock`). This is
   a real property of the pin, not a quirk of this host.
2. **`hf-xet` 1.6.0 transport times out from this network** (`LocalEntryNotFoundError` /
   `ConnectTimeout`). `HF_HUB_DISABLE_XET=1` forces plain HTTPS, which works.
3. **Default HF download timeouts (10 s) are too short for a 171 MB/213 MB LFS payload here**;
   `IncompleteSnapshotError: ... 2 file(s) are missing ... The Hub could not be reached`
   was raised until `HF_HUB_DOWNLOAD_TIMEOUT=180 HF_HUB_ETAG_TIMEOUT=120` were set.
4. **Docling's own page/size limits are unbounded by default.**
   `docling_builtin_limits` in the measurement JSON reports
   `max_num_pages = 9223372036854775807` and `max_file_size = 9223372036854775807`.
   T2a's finite upload/page/vision-call budgets therefore cannot be inherited from docling;
   fin must impose them.

## 8. Files in this directory

| file | role |
|---|---|
| `Dockerfile` | pinned runtime (docling + CPU torch + tesseract eng/chi_sim) on a **digest-pinned base**, apt from a **snapshot mirror at exact versions**, Python from `requirements.lock` under `--require-hashes` |
| `build.sh` | ceiling-constrained build. Orchestration only: it validates the declared inputs, builds, records snapshots via temp-file-then-`mv`, and delegates the identity check to `pin.py`. Exits 65 on an invalid pin (nothing built), 75 on identity drift |
| `pin.py` | every rule that can fail about the declared inputs, in one testable unit with no docker. `self-check` and `compare-image-id` subcommands; raises rather than defaulting, so an unreadable lock is a named failure and never an empty one |
| `image-pin.json` | the **declared expected image id**, read by `build.sh` and never written by it. Holding the expectation here is what keeps the drift check from being true by construction |
| `requirements.lock` | the full 104-package Python resolution, each `==`-pinned with `--hash=sha256:` for every artifact pip may select |
| `gen_requirements_lock.py` | regenerates `requirements.lock` from `results/image_pip_freeze.txt` by resolving hashes from PyPI and the PyTorch CPU index. Network-using and **not** part of the default test suite; the build never calls it |
| `apt-packages.lock` | the apt snapshot timestamp, the 8 requested package versions, and all 188 installed versions |
| `models.lock` | each host-mounted model artifact at a **40-character commit** plus its payload sha256. `pin.py verify-models` checks the fetched weights against it and refuses `main`/`master`/`HEAD`/`latest` outright |
| `driver.py` | in-container measurement driver; emits exactly one JSON line, logs to stderr |
| `run_measured.sh` | ceiling-constrained `docker run` wrapper; merges `docker inspect` OOMKilled with its own out-of-band read of the container scope's `memory.peak` / `memory.swap.peak`. That host-side cross-check read nothing on this host — `host_side_cgroup_memory_peak_bytes` and `host_side_cgroup_memory_swap_peak_bytes` are `null` in every committed meta — so every peak here comes from the driver's in-container reads. It writes the measured line whether or not the run succeeded, then **exits non-zero** when `docker run` failed or the container exited non-zero, naming `docker_run_exit`, `container_exit` and any `state_error` on stderr, so a captured record can never be read as a succeeded run from the exit status alone |
| `PIN_MANIFEST.md` | this file — why the pin is what it is. It is **not** the pin; §1–§3 now describe inputs that are mechanically enforced elsewhere |
| `results/` | build snapshots (`image_*.txt`) + one dir and one `<name>.measured.jsonl` per measured run |

This directory is the harness only. It was built and run before the rest of
T2a, and the ticket's later work — gold fixtures under `data/corpus/`, the vision
probe against the configured provider, and the `tests/host` suite — is recorded in
the validation document above, not here.
