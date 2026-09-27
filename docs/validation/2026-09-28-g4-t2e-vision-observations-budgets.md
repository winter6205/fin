# G4 T2e — versioned visual observations, the vision port, and the run's retry reserve (2026-09-28)

Validation record for plan
[`thread-attachment-source-read`](../plans/thread-attachment-source-read.md)
task **T2e (Persist reusable full-page and crop observations)** on branch
`feat/g4-attachment-t2e-budget`, worktree
`/home/winner/projects/fin-wt-g4-t2e-budget`, content tip `5f11dcb`.

Delivered commits, first-parent order, oldest first:

| commit | content |
| --- | --- |
| `3a7ffa3` | the retry reserve priced into the run budget, the per-attempt `VisionCallLedger`, and `.env.example` 88 → 89 |
| `5d8e43f` | the dedicated attachment vision port (`attachment_vision.py`): one call over one image, typed exits, provider-reported usage/cost |
| `0458100` | the boot fixture's run budget derived through `required_run_calls` instead of restated (preceded by the claim commit `4d16c49`) |
| `afba78d`, `156e316` | the two merges that landed the T2d page/region core as this ticket's dependency |
| `84af537` | the observation envelope and its durability (`attachment_observations.py`, `record_observation` on both store adapters) plus the adapter's `VisionErrorClass` retry classification |
| `4d567cb` | the first-preparation per-page full-page scan wired into `AttachmentPreparationService._scan_pages`, charge-before-send |
| `f88bba8` | the crop re-inspection module (`attachment_vision_inspect.py`, `inspect_for_details`) |
| `5f11dcb` | the composition-root deployment of the page lane, the six `FIN_ATTACHMENT_HERON_*` template rows, and the door tests rewritten for the wired lane |

Inherited contracts: ADR 0089
(`docs/adr/0089-lazy-page-region-attachment-preparation.md`), the spec's
observation clause (`specs/thread-attachment-source-read.md:56`), the T2a
record (the four bounds, the `88 = 80 + 8` floor it could not price a retry
into — *Leftovers* 2, 9, 10), the T2d record (page/region core, the refusal
door and the wiring it assigned to T2e), and the T2e0 decision recorded at
plan item 5a (issue):
one run-wide retry reserve `80 + 8 + 1 = 89`, the vision port reusing
`FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY`, and a monetary ceiling
explicitly deferred.

## Scope

What T2e delivers:

* a **versioned observation envelope** and the host's one deterministic,
  conservative reuse decision (`attachment_observations.py`), persisted per
  `(attachment_id, version, unit, page, observation_version)` on both store
  adapters;
* a **dedicated vision port** separate from the conversational model port
  (`attachment_vision.py`), configured from the host's own three settings,
  with the two outward failures typed and an adapter-owned retry
  classification (`VisionErrorClass.TRANSPORT` / `.PERMANENT`);
* the **per-page full-page scan** on first preparation, including pages with
  no Heron box and pages that froze no unit (`attachment_prepare._scan_pages`);
* the **crop re-inspection** module (`attachment_vision_inspect.py`,
  `inspect_for_details`), with a caller-supplied raster-bytes boundary and
  joint coverage across the observations already read into the run;
* the **run-wide retry reserve priced into the budget** and a
  charge-before-send `VisionCallLedger` that every attempt, failed ones
  included, draws on (`attachment_budgets.py`);
* `.env.example` at **89** plus the six `FIN_ATTACHMENT_HERON_*` rows, closing
  the template absence T2d listed as a leftover;
* the **page lane deployed at the composition root** — the wiring T2d
  assigned here by decision — so `attachment_page_lane_unwired` is
  unreachable on the shipped root's success paths.

What T2e does **not** deliver: durable page rasters (only their SHA-256 is
kept), a `text`-kind Heron box, per-run usage/cost aggregation, or a monetary
ceiling. See *Leftovers*.

## Environment / dataset version

| Component | Value |
| --- | --- |
| Code under test | every command in *Commands* ran at `5f11dcb` in this worktree; `git status --porcelain` was empty before and after each row except the last, which adds only this file and the plan Status line |
| Python / runner | `uv run pytest` 9.1.1 on Python 3.12; the five-file run is hermetic — no network, no container, no credential, no Postgres |
| Gold data | the preparation rows drive the committed `data/corpus/g4_attachment_gold/g4-mixed-chart-001.pdf` through the real admission path (page count, unit freeze, row state); the rasters those rows scan are deterministic synthetic PNGs from a fake page lane, and the provider is the `FakeVisionPort` of the same file. The live leg re-reads the committed `g4-image-chart-001.png` and is recorded separately |
| Provider boundary | `ChatModelVisionAdapter` resolving `FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY` from the main checkout's operator `.env` (a git worktree has no `.env` of its own; the test's credential lookup falls back to the worktree's git common dir parent). The key value appears nowhere in this file |
| Heron container | not exercised: this session cannot reach the docker daemon (`docker info` → `permission denied … /var/run/docker.sock`), and no T2e test requires it — `test_booting_the_lane_touches_no_container_no_binary_no_provider` pins that boot itself pays none |
| Postgres durability | the five `test_postgres_*` observation cases skipped for want of `FIN_CHECKPOINT_URL` plus a reachable server; see *Validation not run* |

## Per-clause evidence

Clause numbers follow the acceptance text of plan item 6. Each row names the
commit that carries the behavior, the shipped anchors, and the tests, in
`file::test` form with the line at this tip. Every name below was grep-verified
against the tree at `5f11dcb` before it was written here.

