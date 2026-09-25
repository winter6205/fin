# T2a PIN MANIFEST — pinned candidate PDF pipeline + 4 GiB measurement harness

Ticket: `T2a` (measurement / decision ticket). This file records the pin and the
harness that measures against it: the image, the package resolutions, the model
artifact hashes, and the 4 GiB ceiling commands. It is not the only place those
versions appear, and it does not claim to be. Where each one is authoritative:

| where | what it is the source of |
|---|---|
| `Dockerfile` (`ARG DOCLING_VERSION`, the two `==…+cpu` requirements) | what the build installs |
| `results/image_pip_freeze.txt` | what pip actually resolved inside the built image |
| per-run `pipeline_identity` in `results/*.json` | the classes, flags, device and thread counts a real run used, read out of the live objects |
| `docs/validation/2026-09-26-g4-t2a-page-vision-budgets.md` | the decision the measurements fed, and the commands that ran |
| this file | why the pin is what it is, and the guard commands that prove it |

`pyproject.toml` and `uv.lock` are deliberately **untouched**: docling is **not**
a declared dependency of the repo yet, and adding it is T2d's runtime-dependency
decision. Everything else T2a changed sits outside this directory — the
uploaded-byte and page distribution script, the vision probe, the gold fixtures
under `data/corpus/`, `src/fin/host/attachment_budgets.py` with its tests, the
`.env.example` bounds, and the validation record — and is enumerated in the
document above.

Everything below was resolved *by the build*, not written from memory: version strings come
from `pip freeze` executed inside the built image (`results/image_pip_freeze.txt`), model
hashes from `sha256sum` on the host cache, and artifact provenance from the HuggingFace Hub
download metadata that the image wrote itself.

---

## 1. Image identity

| field | value |
|---|---|
| tag | `fin-g4-pipeline:t2a` |
| image id | `sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800` |
| created | `2026-09-26T00:41:58.341799675+08:00` |
| content size | `681909257` bytes (docker `Size`: 2.93 GB uncompressed) |
| arch | `amd64` |
| base image | `python:3.12-slim` |
| Python | `3.12.14 (main, Sep 19 2026, 01:04:57) [GCC 14.2.0]` |

Source of these values: `docker image inspect` snapshot in `results/image_identity.txt`.

## 2. System packages (apt, `python:3.12-slim` bookworm)

```
ca-certificates curl libgl1 libglib2.0-0 libgomp1
tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim
```

Tesseract is **not** version-pinned by apt on this base; the installed binary is recorded by
measurement instead: `tesseract 5.5.0` (baked to `/opt/g4/tesseract_version.txt`).

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

## 3. Python pins — the packages this measurement depends on

Install order matters and is baked into the Dockerfile: torch/torchvision are resolved from
the **CPU-only wheel index first**, then docling is installed with
`--extra-index-url https://download.pytorch.org/whl/cpu` so pip can never substitute the
default CUDA build.

