# C1 pin policy — the identity to prove, and when a baseline may be adopted

Date: 2026-10-02. Every "Observed identities" row names where its value was
re-read from. Two kinds of source are cited, and the difference matters: a
**committed** artifact (a tracked file, read out of the working tree at the
named revision) and a **local** artifact (a build output, image tag, or
uncommitted result file that exists only on the measuring host and is named so
it can be re-read there). Rows sourced locally are marked; they are not
re-checkable from this repository alone. Nothing here is quoted from memory.

Branch: `pre330/s2-pipeline` (base `ecd3ab4`).

Task: **T3** of `plans/pre330-remaining-prerequisites.md` — settle the C1 pin
policy against the accepted operator ruling, so T4 has an agreed identity to
prove and an agreed rule for withholding a baseline.

Inherited contract: the operator ruling recorded in
`docs/validation/2026-09-30-g4-buildkit-reproducibility.md` — move to BuildKit,
establish a new baseline, keep `8e10185f…` as history, and prove it with two
independent clean builds — and L7's acceptance criterion, quoted in
`plans/post321-pipeline-reproducibility.md:21` (and again at
`plans/pr321-merge-followup.md:89`) — "a rebuild from the pin reproduces the
recorded `sha256` byte-for-byte, with versions pinned mechanically rather than
in prose".

## The policy, in two rules

**Rule 1 — the identity to prove.** The C1 proof target is *not* any digest
recorded before 2026-09-30. T4 must produce **two independent clean builds of
`scripts/g4_pipeline/Dockerfile` on the declared `g4repro` BuildKit builder from
the immutable committed inputs, and those two builds must agree on a single
image identity.** The identity that satisfies C1 is the one both builds produce.
It is named by the builds, not chosen in advance, and it is not recorded as
`expected_image_id` until it exists as a reproducible pair.

**Rule 2 — when a baseline may be adopted.** `expected_image_id` in
`scripts/g4_pipeline/image-pin.json` is re-baselined **only** when two
independent clean builds produce the *same* full digest. While any two clean
builds disagree, C1 stays `PENDING`, `expected_image_id` keeps its historical
value, and every `build.sh` run keeps exiting 75 at the identity gate. A
digest observed once, however many times it has been seen, is evidence and not
a baseline.

Neither rule changes the accepted policy; both restate it. Any change to the
accepted policy itself is an operator decision and must be recorded as one
**before** T4 adopts it. This record makes no such change and requests none.

## `8e10185f…` is history, not a target

Until the operator's 2026-10-02 decision, `expected_image_id` was
`sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800`,
sourced from the L7 digest recorded on 2026-09-29. That digest was produced by
the **legacy builder**, which the operator's option (a) ruling retired: the
legacy path stamps a wall-clock `created` and per-build mtimes into the image,
so it cannot produce a fixed digest by construction.

It follows that `8e10185f…` is **unreachable by any current builder**. It is not
a failing expectation that a fix would satisfy, and no amount of build-hygiene
work will make a `buildx` build produce it. It is recorded in
`image-pin.json` — in `blocking_reason` — deliberately: the issue forbids
silently replacing the expected digest, so the file records what was claimed
and why the claim became historical. It is a historical marker, not a pending
target. The adopted baseline is `b1b5270c…`; see *T4 status* at the end of this
record.

## Observed identities, and what each one is allowed to be