**A1 — envelope fields persisted (`coverage_schema_version`,
`observation_version`, content version, source unit, physical page,
page-image hash, provider/model version, scope).** `84af537`.
`COVERAGE_SCHEMA_VERSION = "fin.visual-observation/1"` at
`attachment_observations.py:59`; the envelope's fields and their refusals at
`:199-259`; the identity reconciliation from the port's text version to the
store's integer content version at `:262-301` (a non-integer version is
refused, never coerced); the durable shape at
`attachment_store.py:1037-1053`, an additive
`CREATE TABLE IF NOT EXISTS fin_thread_attachment_observation` inside the one
`_SCHEMA` the adapter applies on connect (`:1140`), with
`PRIMARY KEY (attachment_id, version, unit, page, observation_version)`.
Evidence: `tests/host/test_attachment_observations.py::test_envelope_carries_every_persisted_provenance_field:124`,
`::test_envelope_rejects_blank_identity_and_nonpositive_positions:140`,
`::test_envelope_requires_a_tz_aware_created_at_passed_by_the_caller:153`
(nothing reads a clock inside the contract module),
`::test_observation_from_call_result_binds_identity_and_numbers:447`,
`::test_observation_from_call_result_rejects_a_non_integer_content_version:480`,
`::test_record_observation_round_trips_every_field:531`,
`::test_observation_versions_are_monotonic_per_source:595`,
`::test_observations_address_one_source_at_a_time:576`,
`::test_record_observation_refuses_an_envelope_addressing_another_attachment:626`,
`::test_record_observation_on_an_unknown_attachment_raises_thread_gone:637`.
Late-write protection is the store's, inherited from T2c and re-proved for
this new row type: `::test_delete_outranks_a_late_observation_write:646`,
`::test_thread_removal_blocks_a_late_observation_write:657`, and at the
preparation door `tests/host/test_attachment_prepare_vision.py::test_an_observation_written_after_a_delete_loses_the_race:656`.

**A2 — `covered_details` pair a structured detail key with the scope actually
observed.** `84af537`. `CoveredDetail` at `attachment_observations.py:154-170`
(`scope is None` means the whole rendered page), and the promise is enforced
where it is written: an envelope refuses a covered detail whose scope exceeds
the observed scope (`:238-251`). Evidence:
`test_attachment_observations.py::test_envelope_refuses_a_covered_detail_scope_beyond_the_observed_scope:159`.
The first-preparation scan grants exactly one whole-page detail,
`CHART_PRESENCE_DETAIL = "chart_presence"`
(`attachment_prepare.py:112`, used at `:729`) — the scan prompt's question
("is a chart here, and what does it say", `attachment_vision.py:58-63`) is
what that call can honestly license, so a plotted-value question is
insufficient by construction and routes to A6 rather than being granted.

**A3 — a visual question supplies structured `required_details` and a
requested scope, after the candidate observation is read into the run.**
`84af537` + `f88bba8`. `DetailKey` (kind plus optional complete target) at
`attachment_observations.py:99-110`; the read side is the store's
`observations(attachment_id, version, unit, page)`
(`attachment_store.py:236`, both adapters), and `inspect_for_details` takes
the run's already-read set as `run_observations`
(`attachment_vision_inspect.py:112`) together with the caller's
`required_details` and `requested_scope`. The mapping from a provider answer
to detail keys is a caller-supplied callable
(`attachment_vision_inspect.py:120-122`), so nothing in the host reads prose
for coverage. Evidence:
`test_attachment_vision_inspect.py::test_exact_key_with_scope_not_contained_is_insufficient:243`,
`::test_joint_coverage_across_two_run_observations_makes_no_call:208`,
`::test_single_covering_observation_returns_the_read_set_unchanged:228`,
and at the contract level `test_attachment_observations.py::test_single_observation_reuse_after_a_store_round_trip:670`.
The residual half of this clause — the grounded model producing those keys
inside a conversational turn — has no product caller yet; see *Leftovers* 5.

**A4 — reuse only when source/version/hash match, the schema is supported,
every required key matches exactly, and the observed scope contains the
requested scope.** `84af537`. `_identity_matches` (`attachment_observations.py:321-337`)
and `_scope_contains` (`:307-318`), composed in `observation_covers`
(`:401-425`) and `observations_jointly_cover` (`:353-398`). Evidence:
`test_attachment_observations.py::test_exact_key_whole_page_observation_covers_whole_page_request:198`,
`::test_whole_page_coverage_contains_a_crop_request:202`,
`::test_crop_coverage_never_answers_a_whole_page_request:206`,
`::test_crop_coverage_contains_only_crops_fully_inside_it:214`,
`::test_page_image_hash_mismatch_is_insufficient:267`,
`::test_attachment_content_version_mismatch_is_insufficient:277`,
`::test_unknown_coverage_schema_is_insufficient_on_both_sides:284`
(an unknown schema voids reuse wholesale rather than guessing),
`::test_absent_detail_is_insufficient:223`,
`::test_empty_required_details_is_insufficient:301`,
`::test_unknown_requested_scope_shape_is_insufficient:307`.
Joint coverage across several current-run observations is allowed only for one
source unit and page, and every row still passes the identity checks:
`::test_two_crop_observations_jointly_cover_two_required_details:314`,
`::test_joint_coverage_refuses_observations_from_another_page_or_unit:370`,
`::test_joint_coverage_applies_the_identity_checks_to_every_row:410`,
`::test_joint_coverage_of_an_empty_set_is_insufficient:430`.

**A5 — value details bind an exact chart, series and category; a generic
`plotted_value` facet, a missing or ambiguous target, a missing detail, or an
unknown scope is insufficient; no semantic or fuzzy comparison.** `84af537`.
`plotted_value_key` (`attachment_observations.py:113-134`) canonicalizes the
three-part target and refuses a part containing a separator, so the key
round-trips unambiguously; `_completes_plotted_target` (`:137-148`) is the
grant test, and a request whose own target is incomplete is refused before any
comparison (`:386-395`). Evidence:
`test_attachment_observations.py::test_plotted_value_key_requires_all_three_binding_parts:182`,
`::test_detail_key_rejects_a_blank_kind_and_blank_target:175`,
`::test_unrelated_plotted_value_does_not_answer_the_exact_chart_series_category_request:227`,
`::test_generic_plotted_value_requirement_is_always_insufficient:239`,
`::test_generic_plotted_value_coverage_never_covers_a_specific_request:253`,
`::test_missing_target_is_insufficient_for_a_plotted_value_requirement:260`.
The comparison is `covered.key != key` — dataclass equality on two string
fields — and no substring, case, or similarity rule appears on the path.

