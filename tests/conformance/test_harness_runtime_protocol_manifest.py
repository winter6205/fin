"""Offline conformance check for the frozen P4 harness-runtime protocols.

Every expectation here is derived INDEPENDENTLY of the artifact it checks: the
required mechanism items and browser journeys come from the matrix sentences in
``specs/harness-runtime-acceptance.md`` (Contract) and the P4 plan's T5/T6
acceptance text; the case counts come from the frozen ranges recorded in
``docs/verification/2026-10-21-harness-runtime-p4-protocol-freeze.md``; and the
service floors, denominator vocabulary, exit contract, and P10 non-blanket rule
come from the spec plus that freeze note. Nothing is read back out of the JSON
and compared against a copy of itself. The one internal-consistency check is
the gate-description test: the artifact's own per-record ``integration_gates``
are authoritative there, so each top-level gate description must cite exactly
the records that declare it.

The browser reporter test is a cross-file check: the Playwright CLI contract
(a ``--reporter`` flag replaces the configured reporter array) and
``playwright.config.mjs`` itself decide the expectation, so it pins the
recorded command and the config's reporters without running a browser.

Deterministic and offline: no network, database, browser, or model, and no
``eval`` marker, so a default ``uv run pytest -q`` collects it. It asserts the
frozen contract only; it does not claim any campaign result.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, NamedTuple

from fin.eval.acceptance.protocol import (
    case_manifest,
    case_spec,
    validate_case_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = REPO_ROOT / "data" / "conformance" / "harness_runtime"
MECHANISM_PATH = ASSET_DIR / "mechanism_protocol.json"
BROWSER_PATH = ASSET_DIR / "browser_protocol.json"
SPEC_PATH = REPO_ROOT / "specs" / "harness-runtime-acceptance.md"
PLAN_PATH = REPO_ROOT / "plans" / "harness-runtime-p4-acceptance-foundation.md"
FREEZE_PATH = (
    REPO_ROOT / "docs" / "verification" / "2026-10-21-harness-runtime-p4-protocol-freeze.md"
)
PLAYWRIGHT_CONFIG_PATH = REPO_ROOT / "playwright.config.mjs"

LANE_FILES: tuple[tuple[str, Path], ...] = (
    ("mechanism", MECHANISM_PATH),
    ("browser", BROWSER_PATH),
)
RECORD_KEYS = {"mechanism": "cases", "browser": "journeys"}
ID_KEYS = {"mechanism": "case_id", "browser": "journey_id"}
VERSION_KEYS = {"mechanism": "case_version", "browser": "journey_version"}
RACE_FIELD = {"mechanism": "barrier", "browser": "action"}
LANE_PREFIX = {"mechanism": "M", "browser": "B"}

# spec Contract (Mechanism evidence): a case may depend on P1..P7 integration,
# and a P10 rubric only as the per-case ``P10_freeze`` gate.
P_GATES = frozenset(f"P{n}" for n in range(1, 8))
ALLOWED_GATES = P_GATES | {"P10_freeze"}

# A status may say a lane did NOT pass; a bare claim that it did must fail.
_CLAIM = re.compile(r"\b(?:passed|accepted)\b", re.IGNORECASE)
_NEGATED_CLAIM = re.compile(
    r"\b(?:no|not|never|none|nothing|nor)\b[^.;]{0,120}?\b(?:passed|accepted)\b",
    re.IGNORECASE,
)
_VERSION_SHAPE = re.compile(r"p4-(mechanism|browser)-(\d+(?:\.\d+)+)")
_FREEZE_RANGE = re.compile(r"\b([MB])(\d{2})[–—-]([MB])(\d{2})\b")

# A record id is one uppercase letter plus two digits ("M08", "B01"). The
# pattern stays general so an id with an undeclared prefix ("J01") is still
# read as a citation, but a gate key ("P3", "P10_freeze") is not a record id.
_RECORD_ID_TOKEN = re.compile(r"(?<![A-Za-z0-9_])([A-Z])([0-9]{2})(?![0-9])")
_GATE_TOKENS = frozenset(gate.split("_")[0] for gate in ALLOWED_GATES)


class SpecItem(NamedTuple):
    """One required item from the spec/plan, and the case ids that must carry it."""

    label: str
    authority: str
    authority_file: Path
    cases: tuple[str, ...]


# Mapping of every required spec item -> carrying case id(s). The item order and
# wording follow the spec Contract and P4 T5: FIFO/exclusion -> M01; cross-thread
# overlap -> M02; stable intent incl. thread creation/duplicate/lost-response ->
# M03-M05; kill/restart and fresh-process readback -> M06-M07; PG-commit-before-
# Redis -> M08; Redis interruption -> M09; outbox replay -> M10; lease takeover ->
# M11; stale-worker fencing -> M12; stale checkpoint put -> M13; put_writes ->
# M14; parser CAS vs replacement -> M15; vs cancellation -> M16; vs deletion ->
# M17; late/duplicate/out-of-order -> M18-M20; cancellation vs late work -> M21;
# restart/reconciliation cut points -> M22; typed 404 vs retryable 5xx/timeout ->
# M23-M24; ledger/trace fault and redaction -> M25.
MECHANISM_ITEMS: tuple[SpecItem, ...] = (
    SpecItem("same-thread FIFO/exclusion", "same-thread FIFO/exclusion", SPEC_PATH, ("M01",)),
    SpecItem("cross-thread overlap", "cross-thread overlap", SPEC_PATH, ("M02",)),
    SpecItem(
        "stable intent incl. initial thread creation and duplicate/lost-response delivery",
        "stable operation identity including initial thread creation and "
        "duplicate/lost-response delivery",
        SPEC_PATH,
        ("M03", "M04", "M05"),
    ),
    SpecItem(
        "process kill/restart and fresh-process readback",
        "process kill/restart and fresh-process readback",
        SPEC_PATH,
        ("M06", "M07"),
    ),
    SpecItem(
        "PG commit before Redis publication",
        "PG commit before Redis publication",
        SPEC_PATH,
        ("M08",),
    ),
    SpecItem("Redis interruption", "Redis interruption", SPEC_PATH, ("M09",)),
    SpecItem("outbox replay", "outbox replay", SPEC_PATH, ("M10",)),
    SpecItem("lease takeover", "lease takeover", SPEC_PATH, ("M11",)),
    SpecItem("stale-worker fencing", "stale-worker fencing", SPEC_PATH, ("M12",)),
    SpecItem(
        "stale checkpoint put after fencing",
        "stale checkpoint writes through both `put`",
        SPEC_PATH,
        ("M13",),
    ),
    SpecItem(
        "stale checkpoint put_writes after fencing",
        "`put_writes` after fencing",
        SPEC_PATH,
        ("M14",),
    ),
    SpecItem(
        "parser adoption CAS vs source replacement",
        "compare-and-swap against source replacement",
        SPEC_PATH,
        ("M15",),
    ),
    SpecItem(
        "parser adoption CAS vs cancellation",
        "source replacement, cancellation, and deletion",
        SPEC_PATH,
        ("M16",),
    ),
    SpecItem(
        "parser adoption CAS vs deletion",
        "cancellation, and deletion",
        SPEC_PATH,
        ("M17",),
    ),
    SpecItem(
        "late/duplicate/out-of-order results",
        "late/duplicate/out-of-order results",
        SPEC_PATH,
        ("M18", "M19", "M20"),
    ),
    SpecItem(
        "cancellation against late work",
        "cancellation against a late worker",
        SPEC_PATH,
        ("M21",),
    ),
    SpecItem(
        "restart/reconciliation cut points",
        "restart and reconciliation at durable cut points",
        SPEC_PATH,
        ("M22",),
    ),
    SpecItem(
        "typed 404 versus retryable 5xx/timeout",
        "typed `404` versus retryable `5xx`/timeout behavior",
        SPEC_PATH,
        ("M23", "M24"),
    ),
    SpecItem(
        "ledger/trace fault and redaction",
        "ledger/trace fault and redaction observations",
        PLAN_PATH,
        ("M25",),
    ),
)

# Same mapping for the browser lane: six turns through home -> B01; concurrent
# threads with reverse completion -> B02; refresh/reconnect -> B03; replay/reset
# -> B04; duplicate activation + lost-response retry -> B05; targeted cancellation
# -> B06; malformed snapshot/stale GET -> B07; 404 vs 5xx -> B08; attachments
# beside a usable thread -> B09; per-tab independent state -> B10; TaskPanel trace
# read states -> B11.
BROWSER_ITEMS: tuple[SpecItem, ...] = (
    SpecItem(
        "three turns + home + three more",
        "at least three consecutive turns, home navigation/reopen, then at least "
        "three further turns with durable prior content",
        SPEC_PATH,
        ("B01",),
    ),
    SpecItem(
        "two concurrent threads with reverse completion",
        "two concurrent threads with out-of-order completion",
        SPEC_PATH,
        ("B02",),
    ),
    SpecItem(
        "refresh/reconnect during a run",
        "refresh/reconnect during a run",
        SPEC_PATH,
        ("B03",),
    ),
    SpecItem("replay/reset recovery", "replay and reset recovery", SPEC_PATH, ("B04",)),
    SpecItem(
        "duplicate activation + lost-response retry",
        "rapid duplicate activation and lost-response retry",
        SPEC_PATH,
        ("B05",),
    ),
    SpecItem("targeted cancellation", "targeted cancellation races", SPEC_PATH, ("B06",)),
    SpecItem(
        "attachments beside a usable thread",
        "attachment upload, preparation, readiness, and failure while another "
        "thread remains usable",
        SPEC_PATH,
        ("B09",),
    ),
    SpecItem(
        "malformed snapshot + stale GET",
        "malformed local snapshots and stale GET responses do not overwrite newer state",
        SPEC_PATH,
        ("B07",),
    ),
    SpecItem("404 vs 5xx/timeout", "transient `5xx` is not rendered", SPEC_PATH, ("B08",)),
    SpecItem(
        "per-tab independent state",
        "independent per-thread answer/loading/trace/attachment state in two tabs",
        PLAN_PATH,
        ("B10",),
    ),
    SpecItem(
        "TaskPanel trace read states",
        "Exercise the existing TaskPanel trace read states through the real browser",
        SPEC_PATH,
        ("B11",),
    ),
)

# Credential and machine-path shapes. Each rule names itself so a failure reports
# the file and JSON path only, never the matched text. ``sk-`` uses the same
# left-boundary guard as the P4 contract so ordinary hyphenated prose cannot be
# mistaken for a key.
_CREDENTIAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sk-key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]")),
    ("bearer", re.compile(r"bearer ", re.IGNORECASE)),
    ("postgres-dsn", re.compile(r"postgres://", re.IGNORECASE)),
    ("postgresql-dsn", re.compile(r"postgresql://", re.IGNORECASE)),
    ("redis-dsn", re.compile(r"redis://", re.IGNORECASE)),
    ("password-assignment", re.compile(r"password=", re.IGNORECASE)),
    ("non-http-scheme", re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*)://")),
    ("home-path", re.compile(r"/home/|/Users/")),
    ("windows-drive", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")),
)
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _load(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"missing frozen protocol artifact: {path}"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path.name} is not a JSON object"
    return payload


def _records(payload: dict[str, Any], lane: str) -> list[dict[str, Any]]:
    """The lane's case/journey records, with the non-empty floor every loop needs."""
    raw = payload[RECORD_KEYS[lane]]
    assert isinstance(raw, list) and raw, f"{lane}: no case records declared"
    records: list[dict[str, Any]] = []
    for item in raw:
        assert isinstance(item, dict), f"{lane}: record is not a JSON object"
        records.append(item)
    return records


