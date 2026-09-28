# G4 T4 — typed bounded source read: read_source legs, unified locators, live wire

Date: 2026-09-28. Every "Commands" row ran on this branch on this date; a row
naming a hash ran at that tree, otherwise at the then-current tip (final
content tip `740e3c8`).

Branch: `worktree-t4-bounded-source-read` (base `main` `e60f824`). Commits, in
order: `0c6e9f2` the shared bounded-slice contract, `5737784` the company leg,
`7455f9b` the attachment leg, `b140652` the model-facing tool and evidence
wiring, `b371757` the live acceptance module, `17e723f` the bound-tool tail pin,
`7c2d441` the first-round review repairs (1 High + the Medium clusters, listed
below), `01d05fb` the ToolNode-surface pins, `f3ac85b` this validation record,
`7680421` the type-clean pass, `a401934` the issue-the tracked change identity-key cache-seam
fix. Content diff `e60f824..a401934` under `src/` and `tests/`: 19 files,
+3579/−46. `740e3c8` then merges `main` (T3 #316, which carried the tracked change's
`_IdentityKey` cache conversion) with the five `graph.py` conflict regions
resolved per the tracked change: both `attachment_retriever` and
`attachment_source_reader_factory` seams, both tools, and both cache
identities kept, one `_IdentityKey` class.

Inherited contract: `specs/thread-attachment-source-read.md` lines 59–82 (the
`read_source` tool, the 8,000-code-point / 32-KiB bounds, the typed exits,
acceptance 7/8/9) and ADRs 0046 / 0087 / 0088. The consumed backend contract is
iknow-memory's delivered bounded wire (`iknow-memory/specs/bounded-source-read.md`,
main `7e4f0d0` that pull request): success `{id, source_ref, title?, content,
next_position:int|null}`, refusal codes `read_source_not_found|invalidated|
invalid_position|invalid_locator|unavailable`, caps 8,000 code points per slice,
32,768 bytes per serialized ToolResult, 1,024 code points per request locator —
and the slice can be SHORTER than 8,000 code points because the byte cap binds
first, which is what forced the cursor walk on fin's legacy full-body fetch.

Scope boundary honored: `read_file` and the generic `page` DTO survive here;
T5 owns their removal (`plans/thread-attachment-source-read.md`:150).

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | `a401934` (content branch tip); new `src/fin/retrieve/source_read.py` (206 lines), `src/fin/host/attachment_source_read.py` (250), `src/fin/host/tools/read_source.py` (79); company leg in `mcp_adapter.py` (+404), `read_port.py`, `fake_port.py`; wiring in `graph.py` (+233, incl. the `a401934` identity-key cache seam), `stream_map.py`, `agent_instructions.py`; 5 new/extended test modules |
| Python | 3.12.14 (`uv run pytest`); default suite stays hermetic (mcp-marked live modules deselected: 307) |
| Live backend | real iknow-memory stdio server, main checkout `/home/winner/projects/iknow-memory` @ `08cedb1`, `dist/` built 2026-09-26 (contains the bounded `read_source` tool: `dist/tools/read_source.js` carries `next_position`); storage is the live suite's default in-memory source store — the MCP transport, server tool code, slicing, byte-cap authentication, and refusal codes are all real |
| Attachment store | `InMemoryAttachmentStore` (product implementation, same protocol the Postgres store answers); the env-gated Postgres pairs stay skipped on this shell |

## What was claimed, and the evidence for each clause

Acceptance clauses are the plan's T4 item (`plans/thread-attachment-source-read.md`:142).