**A6 — insufficient or unknown coverage inspects the stored original at high
resolution, using a reliable crop when the region is known and the full page
otherwise, then persists a new observation version.** `f88bba8`.
`inspect_for_details` (`attachment_vision_inspect.py:103-132`): `crop=None`
means "target or region unknown" and takes the full-page path; a supplied crop
takes exactly one crop call (`_send_crop`, `:211-231`). The new version is
minted against the same source's own sequence
(`:178-181`, highest prior plus one), and the envelope keeps the identity it
was asked under. Evidence:
`test_attachment_vision_inspect.py::test_reliable_crop_sends_one_charged_call_with_the_question:265`,
`::test_whole_page_reinspection_records_scope_as_whole_page:445`,
`::test_new_envelope_continues_the_source_sequence_and_keeps_identity:399`,
`::test_two_crops_in_one_run_each_take_their_own_charge_and_version:500`,
`::test_crop_answer_claiming_more_than_the_crop_is_refused:457`
(a crop answer may not grant coverage beyond the region it looked at — the
A2 containment rule re-applied at the write), and the input refusals
`::test_empty_image_is_refused_before_any_charge:523`,
`::test_blank_question_is_refused_before_any_charge:534`.
The raster the inspection sends is **caller-supplied bytes**: the module
never opens a file or re-renders, and an empty image is refused before any
charge (`:134-137`). This is the seam a durable-render owner plugs into —
see *Leftovers* 1.