| package | resolved version | pinned as |
|---|---|---|
| docling | `2.130.0` | `docling==2.130.0` (explicit) |
| docling-slim | `2.130.0` | transitive (docling meta-package) |
| docling-core | `2.99.0` | transitive |
| docling-ibm-models | `4.0.3` | transitive |
| docling-parse | `7.21.0` | transitive |
| torch | `2.9.1+cpu` | `torch==2.9.1+cpu` (explicit, `+cpu` local version = CPU index) |
| torchvision | `0.24.1+cpu` | `torchvision==0.24.1+cpu` (explicit) |
| transformers | `5.17.0` | transitive |
| opencv-python | `5.0.0.93` | transitive |
| pypdfium2 | `5.13.0` | transitive (also the render/text backend used by the driver) |
| pytesseract | `0.3.13` | `pytesseract==0.3.13` (explicit) |
| numpy | `2.5.2` | transitive |
| scipy | `1.18.1` | transitive |
| pillow | `12.3.0` | transitive |
| huggingface_hub | `1.33.0` | transitive |
| hf-xet | `1.6.0` | transitive (transport **disabled** at run time, see §7) |
| safetensors | `0.8.0` | transitive |
| accelerate | `1.15.0` | transitive |
| rapidocr | `3.9.2` | transitive — present but *unused* (fin's OCR path is Tesseract) |
| onnxruntime | **absent** | confirmed: `pip freeze` has no onnxruntime ⇒ Docling's ONNX engine
  override for heron is not usable, so layout runs on the **torch** engine |

CPU-wheel guard (runs at build time, aborts the build otherwise):

```
python -c "import torch,sys;
  ok = 'cpu' in torch.__version__ and not torch.cuda.is_available(); ..."
```

Full 104-line resolution: `results/image_pip_freeze.txt` (also baked in-image at
`/opt/g4/pip_freeze.txt`).

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

## 5. Heron / TableFormer artifact pins (host-mounted cache, never baked into the image)

Host cache root: `/home/winner/.cache/fin-g4-docling-models` → mounted read-write at
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
pre-existing directory. The driver additionally re-hashes any path containing `layout`
on every run (`heron_artifact` in the JSON) — the hash above is that measured value.

Fetch command (idempotent preamble in `run_measured.sh`, run **under the same ceiling**):

```
docker run --rm --memory=4g --memory-swap=4g --cpus=4 --user 1000:1000 --env HOME=/tmp \
  --env HF_HOME=/models/hf --env DOCLING_ARTIFACTS_PATH=/models \
  --env HF_HUB_DISABLE_XET=1 --env HF_HUB_DOWNLOAD_TIMEOUT=180 --env HF_HUB_ETAG_TIMEOUT=120 \
  --volume /home/winner/.cache/fin-g4-docling-models:/models \
  --entrypoint /usr/local/bin/docling-tools fin-g4-pipeline:t2a \
  models download --output-dir /models layout tableformer
```

## 6. Hard 4 GB ceiling — verbatim commands

Build (the ceiling is applied to build steps too, so a pip install that cannot fit in 4 GiB
fails here rather than misleadingly succeeding):

```
docker build \
  --memory=4g \
  --memory-swap=4g \
  --cpuset-cpus=0-3 \
  -t fin-g4-pipeline:t2a \
  scripts/g4_pipeline
```

(`docker build` on this docker CLI's legacy builder has no `--cpus`, hence `--cpuset-cpus=0-3`
for the 4-core equivalent. `docker buildx` is not installed.)

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
  --volume /home/winner/projects/fin-wt-g4-attachment:/worktree:ro \
  --volume <...>/scripts/g4_pipeline/results/<run>:/out \
  --volume /home/winner/.cache/fin-g4-docling-models:/models \
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

The WSL2 kernel in use does not enable `CONFIG_MEMCG_SWAP` accounting, so `--memory-swap=4g`
is accepted but **not enforced**: `memory.swap.max` reads `max` (the driver records that as
`cgroup_memory_swap_max_bytes: -1`). The practical consequence for T2a's budget decision is
mitigated by observation rather than assumption: every measured run reports
`VmSwap = 0` bytes and `memory.swap.current = 0`, i.e. the workload never swapped, so
`memory.peak` *is* the true 4 GiB-relevant footprint. A host that does support swap
accounting should re-run this harness before the caps are frozen.

## 7. Environment findings that constrain reproducibility

1. **Docling 2.130.0 does not auto-download weights when `artifacts_path` is set.** With
   `DOCLING_ARTIFACTS_PATH=/models` and an empty cache the layout model raises
   `FileNotFoundError: Model 'docling-project/docling-layout-heron' not found in artifacts_path`.
   The harness therefore carries the idempotent `docling-tools models download` preamble
   above. This is a real property of the pin, not a quirk of this host.
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
| `Dockerfile` | pinned runtime (docling + CPU torch + tesseract eng/chi_sim), with build-time guards |
| `build.sh` | ceiling-constrained build + `tesseract --list-langs` verification + freeze snapshot |
| `driver.py` | in-container measurement driver; emits exactly one JSON line, logs to stderr |
| `run_measured.sh` | ceiling-constrained `docker run` wrapper; merges `docker inspect` OOMKilled + host-side cgroup peaks into the result |
| `PIN_MANIFEST.md` | this file |
| `results/` | build snapshots (`image_*.txt`) + one dir and one `<name>.measured.jsonl` per measured run |

This directory is the harness only. It was built and run before the rest of
T2a, and the ticket's later work — gold fixtures under `data/corpus/`, the vision
probe against the configured provider, and the `tests/host` suite — is recorded in
the validation document above, not here.