| Clause | Evidence (actual observed) |
| --- | --- |
| Company and attachment locators return bounded contiguous slices with advancing `next_position` | `tests/retrieve/test_source_read.py` (8), `test_source_read_company_leg.py` pass-through + cursor, `tests/host/test_attachment_source_read.py` (51) continuation walk, `tests/host/test_read_source_tool.py` (40) |
| Concatenating slices reproduces source text incl. supplementary-plane characters | hermetic: cap-boundary astral runs in `test_source_read.py:60-94`, attachment walk with 😀 at slice ends 7999/15999; LIVE: 16,053-code-point unit reassembled exactly across ≥2 real wire slices with no lone surrogate (`test_live_bounded_read_source.py`, 3 passed) |
| Cross-thread and stale attachment locators reveal no text | `test_attachment_source_read.py`: foreign thread / stale version / tombstone / unprepared / out-of-range unit all return the identical `{"error": "unknown_locator"}` with no differentiating field and no text |
| Malformed/oversized backend results → `source_backend_contract_error`; transport faults → `source_unavailable`; invalid positions/locators typed | `test_source_read_company_leg.py`: >8,000-cp content, 32-KiB-oversized, seven malformed envelope shapes, non-advancing cursor, `OSError`/`TimeoutError`/`RetrieveUnavailableError`, and all five backend refusal-code mappings; live beyond-end position → `invalid_position` |
| Only returned text enters this-run evidence | `test_read_source_tool.py`: exactly one `EvidenceItem` per non-empty slice, none for errors or the legal empty terminal slice; direct read without a same-run retrieve passes the citation gate |
| No model-facing arbitrary path read | the registered tool schema is exactly `{source_ref, position}`; routing is by typed locator kind only; pinned in `test_read_source_tool.py` schema test |
| Legacy `read_file` fetch still correct against the now-bounded backend | cursor-following walk pinned by `test_source_read_alignment.py` (green) and LIVE `test_live_bounded_read_source.py::test_live_legacy_read_follows_the_cursor_to_the_full_body` |
| Sibling dependency | iknow-memory bounded-source-read T1 delivered at main `7e4f0d0` (that pull request); this record consumes it over the real wire |

## Commands