**A7 — the first preparation calls vision once per physical page, including
pages without Heron figure boxes.** `4d567cb`.
`AttachmentPreparationService._scan_pages` (`attachment_prepare.py:662-740`),
reached from `_parse` at `:657` after the units freeze; every page the core
returned is scanned, a page with no box and a page that froze no unit
included, and a unitless page is addressed where its unit would have started
(`:716-721`, the core's unit ordinals are physical page numbers). Evidence:
`tests/host/test_attachment_prepare_vision.py::test_first_preparation_scans_every_physical_page_once:357`
(one call per page of the committed gold PDF, in page order, over that page's
own raster), `::test_a_unitless_page_is_scanned_and_addressed_by_its_page_number:386`,
`::test_a_standalone_image_gets_one_full_page_scan:407`,
`::test_a_no_chart_answer_is_a_successful_persisted_observation:421`
("no chart" is an answer, not a failure), `::test_observation_records_hash_model_usage_and_reported_cost:557`,
and the lanes that must *not* reach vision:
`::test_a_text_only_upload_never_reaches_vision:605`,
`::test_a_refused_page_count_spends_no_vision_call:619`,
`::test_a_page_lane_without_a_vision_port_freezes_units_and_spends_nothing:636`
(a deployment with no scan port is the pre-T2e shape: units freeze, nothing is
sent, nothing is charged, and the fact is logged rather than silent).

**A8 — an injected fin vision port separate from the conversational model
port, configured from `FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY`, adding no
separate vision credentials or model settings.** `5d8e43f`.
`VisionPort` is a single-method `Protocol` (`attachment_vision.py:310`),
`ChatModelVisionAdapter` the default implementation (`:350`), and the only
configuration it reads is those three names — `FIN_MODEL_ENV` /
`FIN_BASE_URL_ENV` declared at `:45-46`, `FIN_API_KEY_ENV` imported from
`fin.host.compile` at `:37` — resolved at `:342-343`
and `:435-438`. Evidence:
`tests/host/test_attachment_vision.py::test_port_is_a_single_method_protocol:112`,
`::test_model_and_base_url_come_from_configuration:406`,
`::test_unset_configuration_falls_back_to_the_hosts_defaults:415`,
`::test_no_model_or_endpoint_literal_and_no_extra_credential:423`
(a scan over the source for a model/endpoint literal and for any fourth
credential name), `::test_default_client_is_built_from_the_resolved_configuration:453`,
`::test_real_client_import_is_lazy:471`,
`::test_missing_credential_is_a_typed_call_failure_naming_the_env_var:440`,
`::test_image_bytes_and_identity_reach_the_provider_once:148`,
`::test_inspect_returns_text_target_and_reported_usage:127`,
`::test_multimodal_parts_content_is_flattened_to_text:170`,
`::test_token_usage_spelling_is_also_read:177`.

**A9 — at most eight crop calls, one reserved full-page transport retry per
run, retried only for an adapter-classified retryable transport failure and
never for a non-retryable error, malformed or empty output, or a crop call;
every outbound attempt consumes one of the 89 calls.** `3a7ffa3` (the reserve
and the ledger) + `84af537` (the classification) + `4d567cb` / `f88bba8` (the
spend sites). `RETRY_RESERVE_CALLS = 1` (`attachment_budgets.py:53`), the run
price in one place — `required_run_calls = pages + crops + reserve` (`:143-153`)
— enforced by `validate` (`:156-175`) and by `preflight_run` (`:226-259`);
`VisionCallLedger` (`:261-378`) charges before the request leaves, under a
lock so two attempts cannot each see room for the last call, with the reserve
owned by the run rather than by one page. Classification is data on the error,
not a re-read of a message: `VisionErrorClass` (`attachment_vision.py:79-131`),
`_classify_provider_error` (`:140-161`), `.retryable` (`:188-189`). Evidence,
budget side: `tests/host/test_attachment_budgets.py::test_the_retry_reserve_is_one_per_run_not_one_per_page:848`,
`::test_ledger_starts_unspent_and_accounts_every_attempt_to_one_run_total:766`,
`::test_every_call_the_policy_allowance_admits_is_charged_and_one_more_is_refused:794`,
`::test_a_failed_attempt_consumes_budget_so_retries_cannot_run_the_tab:818`,
`::test_the_crop_sub_cap_holds_even_while_the_run_total_has_room:870`,
`::test_concurrent_attempts_cannot_double_spend_the_last_call:925`,
`::test_a_malformed_retry_ask_is_a_typed_caller_error_and_spends_nothing:965`.
Evidence, preparation side:
`test_attachment_prepare_vision.py::test_the_reserve_is_the_runs_not_the_pages:469`,
`::test_retryable_transport_failure_spends_the_reserve_and_succeeds:494`,
`::test_second_retryable_failure_exits_vision_call_failed:509`,
`::test_non_retryable_failure_is_never_retried:526`,
`::test_empty_output_exits_vision_empty_text_without_retry:539`,
`::test_failed_attempts_are_charged_and_overflow_refuses_before_sending:444`
(the 89th ask is refused with nothing sent). Evidence, inspection side:
`test_attachment_vision_inspect.py::test_full_page_retryable_failure_spends_the_reserved_retry_once:310`,
`::test_spent_reserve_leaves_a_retryable_failure_unretried:367`,
`::test_second_full_page_failure_is_final:356`,
`::test_permanent_full_page_failure_is_not_retried:332`,
`::test_full_page_empty_answer_is_not_retried:345`,
`::test_retryable_transport_failure_on_a_crop_is_never_retried:281`,
`::test_ninth_crop_of_a_run_is_refused_before_sending:382`.
Evidence, classification unit:
`test_attachment_vision.py::test_transport_faults_are_classified_retryable:258`
(parameterized over the timeout / connection / socket / DNS shapes),
`::test_dns_gaierror_shape_is_retryable_transport:265`,
`::test_provider_refusals_are_classified_non_retryable:294`
(401/403, quota and permission text are adjudicated before any transport cue),
`::test_unclassifiable_fault_is_conservatively_non_retryable:301`,
`::test_empty_output_error_is_never_retryable:317`,
`::test_unset_credential_failure_is_not_a_transport_fault:327`,
`::test_classification_leaves_the_message_class_name_only:309`,
`::test_successful_call_is_unaffected_by_classification:339`.

**A10 — `.env.example` updated to 89 and the page + crop + retry relationship
enforced at boot/preflight.** `3a7ffa3` (value and validator) + `5f11dcb`
(the root that reads it). `.env.example:88-91` now reads
`FIN_ATTACHMENT_VISION_CALLS_PER_RUN=89` with the four bounds beside it, and
the comment states what 89 is — a policy allowance of 80 scans plus 8 crop
calls plus exactly one reserved full-page retry, not measured demand and not
yet the shipped product default. Evidence:
`test_attachment_budgets.py::test_run_budget_must_cover_pages_plus_crop_headroom_plus_one_retry:469`,
`::test_the_deployed_policy_allowance_reserves_a_retry_and_the_old_floor_does_not:499`
(88 is now a config error, 89 is not),
`::test_ledger_rejects_a_deployment_that_leaves_no_room_for_the_retry:889`,
`::test_deployed_vision_total_is_the_policy_allowance_with_its_reserve:1023`
(the template value itself, re-read from `.env.example`, is what the deployed
total must equal),
`::test_serve_aborts_boot_on_a_total_that_leaves_no_retry_reserve:1283`.
The boot fixture derives its own run total through `required_run_calls`
instead of restating a number (`0458100`,
`tests/host/test_scripted_boot.py:350-368`).

**A11 — no monetary ceiling; record provider-reported usage and cost per
attempt, marking cost unavailable when the provider omits it.** `5d8e43f` +
`84af537`. `VisionUsage` / `VisionCost` / `VisionCallResult`
(`attachment_vision.py:273-308`), `ObservationAttempt`
(`attachment_observations.py:173-193`) where `cost is None` is *unavailable*
and a reported zero arrives as a `VisionCost` and stays distinguishable from
silence; a retried-away failure keeps its own attempt record with no invented
zeros (`attachment_vision_inspect.py:197-203`). Evidence:
`test_attachment_vision.py::test_unreported_cost_is_unavailable_never_zero:366`,
`::test_reported_cost_is_kept_including_a_reported_zero:371`,
`::test_usage_absent_entirely_is_none_not_zeros:389`,
`::test_no_ceiling_is_enforced_here:395`,
`test_attachment_observations.py::test_unreported_cost_round_trips_as_absent_not_zero:549`,
`test_attachment_prepare_vision.py::test_cost_the_provider_omits_is_recorded_as_unavailable:586`,
`test_attachment_vision_inspect.py::test_reported_usage_and_absent_cost_are_recorded_per_attempt:472`,
`::test_reported_zero_cost_stays_distinct_from_silence:485`.
The live leg (command 3) observed exactly the promised shape: the provider
reported `input_tokens=1403, output_tokens=681, total_tokens=2084` and no
cost, recorded as `cost=None`. Per-run aggregation of these per-attempt rows
is not delivered — see *Leftovers* 3.

**A12 — `vision_call_failed`, `vision_empty_text`,
`attachment_vision_budget_exceeded` as typed exits, never an empty success.**
`5d8e43f` + `3a7ffa3`. `VISION_CALL_FAILED` / `VISION_EMPTY_TEXT`
(`attachment_vision.py:49-50`) carried by `AttachmentVisionCallFailedError`
(`attachment_prepare.py:235-252`, which also carries the adapter's
`retryable` verdict) and `AttachmentVisionEmptyTextError` (`:255-263`);
`ATTACHMENT_VISION_BUDGET_EXCEEDED` (`attachment_budgets.py:47`) carried by
`AttachmentVisionBudgetExceeded` (`:86-89`), whose `code` is what `_freeze`
writes to the row (`:516-530`). A budget *config* error is deliberately absent
from that catch list: a deployed bound that cannot be read is an operator's
broken startup, not a client-retryable product exit. Evidence:
`test_attachment_prepare_vision.py::test_the_vision_exits_are_caught_preparation_codes:343`,
`test_attachment_vision.py::test_provider_transport_fault_is_vision_call_failed:192`,
`::test_empty_output_is_vision_empty_text_not_a_blank_success:204`,
`::test_the_two_failure_conditions_stay_distinct:211`, plus the ledger rows
cited in A9 and `test_attachment_vision_inspect.py::test_unusable_crop_answer_is_the_ports_typed_exit:296`.

**A13 — no provider/model ID hardcoded, no local VLM, no separate picture
classifier.** Same evidence as A8: `test_attachment_vision.py::test_no_model_or_endpoint_literal_and_no_extra_credential:423`
and `::test_real_client_import_is_lazy:471`; the port's only dependency is the
host's existing chat-model client, and the identity that lands in an envelope
is the model that actually answered (`provider=result.model_id`,
`attachment_prepare.py:726`, `attachment_vision_inspect.py:193`) — no
second name is invented. The live run recorded
`model=MiniMax-M3 (=host default)`, i.e. the value the configuration resolved
to, not a literal in `src/`.

**Composition root — the page lane deployed (the wiring T2d assigned to this
ticket).** `5f11dcb`. `_build_default_attachment_service`
(`workspace_http.py:2276-2358`) now constructs the service with
`budgets=…, pages=…, vision=…` (`:2357`): the four bounds through the boot
singleton the server itself reads before binding, the Heron page-lane parser
from `HeronLayoutConfig.from_environ` (`:2330-2332`), and one
`ChatModelVisionAdapter` (`:2335`) whose client is lazy, so boot pays no
provider import. Both config reads happen before any pool is opened or byte
stored. Evidence:
`tests/host/test_workspace_http_attachment_lane.py::test_the_root_boots_with_the_page_lane_deployed:138`,
`::test_booting_the_lane_touches_no_container_no_binary_no_provider:160`,
`::test_a_malformed_heron_knob_fails_the_build_rather_than_guessing:186`,
`::test_a_missing_bound_refuses_the_build_before_any_door_is_served:201`,
`::test_the_lane_serves_a_text_document_without_the_byte_store:215`,
`::test_one_pdf_earns_one_staged_copy_holding_its_own_bytes:252`,
`::test_two_documents_never_share_one_staged_copy:279`,
`::test_a_host_without_the_harness_still_serves_the_page_lane:301`,
`::test_a_staged_copy_is_gone_even_when_the_document_cannot_be_read:325`,
`::test_a_standalone_image_never_stages_a_heron_document:350`.
The door reverses T2d's pinned refusal:
`tests/host/test_attachment_prepare_door.py::test_the_shipped_root_builds_no_door_that_can_refuse_a_page_document:202`
and `::test_a_deployed_lane_freezes_the_page_document_it_once_refused:259`,
while the lane-less service still answers its named code instead of escaping
(`::test_the_lane_less_door_still_names_its_code_instead_of_escaping:398`,
`::test_the_deployed_door_answers_200_and_agrees_with_itself:287`).

## Commands, output, exit status

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 1 | `uv run pytest tests/host/test_attachment_observations.py tests/host/test_attachment_prepare_vision.py tests/host/test_attachment_vision_inspect.py tests/host/test_attachment_vision.py tests/host/test_attachment_budgets.py -q` | 0 | `321 passed, 6 skipped in 8.48s` — the 6 skips are the five `test_postgres_*` observation cases (`FIN_CHECKPOINT_URL is not set`) and `test_live_round_trip_against_the_configured_provider` (`FIN_VISION_LIVE_ROUNDTRIP=1 not set: live case not selected`; row 3 runs it as a real call). Per-file collection 40 + 17 + 20 + 38 + 212 = 327, which is exactly the 321 + 6 |
| 2 | `uv run pytest tests/host/test_attachment_prepare_door.py tests/host/test_workspace_http_attachment_lane.py tests/host/test_scripted_boot.py tests/host/test_attachment_prepare_pages.py tests/host/test_attachment_prepare_image.py -q` | 0 | `102 passed, 7 warnings in 6.76s` — the T2d-pinned suites re-green against the wired root |
| 3 | `FIN_VISION_LIVE_ROUNDTRIP=1 uv run pytest -q -s tests/host/test_attachment_vision.py -k live_round_trip` | 0 | `1 passed, 37 deselected in 6.07s`, one real provider call over the committed `data/corpus/g4_attachment_gold/g4-image-chart-001.png`, printed by the case itself: `[vision-live] model=MiniMax-M3 (=host default) chars=2363 usage=VisionUsage(input_tokens=1403, output_tokens=681, total_tokens=2084) cost=None`. An earlier run of the same command (`1 passed … in 5.91s`) reported `chars=2199, output_tokens=654, total_tokens=2057, cost=None`: the answer length is the provider's, and the two things the contract asserts — non-empty text and a model id — plus `cost=None` (omitted, therefore unavailable, never zero) held across both. No key value appears here |
| 4 | `grep -n "^FIN_ATTACHMENT\|^#FIN_ATTACHMENT" .env.example` | 0 | `.env.example:88-91` the four bounds with `FIN_ATTACHMENT_VISION_CALLS_PER_RUN=89`, and `:103-118` the six `FIN_ATTACHMENT_HERON_*` rows (`SCRIPT`, `STAGING_DIR`, `TIMEOUT_S=900`, `IMAGE`, `MODEL_CACHE`, `SLICE_PASS=false`) — T2d's "appear nowhere in `.env.example`" leftover closed, optional keys commented out so an unset key takes the named shipped default while a malformed value fails boot |

```text
Validation:
- Not run: the durable Postgres leg of observation persistence —
  tests/host/test_attachment_observations.py::test_postgres_record_observation_round_trips_every_field:745,
  ::test_postgres_observations_cascade_with_deletion:757,
  ::test_postgres_tombstone_blocks_a_late_observation:775,
  ::test_postgres_stale_observation_version_is_refused:786 and
  ::test_observation_methods_are_on_both_protocol_implementations:801.
- Expected command: start `fin-postgres-1` and export `FIN_CHECKPOINT_URL`,
  then `uv run pytest -q tests/host/test_attachment_observations.py`.
- Blocking issue: this session cannot reach the docker daemon
  (`docker info` → `permission denied … /var/run/docker.sock`) and the worktree
  carries no `.env`, so `FIN_CHECKPOINT_URL` is structurally absent — the same
  bound T2d recorded for its `jev_live` leg. These five rows are blocked
  evidence, not a green skip: the same behaviors are pinned on the in-memory
  adapter (rows cited in A1/A4), and `test_observation_methods_are_on_both_protocol_implementations`
  is the case that proves the Postgres adapter exposes the same two methods.
- Not run: a real Heron container pass on the wired root. No T2e test needs the
  container (row 2's suite pins that boot touches no container, binary, or
  provider); the harness crossing belongs to the lane's own owner, T2d's
  record, and to T2f's gold round.
```

## Review repair rounds (final branch state `4785dc6` + docs `bba07dd`)

After this record was written at `5f11dcb`, the code-review phase returned one
boot regression and two Highs. All three were repaired on this branch before
it lands; this section is their record. Commits, oldest first, each verified
with `git show --stat` before it was named here:

| commit | what it repaired |
| --- | --- |
| `28d2484` | the boot regression — the root now gates the page lane on *declared* bounds instead of reading the budget unconditionally at boot — and the run-vs-preparation ledger High: a run-wide `VisionCallLedger` is now an injected seam |
| `21a0e51` | the spec High — a stored observation now carries the provider's answer text, not only its provenance shell |
| `d50e83b` | the integration commit — both vision seams delegate to one full-page send ladder, a spent reserve with run headroom exits the provider's failure rather than the budget code on the preparation seam, and re-inspection page addressing becomes a `PageIdentity` value object |

The anchors and line numbers in *Per-clause evidence* above were
grep-verified at `5f11dcb`; the repair rounds moved some of them. Every
anchor and every `file::test` name below was grep-verified against the final
tip `d50e83b`.

**R1 — bounds-gated page lane, run-wide ledger seam (`28d2484`).** The boot
regression: at `5f11dcb` the root read the four `FIN_ATTACHMENT_*` bounds
unconditionally, so a process that configured none failed at boot on that
read and the shipped text-only door became unreachable through the builder —
the very shape `attachment_page_lane_unwired` exists to answer. The builder
now gates on whether the deployment *declared* any bound —
`lane_declared = any(key in os.environ for key, _ in _ENV_BOUNDS)`
(`workspace_http.py:2345`; `_ENV_BOUNDS` is imported at `:2343` for the key
names only, so the gate cannot drift from what `from_environ` reads): a
declared set still boots the lane, a partially declared set still refuses the
build (`test_a_missing_bound_refuses_the_build_before_any_door_is_served:221`
in `tests/host/test_workspace_http_attachment_lane.py`), and nothing declared
logs the fact and keeps the lane-less door (`workspace_http.py:2361-2368`),
whose named `attachment_page_lane_unwired` answers stay pinned by
`tests/host/test_attachment_prepare_door.py::test_the_lane_less_door_still_names_its_code_instead_of_escaping:398`.
The ledger High: `AttachmentPreparationService` takes
`call_ledger: VisionCallLedger | None = None` (`attachment_prepare.py:412`,
the seam spelled out at `:428-433`, stored at `:442`) and `_scan_pages`
charges the injected run ledger when the caller priced the whole run,
otherwise its own (`:710-713`) — so the 89-call allowance and the single
reserved retry bound a run across attachments instead of silently resetting
per document. Evidence:
`test_workspace_http_attachment_lane.py::test_one_injected_ledger_prices_every_preparation_against_the_run:478`
and its default-pinning mate
`::test_without_an_injected_ledger_each_preparation_prices_itself:500`.

**R2 — the answer text persists with the envelope (`21a0e51`).** A stored
observation carried provenance and covered-detail shells but dropped
`VisionCallResult.text`, so a "sufficient" reuse verdict could have grounded
an answer with nothing citable. `ObservationEnvelope.answer_text`
(`attachment_observations.py:225`) is a required string — empty is reserved
for the pre-content shape, a non-string is refused (`:242-245`) — and the
factory always copies the provider's answer (`answer_text=result.text`,
`:317`). Both store backends gained an
`answer_text TEXT NOT NULL DEFAULT ''` column: fresh installs through the
table shape (`attachment_store.py:1049`), existing installs through the
repo's idempotent `ADD COLUMN IF NOT EXISTS` convention inside the same
`_SCHEMA` (`:1061`); the decode now binds through one named column list
(`_OBS_COLUMN_NAMES`, `:1458`) instead of positional `row[10..13]` offsets,
and the upsert writes the column (`:1648`, `:1668`). Evidence:
`tests/host/test_attachment_observations.py::test_envelope_defaults_the_answer_payload_to_empty:816`,
`::test_observation_from_call_result_copies_the_provider_answer_text:822`,
`::test_record_observation_round_trips_the_provider_answer_text:845`,
`::test_postgres_record_observation_round_trips_the_provider_answer_text:859`
— the latter ran against a real server in row 6 below, where the same-named
gap the *Validation not run* block above recorded is now closed — and
`tests/host/test_attachment_store.py::test_observation_answer_text_round_trips_on_both_backends:1323`.