| Identity (full digest) | Where it was re-read from | What it is |
| --- | --- | --- |
| `sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296` | `scripts/g4_pipeline/image-pin.json` → `expected_image_id`; built as `fin-g4-pipeline:r1` and `:r2` (local tags) | **The adopted baseline.** Two independent clean builds from `16adabe`, 11 of 11 layers identical, adopted by the operator 2026-10-02. Committed as a *value*; the tags are local. |
| `sha256:8e10185fa27ea5adcfca94732adc27a2720ed9a31b67698d0a2c9e7a1a62c800` | `scripts/g4_pipeline/image-pin.json` → `blocking_reason` | **Historical.** L7, built by the retired legacy builder. Unreachable by any current builder. No longer the declared expectation. |
| `sha256:0f278c22daa45a8361235f37620fa31a237f0284501436256429e2a8eba35229` | `scripts/g4_pipeline/results/image_identity.txt:1` | **Committed artifact, drift.** A 2026-09-29 legacy-builder rebuild of the drifted shape. It is still what `build.sh` compares against on this host, so the identity gate still fires here; adopting it was refused and remains refused. |
| `sha256:f896868992d2…` (12-char prefix only) | `docs/validation/2026-09-30-g4-buildkit-reproducibility.md:36` | **Historical measurement, first of a disagreeing pair.** Full digest was never captured and does not exist in any artifact. |
| `sha256:8f0009ab766b…` (12-char prefix only) | `docs/validation/2026-09-30-g4-buildkit-reproducibility.md:36` | **Historical measurement, second of a disagreeing pair.** Full digest was never captured. |
| `sha256:d3bae2c0de4907b34f6fd83939b69b1c87f0aa4f4c05c75046c023938fc77ccc` | **local**, re-read from the image tag `fin-g4-pipeline:pre330-a` on the measuring host (`docker image inspect … --format '{{.Id}}'`); the build's own `results/image_identity.txt` was written under `.fin/pre330/build-a/`, which is **untracked and absent from this checkout**, so it is not a re-readable committed path | **Third observed buildx-era identity.** `created=2023-11-14T22:13:20Z`, `size_bytes=589605874` — the SOURCE_DATE_EPOCH normalisation is working. The build exited 75. Observed, **not adopted**, and **not the current baseline** — `b1b5270c…` is. |

### The 2026-09-30 pair may not be adopted, and the reason is specific

The two full pinned builds of 2026-09-30 produced `f896…` and `8f000…`. They
disagreed in **2 of 11 layers**; the four differing full layer digests are the
only full-digest artifacts that measurement retained
(`docs/validation/2026-09-30-g4-buildkit-reproducibility.md:45-49`).

Three facts make silent adoption impossible, and each is a reason on its own:

1. **They disagree with each other.** A digest is only an identity if the build
   is a function of its inputs. Two clean builds of the same committed inputs
   yielding two digests means the inputs do not yet determine the output.
2. **Their full digests were never captured.** Only 12-character prefixes
   survive. An identity that cannot be written down in full cannot be pinned,
   compared, or reproduced by a later build.
3. **The differing layers were located but never repaired.** The record's own
   closing item is that the two differing layers are "located but not fixed",
   and it names the Dockerfile change as the next step. That step was not taken
   before `ecd3ab4`, so the disagreement is unexplained, not closed.

`d3bae2c0…` does not rescue the pair. It is a third identity from the same
buildx era; adding a third disagreeing observation to two disagreeing
observations is not convergence, and a build that ended in exit 75 has not
proven anything about its own reproducibility. It was left unadopted, and it
is not the identity that was ultimately adopted either.

## C1's status at ecd3ab4

Everything in this subsection is T3's finding **at base `ecd3ab4`**, before the
repair and before the operator's decision. It is kept as written because it is
the state T4 started from; it is superseded by *T4 status* below, which is the
current state.

`build.sh` exits 75 because of the identity gate, and that refusal is correct
behaviour, not a defect to route around. The gate is
`scripts/g4_pipeline/build.sh:206-212`:

```bash
echo "[build] verifying the built image against the declared identity"
if ! python3 "${HERE}/pin.py" compare-image-id \
        --pin "${HERE}/image-pin.json" \
        --observed "${RESULTS}/image_identity.txt"; then
    echo "[build] FATAL: the image built as ${IMAGE} is not the declared one; actual id is in ${RESULTS}/image_identity.txt" >&2
    exit 75
fi
```

`pin.py:576-578` implements `compare_image_id` as bare string equality, so at
`ecd3ab4` the gate fires on **any** digest other than `8e10185f…` — including a
perfectly reproducible pair. That is the intended shape while no baseline
exists: the gate refuses rather than blesses. It is still the shape after the
adoption, pointed at a different digest: the check is unchanged, only the
declared value moved.