def _frozen_ids(lane: str) -> set[str]:
    """The id set the freeze note declares, e.g. 'M01-M25' -> {'M01'..'M25'}."""
    text = FREEZE_PATH.read_text(encoding="utf-8")
    prefix = LANE_PREFIX[lane]
    ids: set[str] = set()
    for first, start, last, end in _FREEZE_RANGE.findall(text):
        if first != prefix:
            continue
        assert last == prefix, (first, last)
        ids |= {f"{prefix}{n:02d}" for n in range(int(start), int(end) + 1)}
    assert ids, f"the freeze note declares no {prefix}NN range for lane {lane}"
    return ids


def _ids(payload: dict[str, Any], lane: str) -> list[str]:
    return [record[ID_KEYS[lane]] for record in _records(payload, lane)]


def _cited_record_ids(description: str) -> list[str]:
    """The record ids a gate description cites, in the order they appear.

    Only a gate key is excluded, so an id with an undeclared prefix (e.g.
    ``J01``) is still read as a citation and reported.
    """
    return [
        match.group(0)
        for match in _RECORD_ID_TOKEN.finditer(description)
        if match.group(0) not in _GATE_TOKENS
    ]


def _string_leaves(value: Any, path: str) -> list[tuple[str, str]]:
    """Every string in the structure, keys included, with its JSON path."""
    leaves: list[tuple[str, str]] = []
    if isinstance(value, str):
        leaves.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            leaves.append((f"{path}<{key}>", key))
            leaves.extend(_string_leaves(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            leaves.extend(_string_leaves(item, f"{path}[{index}]"))
    return leaves


def _reporter_names(config: str) -> list[str]:
    """The reporter names declared in the config's ``reporter`` array.

    Read out of the config source, so the expectation follows the file rather
    than a copy of it.
    """
    declarations = re.findall(r"^\s*reporter\s*:", config, re.MULTILINE)
    assert len(declarations) == 1, (
        "playwright.config.mjs must declare exactly one reporter array: "
        f"{len(declarations)}"
    )
    open_bracket = config.index("[", config.index("reporter:"))
    depth = 0
    block = ""
    for index in range(open_bracket, len(config)):
        if config[index] == "[":
            depth += 1
        elif config[index] == "]":
            depth -= 1
            if depth == 0:
                block = config[open_bracket + 1 : index]
                break
    assert block, "playwright.config.mjs reporter array is not closed"
    return re.findall(r'\[\s*"([A-Za-z][A-Za-z0-9]*)"', block)


def test_identity_lane_and_status_claim_no_campaign_result() -> None:
    for lane, path in LANE_FILES:
        payload = _load(path)
        assert payload["schema_version"] == "1.0.0", (path.name, payload["schema_version"])
        # The lane must match the artifact's own filename, not just its content.
        assert payload["lane"] == path.stem.split("_")[0] == lane, (
            path.name,
            payload["lane"],
        )
        protocol_id = payload["protocol_id"]
        assert isinstance(protocol_id, str) and protocol_id.strip(), (
            f"{path.name}: no protocol id"
        )
        status = payload["status"]
        assert isinstance(status, str) and status.strip(), f"{path.name}: empty status"
        assert re.search(r"\bno\b.*\brun\b", status, re.IGNORECASE), (
            f"{path.name}: status does not record that no campaign ran: {status!r}"
        )
        # Independent basis: the freeze note states nothing is scored/passed.
        remaining = _NEGATED_CLAIM.sub(" ", status)
        assert not _CLAIM.search(remaining), (
            f"{path.name}: status claims a campaign result: {status!r}"
        )


def test_ids_are_exactly_the_frozen_ranges_without_duplicates() -> None:
    for lane, path in LANE_FILES:
        payload = _load(path)
        ids = _ids(payload, lane)
        assert len(ids) == len(set(ids)), f"{path.name}: duplicate ids"
        expected = _frozen_ids(lane)
        assert set(ids) == expected, (path.name, sorted(set(ids) ^ expected))


def test_every_required_spec_item_is_carried_by_a_case() -> None:
    """Each spec/plan item is carried by the named case(s); no case is unmapped."""
    texts = {
        SPEC_PATH: SPEC_PATH.read_text(encoding="utf-8"),
        PLAN_PATH: PLAN_PATH.read_text(encoding="utf-8"),
    }
    for lane, path, items in (
        ("mechanism", MECHANISM_PATH, MECHANISM_ITEMS),
        ("browser", BROWSER_PATH, BROWSER_ITEMS),
    ):
        payload = _load(path)
        ids = set(_ids(payload, lane))
        labels = [item.label for item in items]
        assert len(labels) == len(set(labels)), (lane, "duplicate item label")

        covered: set[str] = set()
        for item in items:
            assert item.authority in texts[item.authority_file], (
                f"{lane}: item authority missing from {item.authority_file.name}: "
                f"{item.label}"
            )
            missing = set(item.cases) - ids
            assert not missing, (lane, item.label, sorted(missing))
            covered |= set(item.cases)
        assert covered == ids, (lane, "cases carrying no required item", sorted(ids - covered))
        assert covered == _frozen_ids(lane), (lane, sorted(covered ^ _frozen_ids(lane)))


def test_every_case_is_executable_and_gates_stay_bounded() -> None:
    for lane, path in LANE_FILES:
        payload = _load(path)
        top_gates = payload["integration_gates"]
        assert isinstance(top_gates, dict) and top_gates, f"{path.name}: no top gates"
        unknown = set(top_gates) - ALLOWED_GATES
        assert not unknown, (path.name, "unknown top-level gates", sorted(unknown))
        for gate, description in top_gates.items():
            assert isinstance(description, str) and description.strip(), (path.name, gate)

        # The freeze note says no case declares a P10 rubric, so the top-level
        # P10_freeze entry must scope itself to a per-case opt-in, not a blanket
        # requirement, and no record may opt in while that statement stands.
        p10 = top_gates["P10_freeze"].lower()
        assert "none of the" in p10 and "declares one" in p10, (path.name, p10)
        assert ("cases" in p10) or ("journeys" in p10), (path.name, p10)

        for record in _records(payload, lane):
            case_id = record[ID_KEYS[lane]]
            category = record["primary_category"]
            assert isinstance(category, str) and category.strip(), (
                case_id,
                "empty primary_category",
            )
            race = record[RACE_FIELD[lane]]
            assert isinstance(race, str) and race.strip(), (
                case_id,
                f"empty {RACE_FIELD[lane]}",
            )

            observation_fields = (
                ("required_observations",)
                if lane == "mechanism"
                else ("visible_observations", "durable_observations")
            )
            for field in observation_fields:
                observations = record[field]
                assert isinstance(observations, list) and observations, (
                    case_id,
                    f"no {field}",
                )
                for observation in observations:
                    assert isinstance(observation, str) and observation.strip(), (
                        case_id,
                        field,
                    )

            gates = record["integration_gates"]
            assert isinstance(gates, list) and gates, (case_id, "no integration_gates")
            assert set(gates) <= ALLOWED_GATES, (case_id, sorted(set(gates) - ALLOWED_GATES))
            assert "P10_freeze" not in gates, (case_id, "unexpected P10_freeze gate")
            assert "rubric_id" not in record and "rubric_version" not in record, case_id


def test_gate_descriptions_cite_exactly_the_records_that_declare_them() -> None:
    """Each top-level gate description cites exactly the records declaring it.

    The per-record ``integration_gates`` field is the authority; a description
    that omits a declaring record, names a record that does not declare the
    gate, or names an id that is not a record at all is a defect.
    """
    for lane, path in LANE_FILES:
        payload = _load(path)
        top_gates = payload["integration_gates"]
        records = _records(payload, lane)
        id_key = ID_KEYS[lane]
        record_ids = set(_ids(payload, lane))
        for gate, description in top_gates.items():
            expected = sorted(
                record[id_key]
                for record in records
                if gate in record["integration_gates"]
            )
            cited = _cited_record_ids(description)
            unknown = sorted(set(cited) - record_ids)
            assert not unknown, (
                path.name,
                gate,
                "cites ids that are not record ids",
                unknown,
            )
            if not expected:
                assert not cited, (path.name, gate, "cites ids, none declared", cited)
                continue
            missing = sorted(set(expected) - set(cited))
            extra = sorted(set(cited) - set(expected))
            assert not missing and not extra, (
                path.name,
                gate,
                "missing",
                missing,
                "extra",
                extra,
            )
            # The cited ids must be exactly the declaring ids, ascending, once each.
            assert cited == expected, (path.name, gate, "cited", cited)


def test_denominator_and_attempt_rules_preserve_every_case() -> None:
    for lane, path in LANE_FILES:
        payload = _load(path)
        denominator = payload["denominator_rule"].lower()
        for token in ("flaky", "not_run", "blocked", "interrupted"):
            assert token in denominator, (path.name, f"denominator rule omits {token}")
        assert "attempt" in denominator, path.name
        attempt_rule = payload["attempt_rule"].lower()
        assert "attempt" in attempt_rule, path.name


def test_mechanism_declares_two_by_two_processes_real_services_and_lock_rule() -> None:
    payload = _load(MECHANISM_PATH)
    services = payload["required_services"]
    # spec: "at least two Runtime API processes and two graph workers"
    assert services["runtime_api_processes"] >= 2, services["runtime_api_processes"]
    assert services["graph_worker_processes"] >= 2, services["graph_worker_processes"]
    # spec: "Use barrier-controlled races ...; sleeps alone do not establish a race."
    fencing = services["fencing"].lower()
    assert "barrier" in fencing and "sleep" in fencing, services["fencing"]

    postgres = services["postgres"].lower()
    assert "postgresql" in postgres and "_test" in postgres, services["postgres"]
    redis = services["redis"].lower()
    assert "redis" in redis and "real" in redis, services["redis"]

    # spec: "PG locks never cover external fetch, validation, model/network wait,
    # or indexing."
    lock_rule = payload["lock_rule"].lower()
    for token in (
        "ownership",
        "never",
        "external fetch",
        "validation",
        "model/network",
        "indexing",
    ):
        assert token in lock_rule, f"lock rule omits {token}: {lock_rule!r}"


def test_browser_declares_real_application_api_and_rejects_surrogates() -> None:
    # spec: "A proxy replay or harness meta-test alone is not real-browser evidence."
    assert (
        "A proxy replay or harness meta-test alone is not real-browser evidence"
        in SPEC_PATH.read_text(encoding="utf-8")
    )
    foundation = _load(BROWSER_PATH)["foundation"]
    assert foundation["real_application_api"] is True, foundation["real_application_api"]
    assert foundation["proxy_replay_allowed"] is False, foundation["proxy_replay_allowed"]
    assert foundation["scripted_model_as_evidence"] is False, (
        foundation["scripted_model_as_evidence"]
    )


def test_browser_exit_contract_is_exact() -> None:
    contract = _load(BROWSER_PATH)["execution"]["exit_contract"]
    assert set(contract) == {"0", "1", "2"}, sorted(contract)
    every_and_passed = contract["0"].lower()
    assert "every" in every_and_passed and "passed" in every_and_passed, contract["0"]
    assert "failed" in contract["1"].lower(), contract["1"]
    assert "could not run" in contract["2"].lower(), contract["2"]


def test_browser_command_leaves_the_configured_json_reporter_in_place() -> None:
    """The recorded command must run with the config's own reporters.

    A Playwright CLI ``--reporter`` flag replaces the whole reporter array, so
    an override silently drops the JSON report that is the campaign evidence.
    """
    execution = _load(BROWSER_PATH)["execution"]
    command = execution["command"]
    assert "--reporter" not in command, (
        f"the recorded command overrides the config reporters: {command!r}"
    )
    names = _reporter_names(PLAYWRIGHT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert "json" in names, (PLAYWRIGHT_CONFIG_PATH.name, names)
    assert "list" in names, (PLAYWRIGHT_CONFIG_PATH.name, names)
    assert "reporter" in execution["reporters"].lower(), execution["reporters"]


def test_no_credentials_or_absolute_machine_paths() -> None:
    for path in (MECHANISM_PATH, BROWSER_PATH):
        payload = _load(path)
        violations: list[str] = []
        for json_path, text in _string_leaves(payload, path.name):
            for rule, pattern in _CREDENTIAL_RULES:
                match = pattern.search(text)
                if match is None:
                    continue
                if rule == "non-http-scheme" and match.group(1).lower() in _ALLOWED_SCHEMES:
                    continue
                # Report the location and rule only; never the matched value.
                violations.append(f"{json_path} [{rule}]")
        assert not violations, (
            f"{path.name}: {len(violations)} unsafe string(s): " + ", ".join(violations)
        )


def test_protocol_ids_are_distinct_and_pinnable() -> None:
    protocol_ids: dict[str, str] = {}
    for lane, path in LANE_FILES:
        protocol_id = _load(path)["protocol_id"]
        match = _VERSION_SHAPE.fullmatch(protocol_id)
        assert match is not None, (path.name, "protocol_id is not p4-<lane>-<version>")
        assert match.group(1) == lane, (path.name, protocol_id)
        protocol_ids[lane] = protocol_id
    assert len(set(protocol_ids.values())) == len(protocol_ids), protocol_ids


def test_case_ids_carry_the_lane_prefix() -> None:
    for lane, path in LANE_FILES:
        payload = _load(path)
        prefix = LANE_PREFIX[lane]
        wrong = [cid for cid in _ids(payload, lane) if not cid.startswith(prefix)]
        assert not wrong, (path.name, wrong)


def test_frozen_protocol_materializes_as_a_case_manifest() -> None:
    """A campaign pins this protocol as a CaseManifest today.

    The artifact is a protocol document that declares no manifest version of
    its own; a campaign pins the artifact's dotted ``schema_version`` as its
    manifest identity. The artifact declares no dataset, so no dataset version
    is invented here.
    """
    for lane, path in LANE_FILES:
        payload = _load(path)
        records = _records(payload, lane)
        specs = [
            case_spec(
                case_id=record[ID_KEYS[lane]],
                case_version=record[VERSION_KEYS[lane]],
                lane=lane,
                primary_category=record["primary_category"],
                fixture_version=payload["protocol_id"],
                provenance="frozen protocol artifact",
                rebuild_instructions=f"data/conformance/harness_runtime/{path.name}",
            )
            for record in records
        ]
        manifest = case_manifest(
            manifest_version=payload["schema_version"],
            lane=lane,
            dataset_manifest_version=payload["schema_version"],
            cases=specs,
            created_by="tests/conformance/test_harness_runtime_protocol_manifest.py",
        )
        assert validate_case_manifest(manifest) == [], lane
        assert [spec.case_id for spec in manifest.cases] == _ids(payload, lane)
        assert len(manifest.cases) == len(_frozen_ids(lane))