**R3 — one full-page send ladder, `PageIdentity` (`d50e83b`).** Preparation
and re-inspection had each hand-rolled the same charge → inspect →
retry-once ladder, and the copies disagreed on exit codes: with the run's
reserve already spent but the run total still holding calls, a retryable
failure on the preparation seam reported `attachment_vision_budget_exceeded`
when what the run had hit was the provider's dropped request, not an operator
cap refusing anything. The ladder now lives once as `send_full_page`
(`attachment_budgets.py:409`, module contract at `:19-23`): charge before
the send, retry only an adapter-classified transport failure, spend the
reserve at most once, and let the port's typed errors raise unchanged to each
seam. Both seams delegate (`attachment_prepare.py:792`;
`attachment_vision_inspect.py:377`, inside `_send_full_page` at `:367`), so
the flipped verdict is one policy rather than a second copy: a spent reserve
with headroom exits `vision_call_failed` —
`tests/host/test_attachment_prepare_vision.py::test_failed_attempts_are_charged_and_a_spent_reserve_exits_the_provider_failure:451`
(this is the A9 row `test_failed_attempts_are_charged_and_overflow_refuses_before_sending`
flipped to the corrected name and code, the same flip for
`test_the_reserve_is_the_runs_not_the_pages` at
`::test_the_reserve_belongs_to_the_run_and_its_exhaustion_exits_the_provider_failure:478`),
while a true run-total overflow on the retry's own charge still exits the
budget code (`::test_a_run_total_exhausted_shared_ledger_keeps_the_budget_exit:506`).
Ladder unit evidence:
`tests/host/test_attachment_budgets.py::test_send_full_page_answers_and_charges_exactly_one_attempt:1060`,
`::test_send_full_page_never_retries_a_permanent_refusal:1078`,
`::test_send_full_page_spends_the_run_reserve_once_on_a_transport_failure:1094`,
`::test_send_full_page_a_second_failure_is_final_and_never_retries_twice:1110`,
`::test_send_full_page_a_spent_reserve_exits_the_provider_failure:1125`,
`::test_send_full_page_run_total_exhaustion_on_the_retry_charge_keeps_the_budget_exit:1145`,
`::test_send_full_page_empty_output_is_never_retried:1163`.
The re-inspection's five clumped page-addressing keywords collapsed into one
`PageIdentity` value object (`attachment_vision_inspect.py:85`, bridging to
the provider shape through `to_target` at `:102`); `inspect_for_details`
(`:142`) accepts either `identity` or the legacy flat keywords — exactly one
form, resolved by the single adapter branch `_resolve_identity`
(`:177`, `:200`) — so the envelope's provenance and the call's target can no
longer drift apart field by field. Evidence:
`tests/host/test_attachment_vision_inspect.py::test_flat_keywords_and_identity_object_yield_the_same_outcome:587`
(parameterized over crop and full-page),
`::test_both_identity_forms_at_once_is_refused_before_any_charge:607`,
`::test_neither_identity_form_is_refused_before_any_charge:618`,
`::test_incomplete_flat_fields_or_stray_page_field_is_refused:638`,
`::test_identity_object_alone_drives_the_reuse_verdict:662`.