It follows, stated plainly so it is not misread later:

- **C1 was BLOCKED.** `image-pin.json` carried
  `status: "BLOCKED_ON_OPERATOR_DECISION"`.
- **C1 had no reproducibility pass.** The two independent clean builds that
  would constitute the pass had not been run at `ecd3ab4`, and the only
  full-build evidence on record was a *disagreeing* pair. "The buildx path was
  adopted" is not "the build reproduces".
- The 2026-09-30 record's conclusion — that the builder switch is "executed;
  its acceptance is not" — still holds at this revision.

## What T4 inherits

- Prove Rule 1's pair on the declared `g4repro` builder from committed inputs.
- Apply Rule 2: re-baseline only on two agreeing clean builds, as an explicit
  recorded decision, never silently.
- Do not weaken `pin.py`, the identity gate, or any pin check to make a build
  pass. A gate that fires is a finding to investigate, not an obstacle to
  bypass.
- If the two builds still disagree, leave C1 `PENDING` with the exact cause and
  the full per-layer digests. A recorded failure with evidence advances C1; an
  adopted mismatching baseline destroys it.

## Status

**T3: DONE — the policy is settled and recorded; no policy change is proposed
and no baseline is adopted.**

---

# T4 — the repair and the two-build proof

Same branch, commit `16adabe` on top of `ecd3ab4`.

## First measurement: the pre-repair pair disagreed

Two independent clean builds of `ecd3ab4`, run sequentially on the declared
`g4repro` builder with `--no-cache`:

| | build 1 | build 2 |
| --- | --- | --- |
| image id | `sha256:717881c47e2c…` | `sha256:fd61c0042ddb…` |
| layers | 11 | 11 |

**8 of 11 layers identical, 3 differing** — so the 2026-09-30 disagreement
reproduces, and the index trap is confirmed: the differing layers are **4, 8
and 9**, not the record's "steps 4 and 7".

Extracting both images' layer tars and hashing every file turned the record's
hypotheses into measurements. Path, mode, size, uid, gid and mtime were
**identical** in all three layers; only file *content* differed. The per-layer
diff ids behind this section, and the regeneration recipe for the per-file
lists, are committed in `docs/validation/2026-10-02-c1-layer-diffids.txt`.

| Layer | Step | Differing files | Cause (measured) |
| --- | --- | --- | --- |
| 4 | apt install | 8 | 4x `/var/cache/fontconfig/*.cache-9`, `/var/cache/ldconfig/aux-cache`, `/var/log/apt/history.log`, `/var/log/apt/term.log`, `/var/log/dpkg.log` |
| 8 | `RUN python` (torch assert) | 774 | `.pyc` regenerated by `import torch` |
| 9 | `RUN … pip freeze …` | 13 | `.pyc` regenerated by `pip freeze` |

The two `.pyc` layers are one cause: `--no-compile` stopped pip writing bytecode
at install time, but the two steps that *run* Python regenerate it on import,
and a `.pyc` embeds the source mtime that pip stamps at install time. Both
builds carried exactly 1,099 `.pyc` files, so a count check would have missed
this — only the per-file hash exposed it. Note what this **rules out**: the
record's "step 4 `COPY requirements.lock`" theory does not reproduce. Layer 6
(the COPY) was byte-identical across builds, and so was layer 7 (the pip
install), which also means the untested `--no-compile` fix **does** work at full
scale.

## The repair

Two changes, both in `scripts/g4_pipeline/Dockerfile` (commit `16adabe`):

1. **`PYTHONDONTWRITEBYTECODE=1` in the `ENV` block**, so no build step writes
   bytecode. This covers both Python steps at once and makes the torch-assert
   layer empty, like the two no-op steps beside it.
2. **The apt step now also purges** `/var/cache/fontconfig/*`,
   `/var/cache/ldconfig/aux-cache`, `/var/log/apt/{history,term}.log` and
   `/var/log/dpkg.log`, alongside the existing `/var/lib/apt/lists/*` removal.