| Command | Result |
| --- | --- |
| `uv run pytest -q tests/retrieve tests/host tests/corpus` (at `7c2d441`) | 4249 passed, 111 skipped, 307 deselected; 3 failed — 2 were the deliberate ToolNode-surface set-equality pins, updated in `01d05fb` and re-run green (13 passed) at the tip; 1 is `test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory`, blocked by this host's docker preflight (the test starts no services) — see Limitations |
| `uv run pytest -q -m mcp tests/retrieve/test_live_bounded_read_source.py tests/retrieve/test_live_mcp_read_source.py` | 6 passed, exit 0 (real stdio process, real bounded tool; re-run at `a401934`) |
| `uv run pytest -q tests/retrieve/test_source_read.py ... tests/host/test_graph.py` (repair-round set) | 426 passed |
| review round 1 | Standards 0H/3M/5L, Spec 1H/0M/1L → `GATE: BLOCKED: 1 High`; repaired in `7c2d441` with fail-before-fix evidence (CJK 48,124 B / astral 96,124 B emitted vs the ≤32,768 B bound; 4 failed → 40 passed after emitting through `serialize_result`) |
| `uv run --with pyright pyright` on the eight changed source modules (re-measured at `a401934`) | 37 errors: `graph.py` 35 vs 34 on `main`, `fake_port.py` 2 (both pre-existing on `main`), other six modules clean. The +1 is the new tool's `config: Annotated[RunnableConfig, InjectedToolArg()] = None` default — byte-identical to the existing `retrieve` / `recall_memory` convention in the same file, kept over diverging. The three type errors the first pass introduced (2 in `summarize_read_source`, 1 in the wrapper `getattr` routing) were fixed in `7680421` |
| `uv run pytest -q tests/host` (at `a401934`, identity-key fix) | 2608 passed, 94 skipped, 247 deselected, 1 failed — the docker-socket-gated launcher test above; the identity pin `test_idempotence_cache_holds_the_reader_factory_object_itself` fails before `a401934` and passes after |
| `uv run pytest -q tests/host tests/retrieve` (merged tree at `740e3c8`) | 3521 passed, 94 skipped, 315 deselected, 1 failed — the same docker-socket-gated launcher test |
| `uv run pytest -q` on the issue-the tracked change named set: `test_graph_attachment_retrieve.py test_graph_attachment_gate.py test_graph_cache.py test_attachment_source_read.py test_read_source_tool.py test_source_read.py test_source_read_company_leg.py` (at `740e3c8`) | 200 passed; includes the new `test_same_thread_retrieve_then_read_source_handoff` — one graph wiring both seams, the locator T3's envelope publishes is read verbatim by `read_source` under the same host-bound thread, and only the returned slice text carries the citation |
| live MCP re-run at `740e3c8` | 6 passed (real stdio iknow-memory, bounded wire) |
| pyright on `graph.py` + `compile.py` at `740e3c8` | 38 errors vs 37 on current `main` (`graph.py` 35→36, `compile.py` 2→2): the single delta remains the documented `config: Annotated[RunnableConfig, InjectedToolArg()] = None` convention copy; the merge introduced nothing |
| post-restart launcher re-run 2026-09-28 at `2755777` (the tracked change): `id -nG` → `winner docker`; `getent group docker` → `docker:x:102:winner`; `docker info` → client 29.1.3 present, server `dial unix /var/run/docker.sock: connect: no such file or directory` | docker-group fix CONFIRMED (`DOCKER_DENIED` gone); the daemon itself was absent — see Limitations 2 |
| `uv run pytest -q tests/host/test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory` (2026-09-28, at `2755777`, docker down) | RED, 1 failed — exit 1 from `scripts/lib/fin-root.sh:99`: `dev-up: docker daemon is not answering (docker info failed)` |
| `bash scripts/cursor-dockerd.sh start` (2026-09-28, this host) | `cursor-dockerd: starting` → `cursor-dockerd: ready`. `docker info` then answered: Server 29.1.3, storage driver `overlayfs`; `/var/run/docker.sock` is `srw-rw---- root docker` — the socket ownership the docker-group fix was after |
| `uv run pytest -q tests/host/test_fin_launcher.py::test_shim_drives_a_checkout_from_any_directory` (2026-09-28, at `2755777`, docker up) | **GREEN — 1 passed, 1.35s, exit 0.** Gate (1) satisfied |
| `uv run pytest -q tests/host tests/retrieve` (2026-09-28, docker down) | 3522 passed, 94 skipped, 315 deselected, 1 failed — the same launcher test; +1 passed vs the `740e3c8` row, so no T4-path regression at this tip |
| `uv run pytest -q tests/host tests/retrieve` (2026-09-28, at `2755777`, docker up) | **3523 passed, 94 skipped, 315 deselected, 0 failed**, 513s, exit 0 — first fully green run of these two suites on this branch; the previously-failing launcher test is the +1 |
| host migration 2026-09-29: `ps -p 1 -o comm=` → `systemd`; `systemctl is-system-running` → `running`; `systemctl --failed` → **0 units**; `systemctl status docker` → `active (running)`, `enabled`, Main PID 136; `docker info` → Server 29.1.3, `Cgroup Driver: systemd`, overlayfs, reachable without sudo; `/var/run/docker.sock` = `srw-rw---- root docker`; `bash scripts/fin check` → `dev-up: check ok` | the legacy `[boot] command=/usr/local/bin/wsl-boot.sh` hook is gone; systemd owns the daemon and brings it up at boot, so no manual `cursor-dockerd.sh` is needed any more. Enabling it took three pieces: `[boot] systemd=true` in `/etc/wsl.conf`, the `systemd` package, and the `systemd-sysv` package (without which `/sbin/init` does not exist and WSL has nothing to promote to PID 1). Original config backed up at `/etc/wsl.conf.bak-20260928` |
| `uv run pytest -q tests/host tests/retrieve` (2026-09-29, at merged `7cb068b`, under systemd) | 3523 passed, 93 skipped, 315 deselected, **1 failed** — `tests/host/test_workspace_http_attachment_lane.py::test_a_standalone_image_never_stages_a_heron_document`, which is NOT a T4 path: that test asserted `pytest.raises(OcrRequiredUnavailableError)` on the premise that the optional `[ocr]` extra is absent, and this host now has `pytesseract` + `tesseract` 5.5.0 installed. Attributed to host state, repaired as a test-only fix (see below) |

## Limitations / leftovers

1. The live leg ran against iknow-memory's in-memory source store (the live
   suite's default); a PG/ES-backed `read_source` round trip was exercised in
   iknow-memory's own gold record, not re-run here. The MCP transport, tool
   code, slicing, and refusal codes were real.