**R4 — second-pass closures (`4785dc6`).** The four Mediums the second
Standards + Spec pass reported on `ab604d1..025b1c4` closed in one change:
the composition-root gate reads the now-public `BOUND_ENV_KEYS`
(`attachment_budgets.py:75`, in `__all__`) instead of the module-private
name; the nothing-declared branch the Spec pass found unpinned is pinned by
`tests/host/test_workspace_http_attachment_lane.py::test_a_deployment_declaring_no_bounds_builds_the_unwired_text_door:238`;
`_scan_pages` gives its observation assembly to `_page_observation`
(`attachment_prepare.py:739`) and sits at 60 lines; the flat-keyword
compatibility form R3 introduced is deleted — `inspect_for_details` takes
`PageIdentity` only — so the three refusal tests above shrink to the
identity-only validations
(`tests/host/test_attachment_vision_inspect.py::test_a_missing_identity_is_refused_before_any_charge`,
`::test_a_none_identity_is_refused_before_any_charge`,
`::test_a_stray_page_keyword_is_refused`), and R3's dual-form description is
superseded by this paragraph. The 1114-line `attachment_prepare.py` stays a
named accepted breach of the 1000-line soft gate for a later split ticket.
Evidence: `uv run pytest` over the ten T2e files → `485 passed, 26 skipped`;
the same files' Postgres legs at the live DSN → `161 passed`.