fontconfig and ldconfig rebuild their caches on first use, dpkg reopens its logs
on the next package operation, and Python recompiles on demand — so nothing at
run time needs any of it baked in. The snapshot step still records the resolved
package set through `dpkg-query`, which reads dpkg's database and is unaffected.

Nothing else was touched. No pin value, no hash, no check, and **not**
`memory_ceiling` — that stays a build input enforced by the builder's
`--driver-opt memory=4g` and remains strictly separate from the
`G4_CEILING_*` runtime caps in `run_measured.sh`, which this change does not
reach. `pin.py self-check` still exits 0 against the repaired Dockerfile.

## Second measurement: the two proof builds agree

Two more independent clean builds, again sequential, again `--no-cache`:

| | build 3 | build 4 |
| --- | --- | --- |
| image id | `sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296` | `sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296` |
| layers | 11 | 11 |
| size | 579,825,559 | 579,825,559 |
| `created` | `2023-11-14T22:13:20Z` | `2023-11-14T22:13:20Z` |

**11 of 11 layers identical. No layer differs.** The image also lost 1,099
`.pyc` files and the generated caches, 9.8 MB smaller than before the repair.
Layer 8 is now the empty-diff digest `sha256:5f70bf18a086…`, the same value
that layers 5 and 10 carry, which is the structural signature of a step that no
longer writes anything.

This satisfies Rule 1. It did **not** by itself satisfy Rule 2's adoption
step, which is an explicit operator decision: at that point `expected_image_id`
was still `sha256:8e10185f…`, and both proof builds exited 75 at the identity
gate, which was correct — the gate refused because no baseline had been adopted,
not because the build failed. Rule 2 was applied separately, below.

## Availability finding, independent of determinism

`snapshot.debian.org` is **intermittently unreachable from this host**. Two of
six clean-build attempts failed in the apt step with `Unable to connect to
snapshot.debian.org:http` and exit 100 — one after 314 `Ign:` retry lines, one
immediately. A direct probe failed 1 of 3 attempts at the same time, while the
host itself reached pypi.org in 1.6 s. The Python index was simultaneously
unstable inside the build, though `PIP_RETRIES=8` rode through it.

This is a real property of the current pin design, not a flake to ignore:
`apt-packages.lock` *records* the ~188 installed transitive packages but does not
gate them, and every clean build fetches all of them live over HTTP. The
Dockerfile's claim that the snapshot mirror "binds the layer" is therefore
conditional on a third party's uptime. Determinism was proven here across two
builds, but proving it repeatedly depends on that mirror. C1's byte-for-byte
claim is unaffected; C1's *repeatability* carries this risk.

## T4 status

**The reproducibility proof PASSES.** Two independent clean builds of the
repaired Dockerfile produced the same image identity, layer for layer.

**C1 is no longer blocked: the operator adopted the baseline on 2026-10-02.**
`expected_image_id` in `scripts/g4_pipeline/image-pin.json` is now
`sha256:b1b5270c49cffbd2165aed904226612f2ccf925072c91766b4d4979f24bb9296`,
`status` is `BASELINE_ADOPTED`, and the per-layer evidence is committed in
`docs/validation/2026-10-02-c1-layer-diffids.txt`. That satisfies Rule 2 in the
form the rule demands: an explicit, recorded decision, taken after two
independent clean builds agreed — not a silent re-baseline onto whatever a
build happened to produce. `8e10185f…` is kept in the file's `blocking_reason`
as history and is unreachable by any current builder.

**What the adoption does not change.** `pin.py` is untouched and the identity
gate is still a bare string comparison that exits 75 on any digest other than
the declared one; a build that produced anything else still fails, and that
refusal is now measured against a real baseline rather than an unreachable
one. `d3bae2c0…` and `0f278c22…` are still not adopted. The availability
finding above is untouched by the decision: the apt snapshot mirror is still a
live third-party dependency, so repeatability remains conditional on it. C2 is
unaffected and stays deferred.