2. `test_shim_drives_a_checkout_from_any_directory` is now GREEN on this host;
   the two environment defects behind its earlier reds are both closed, and the
   fix was environmental, not a code change.
   (a) The original `DOCKER_DENIED` — socket mode 660 `root:docker` with an
   empty docker group — is resolved: `getent group docker` returns
   `docker:x:102:winner`, `id -nG` lists `winner docker`.
   (b) The second blocker was that no docker daemon was running at all. This host
   is WSL2 with NO systemd (PID 1 is `init(Ubuntu)`), and `/etc/wsl.conf` starts
   the daemon via the legacy `[boot] command=/usr/local/bin/wsl-boot.sh` →
   `exec dockerd`. Nothing supervises that process, so it died with its session
   (visible in `/tmp/dockerd-restart.log` as `copy stream failed / file already
   closed` after 14:36) and the boot hook did not bring it back, leaving
   `/var/run/docker.sock` absent. `bash scripts/cursor-dockerd.sh start` (the
   repo's own launcher, which waits for readiness and fixes socket group) brought
   it up; the test then passed.
   Note for future runs: this host has `docker.io` only — no Docker Desktop —
   so the `sudo service docker start` line in `scripts/lib/fin-root.sh`'s hint
   does not apply (`docker.io` ships a systemd unit but no `/etc/init.d/docker`,
   and systemd was off). The durable fix — enabling systemd — was applied on
   2026-09-29 (see Commands table): the legacy `[boot] command=` hook is gone,
   systemd owns the daemon, and no manual `cursor-dockerd.sh` is needed. Do NOT
   run `cursor-dockerd.sh` now that systemd owns the daemon — it would race a
   second dockerd for the same socket. That script remains correct for the Cursor
   Cloud VM, which genuinely has no init system. The test touches no T4 code path.
3. The `page`→`unit` DTO rename, `read_file` removal, and attachment citation
   identity remain T5's, as the plan assigns.
4. Web (`npm run typecheck` / `npm test`) was not run: this diff ships no web
   surface — the read_source tool result only reaches the operator trace
   through the existing `format_tool_summary` path.
5. Two suite failures seen on 2026-09-29 are this host's `.env`, not the diff,
   and both reproduce on a clean checkout of `7cb068b`.
   (a) `tests/corpus/test_chunk_and_push.py::test_cli_requires_explicit_opt_in`
   fails with `Permission denied: 'iknow-memory'` because
   `tests/conftest.py`'s `apply_dotenv` reads `FIN_RETRIEVE_ADAPTER=mcp` from
   the operator `.env`; the test asserts the CLI refuses without an explicit
   opt-in. That conftest strips several operator vars for exactly this reason
   (`FIN_MEMORY_ENABLED`, the LLM keys) but not this one. A CI runner has no
   `.env`, so it stays green there.
   (b) `tests/corpus/test_ocr.py::test_default_pytest_does_not_import_pytesseract`
   passes alone and fails only in a session where another file built a real
   `TesseractEngine` first; the leak itself was repaired here (see the
   `_no_pytesseract_leak` fixture in
   `tests/host/test_workspace_http_attachment_lane.py`), but the same class of
   order-dependence remains possible wherever a sibling instantiates the
   engine without cleaning up.

## Merge gates (the tracked change)

Owner review of this record kept that pull request in waiting-to-merge state with three
gates: (1) the launcher preflight re-run on a docker-socket-capable runner
above — CLOSED 2026-09-28 (below); (2) a continuation-read smoke against a
DEPLOYED iknow-memory build containing bounded `read_source` (the live cases here
use the in-memory store and do not establish deployed-store availability); (3)
T3/T4 integration —
DONE ON-BRANCH at `740e3c8`: `main` (T3 #316, incl. the the tracked change `_IdentityKey`
conversion) merged with the five `graph.py` conflicts resolved keeping both
seams, both tools, and both cache identities; the named suites and the
same-thread `retrieve` → `read_source` handoff re-ran green at that tree
(Commands table).

Gate (1) CLOSED 2026-09-28 (the tracked change). `test_shim_drives_a_checkout_from_any_directory`
passes: 1 passed, 1.35s, exit 0 (Commands table). It needed no T4 change and no
new dependency — only a docker daemon that answers on this host, reached via the
repo's own `scripts/cursor-dockerd.sh`, after the issue-the tracked change docker-group fix put
this user in the socket's group. Root cause of the two earlier reds was entirely
host configuration (missing group membership, then an unsupervised `exec dockerd`
under WSL2-without-systemd), not the diff. See Limitations 2. The host-side
systemd fix that makes this durable landed on 2026-09-29 (Commands table) and
that pull request merged as `7cb068b`.

Gate (2) stays open until run by whoever has the environment. `read_file` /
`page`
removal and the full real-store G4 gold run stay with T5 / end-of-round
acceptance.