## Commands, output, exit status — repair rounds

Re-run at the final tip `d50e83b` with the *Environment / dataset version*
boundaries unchanged, plus one new one: row 6 supplies the live DSN as
`FIN_CHECKPOINT_URL` from the main checkout's operator `.env` (a worktree
carries no `.env`) — the value appears nowhere in this file — and row 7
supplies `FIN_MODEL` / `FIN_BASE_URL` / `FIN_API_KEY` the same way, again
with no key value recorded.

| # | Command | Exit | Result |
| --- | --- | --- | --- |
| 5 | `uv run pytest tests/host/test_attachment_vision_inspect.py tests/host/test_attachment_budgets.py tests/host/test_attachment_prepare_vision.py tests/host/test_attachment_observations.py tests/host/test_attachment_vision.py tests/host/test_attachment_prepare_pages.py tests/host/test_attachment_prepare_door.py tests/host/test_workspace_http_attachment_lane.py -q` | 0 | `401 passed, 7 skipped in 5.24s` — row 1's five-file hermetic set widened to the three files the repair rounds and the wiring touched. The skips are the six `test_postgres_*` observation cases — the five *Validation not run* above plus the R2 case at `test_attachment_observations.py:859` — and the live round trip at `test_attachment_vision.py:535` (not selected; row 7 runs it) |
| 6 | `FIN_CHECKPOINT_URL=<the operator .env's DSN, value not recorded here> uv run pytest tests/host/test_attachment_observations.py tests/host/test_attachment_store.py tests/host/test_thread_attachment_durable_postgres.py tests/host/test_attachment_prepare_vision.py tests/host/test_attachment_vision_inspect.py -q` | 0 | `170 passed in 8.15s` — the durable Postgres legs the *Validation not run* block above had to record as blocked now ran against a live server, including R2's `::test_postgres_record_observation_round_trips_the_provider_answer_text` and the observation-answer round trip on both backends |
| 7 | `FIN_VISION_LIVE_ROUNDTRIP=1 uv run pytest -q -s tests/host/test_attachment_vision.py -k live_round_trip` | 0 | `1 passed, 37 deselected in 10.56s`, one real provider call over the committed `g4-image-chart-001.png`, printed by the case: `[vision-live] model=MiniMax-M3 (=host default) chars=2140 usage=VisionUsage(input_tokens=1403, output_tokens=615, total_tokens=2018) cost=None`. A second run of the same command at the same tip printed `chars=2279, output_tokens=649, total_tokens=2052, cost=None`: the answer length is the provider's, and the contract's own asserts — non-empty text, the resolved model id, and cost omitted therefore unavailable and never zero (A11) — held across both |
| 8 | `FIN_CHECKPOINT_URL=<…> uv run pytest -m mcp tests/host/test_live_attachment_upload_door.py -q` | 1 | `2 failed, 10 passed, 2 skipped in 6.03s`. The two skips are the gated real-store legs — `test_live_upload_bytes_persist_after_uvicorn_exit_in_fresh_adapter_process:1135` (live-server upload read-back from a fresh process) and `test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart:1364` (cleanup-failure restart) — both naming their missing prerequisite `FIN_OBJECT_STORE_*`, which this session cannot supply: the docker daemon is unreachable, so the MinIO bucket cannot come up. That is **blocked evidence, never a green skip**. A third real-store leg, `test_live_single_attachment_delete_voids_the_real_bytes:1296`, asserts against the real bucket from inside its body without taking the `real_s3_object_store` gate, so it *failed* (`ObjectStoreNotConfiguredError` at its first read, `:1323`) rather than skipping — the same missing prerequisite, surfaced as a failure; the test's own gating is a repair for whoever owns that suite, not this record's scope. Its incomplete delete is what the module's accounting check `test_module_leaves_the_real_tables_as_it_found_them:1448` fails for: that leg has since taken the `real_s3_object_store` gate it was missing (repair at `4785dc6`+), so the suite now reads `11 passed, 3 skipped, exit 0` — the ungated failure and the table residue it left are gone, the module's accounting check `test_module_leaves_the_real_tables_as_it_found_them:1449` passes, and only the two genuinely store-dependent legs remain blocked evidence awaiting the operator's `FIN_OBJECT_STORE_*` re-run |
| 9 | `uv run pytest -m mcp tests/retrieve/test_mcp_smoke.py -q` | 0 | `20 passed in 1.42s` — the MCP protocol surface against the local fixture server; protocol evidence only, explicitly not a live iknow-memory round trip. Recorded because this branch touches no retrieve code, and row 9 shows that boundary is undisturbed |

## Leftovers

1. **The run-wide ledger is an injected seam whose deployed default is
   per-preparation.** R1 opened the seam (`call_ledger`,
   `attachment_prepare.py:412`) and one caller pricing a whole run can share
   a single ledger across attachments
   (`test_one_injected_ledger_prices_every_preparation_against_the_run:478`),
   but nothing in the product path yet builds that per-run orchestrator: the
   default stays `::test_without_an_injected_ledger_each_preparation_prices_itself:500`,
   and the root injects no ledger. Run orchestration — who holds the one
   ledger for a turn's attachments — belongs to **T2f**, which owns the run
   record.
2. **Page rasters are not durably stored — only their SHA-256 is.** The
   envelope binds `page_image_sha256` (computed over the rendered PNG at
   `fin/corpus/parsers/_pages.py:150`), and nothing writes the pixels
   anywhere: `attachment_prepare.py:722` hands `page.raster.png` to the port
   and drops it. So every re-inspection caller must **re-render** and then
   prove the hash matches the stored envelope before the bytes may answer a
   question — which is exactly why `inspect_for_details` takes the image as a
   caller-supplied argument (`attachment_vision_inspect.py:146`, refused when
   empty at `:178-181`) rather than holding a render seam of its own. A
   re-render is not byte-identical by assumption: a raster whose hash drifted
   voids reuse under A4 instead of quietly answering from a different image.
   The durable-render seam (where the pixels live, with what provenance and
   what cap) lands with **T2f**'s job, which owns the preparation run record.
3. **Text-labelled regions still have no Heron box** (T2d's leftover,
   unchanged by this ticket and by the repair rounds). `_BOXED_KINDS` is
   still `("table", "figure")` (`attachment_layout.py:85`), so a question
   about detail inside a `text`-labelled region has no reliable crop to pass
   as `inspect_for_details(crop=…)` and takes the full-page fallback. This is
   a harness-record plus adapter-shape change, not a core change
   (`REGION_KINDS` already contains `"text"`), and it costs one full-page
   call per such question while it stays open.
4. **Per-run usage and cost are not aggregated durably.** Attempts are
   persisted per observation envelope (`ObservationAttempt`, A11) together
   with the answer text R2 added, and the ledger's counters — `calls_spent`,
   `full_page_calls_spent`, `crop_calls_spent`, `retries_spent` — are **per
   object, per process, per run by design** (`VisionCallLedger`'s own
   durability boundary, `attachment_budgets.py:276` / `:287-295`, names the
   four counters the future run record must copy). A restart forgets them;
   nothing in this tree reads a total across attachments, and the "and run"
   half of A9's clause is therefore unmet until **T2f**'s run record exists —
   R1 made a run *shareable* (leftover 1), not durable.
5. **The monetary ceiling stays deferred**, per the T2e0 decision recorded at
   plan item 5a (issue),
   and nothing here smuggles one in: `test_no_ceiling_is_enforced_here:395`
   pins that the port prices nothing, and the ledger counts calls, never
   money. What this ticket makes possible is the reading T2a *Leftovers* 2
   asked for — per-attempt provider usage is now recorded, as command 3 and
   row 7 above show for real calls — so the operator's ceiling, when it is
   decided, has a place to attach.
6. **No product turn calls the re-inspection seam yet.** `inspect_for_details`
   and the reuse predicate are exercised only by their own suites (grep over
   `src/fin` finds no other user, re-checked at `d50e83b`), because the turn
   that reads an observation into a run, asks the grounded model for
   `required_details` and a requested scope, and maps the answer onto
   covered keys, is the work T2f gates behind the durable job and T3 puts in
   front of retrieval. The half T2e owns — the host-side deterministic
   verdict, the charged re-inspection, the new version — is delivered and
   tested; the caller is not.
7. **`89` is a policy allowance, not a shipped default.** `.env.example` now
   states it and `validate()` now refuses anything below `pages + crops + 1`,
   but the running service reaches that template only when this branch ships,
   and T2a *Leftovers* 1 and 5 still stand: no 80-page attachment has been run
   at these four bounds, and no dedicated 4 GB device reading exists. Both are
   T2f's gold-round obligations, not this ticket's.
8. **The real object-store legs stay blocked this session.** Row 8 records
   the two gated durable-S3 legs skipping on their named prerequisite
   (`FIN_OBJECT_STORE_*`, with the docker daemon unreachable so no bucket can
   come up) are blocked evidence, never a green skip or a pass. The delete
   leg that had failed on the same absence without taking the gate now
   takes `real_s3_object_store` too, so the suite reads `11 passed, 3
   skipped` and the tables hold no residue from this session.
9. **Per-call image dimensions and vision latency are not recorded.** The
   spec's record-keeping clause (`specs/thread-attachment-source-read.md:57`)
   asks for page count, full-page and crop calls, image dimensions,
   provider-reported usage/cost, latency, and retry count. The call and
   retry counters (`VisionCallLedger`) and the per-attempt usage/cost
   (`ObservationAttempt`, A11) are delivered; dimensions and latency appear
   on no read of the shipped shapes — neither `ObservationEnvelope` nor
   `ObservationAttempt` carries them, and the port returns neither. They
   belong to **T2f**'s run record, which owns the durable per-run reading
   of these counters (leftover 4) and can attach the per-call observations
   there rather than on the per-attempt persistence row.
