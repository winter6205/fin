"""`fin` launcher: the readiness verdict and its exit-code contract.

The launcher (`scripts/fin`, `scripts/dev-up.sh`) answers one question —
"is serve up, and is the knowledge MCP reachable?" — and both the boot
reporter and `fin status` must answer it identically. The classification
lives in `scripts/lib/readiness.sh`; these tests pin the contract that
file implements against the product's own gate semantics
(`fin.host.knowledge_mcp`).

Why an HTTP stub rather than the real server: the verdict is a pure
function of the `GET /api/status` body. Booting a real `fin-serve` here
would drag Docker, Postgres and an iknow checkout into the hermetic
suite, and the classification would still be the same three lines. The
real boot path is exercised by the live smoke lanes, not by pytest.

Exit codes under test (documented in `scripts/fin`):
    0  ready      — serve answers AND the knowledge MCP is reachable
    1  usage      — not a fin checkout, bad option
    2  not ready  — serve is not answering
    3  gated      — serve answers but knowledge_mcp is 缺 / 坏
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

from fin.host.knowledge_mcp import (
    knowledge_mcp_gate_closed,
    publish_knowledge_mcp_state,
    reset_knowledge_mcp_state,
    resolve_bound_retrieve_adapter_kind,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIN = REPO_ROOT / "scripts" / "fin"
READINESS = REPO_ROOT / "scripts" / "lib" / "readiness.sh"
FIN_ROOT = REPO_ROOT / "scripts" / "lib" / "fin-root.sh"

EXIT_READY = 0
EXIT_USAGE = 1
EXIT_NOT_READY = 2
EXIT_GATED = 3

# A deliberate minimal PATH: the launcher needs bash, curl, jq and coreutils
# but must not inherit a stray PATH that masks those. `fin check` also needs
# docker / node / uv, which live outside /usr/bin on this host, so those
# tests pass the ambient PATH instead.
_BASE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "FIN_IKNOW_ROOT": str(Path.home() / "projects" / "iknow-memory"),
}


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    """Answer ``GET /api/status`` with one fixed body.

    ``raw_body`` overrides the JSON payload, which is how the
    "something else holds the port" case is simulated: an HTTP 200 whose
    body is not the pinned status JSON at all.
    """

    payload: dict[str, object] = {}
    raw_body: bytes | None = None
    content_type: str = "application/json"

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        if self.path != "/api/status":
            self.send_response(404)
            self.end_headers()
            return
        body = (
            self.raw_body
            if self.raw_body is not None
            else json.dumps(self.payload).encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Silence the per-request log line."""


class StatusStub:
    """A stub /api/status on an ephemeral port."""

    def __init__(
        self,
        state: str,
        adapter: str,
        *,
        raw_body: bytes | None = None,
        content_type: str = "application/json",
    ) -> None:
        payload: dict[str, object] = {
            "knowledge_mcp": state,
            "retrieve_adapter_kind": adapter,
            "model_identity": "stub-model",
            "corpus_filing_count": 12,
            "bound_collection_name": "stub-collection",
        }
        handler = type(
            "_H",
            (_StatusHandler,),
            {
                "payload": payload,
                "raw_body": raw_body,
                "content_type": content_type,
            },
        )
        self._server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def run_fin_status(port: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `fin status` against ``port``."""
    return subprocess.run(
        ["bash", str(FIN), "status"],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env={**_BASE_ENV, "WORKBENCH_SERVE_PORT": str(port)},
        timeout=30,
        check=False,
    )


# ----- deriving the expected verdict from the PRODUCT gate -----
#
# A hand-kept `(state, adapter) -> exit code` table is exactly the drift
# this suite exists to catch: it copies the shell classifier's assumption
# instead of checking it. The expectations below are computed from the
# product's own predicate (`fin.host.knowledge_mcp`) so that a change to
# the gate semantics fails HERE rather than shipping a launcher that
# disagrees with the server it reports on.

# The boot shapes the launcher must classify correctly. Each is
# (label, gate environ, published payload).
#
# The payload's `retrieve_adapter_kind` is what the route PUBLISHES for
# that boot — the BOUND port (`resolve_bound_retrieve_adapter_kind`),
# never the raw `FIN_RETRIEVE_ADAPTER` string.
_BOOT_SHAPES: list[tuple[str, dict[str, str], dict[str, object]]] = [
    (
        "product mcp boot, reachable",
        {"FIN_RETRIEVE_ADAPTER": "mcp"},
        {"knowledge_mcp": "reachable", "retrieve_adapter_kind": "mcp"},
    ),
    (
        "product mcp boot, 坏 (fault)",
        {"FIN_RETRIEVE_ADAPTER": "mcp"},
        {"knowledge_mcp": "fault", "retrieve_adapter_kind": "mcp"},
    ),
    (
        "product mcp boot, 缺 (missing)",
        {"FIN_RETRIEVE_ADAPTER": "mcp"},
        {"knowledge_mcp": "missing", "retrieve_adapter_kind": "mcp"},
    ),
    (
        "explicit fake boot, 缺",
        {"FIN_RETRIEVE_ADAPTER": "fake"},
        {"knowledge_mcp": "missing", "retrieve_adapter_kind": "fake"},
    ),
    (
        # The divergent pair, and the reason the expectations are derived
        # rather than tabulated: the scripted lane forces the FAKE port
        # while `FIN_RETRIEVE_ADAPTER` still names the product adapter, so
        # the configured string says "gated" and the bound port says the
        # opposite. A table written from the config string would pin the
        # false failure (a GATED report on a process that answers
        # create / open / send) and pass.
        "scripted boot with the product adapter configured",
        {"FIN_SCRIPTED_MODEL": "1", "FIN_RETRIEVE_ADAPTER": "mcp"},
        {"knowledge_mcp": "missing", "retrieve_adapter_kind": "fake"},
    ),
    (
        # A product-adapter boot that never probed: the snapshot is None,
        # so the gate is OPEN (see the module docstring) while the route
        # reports 缺. Classifying 缺 as gated without consulting the
        # adapter would call this not-ready.
        "product adapter, never probed (snapshot None)",
        {"FIN_RETRIEVE_ADAPTER": "mcp"},
        {"knowledge_mcp": "missing", "retrieve_adapter_kind": "mcp"},
    ),
]


def _expected_exit_code(
    environ: dict[str, str], published: dict[str, object]
) -> int:
    """The launcher's verdict, computed from the PRODUCT gate.

    Gated (3) iff the product would refuse conversation entry for this
    boot; ready (0) otherwise, because the route answered 200. "Nothing
    answers" (2) is a separate test — it is not a payload state.
    """
    state = str(published["knowledge_mcp"])
    publish_knowledge_mcp_state(state)
    try:
        gate_closed = knowledge_mcp_gate_closed(environ=environ)
    finally:
        reset_knowledge_mcp_state()
    return EXIT_GATED if gate_closed else EXIT_READY


@pytest.mark.parametrize(
    ("label", "environ", "published"),
    _BOOT_SHAPES,
    ids=[shape[0] for shape in _BOOT_SHAPES],
)
def test_status_exit_code_matches_the_product_gate(
    label: str, environ: dict[str, str], published: dict[str, object]
) -> None:
    expected = _expected_exit_code(environ, published)

    stub = StatusStub(
        str(published["knowledge_mcp"]), str(published["retrieve_adapter_kind"])
    )
    try:
        result = run_fin_status(stub.port)
    finally:
        stub.close()
    assert result.returncode == expected, (
        f"{label}: the product gate says exit {expected}; "
        f"the launcher returned {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_scripted_boot_is_ready_not_gated() -> None:
    """The divergent pair, asserted by name rather than by derivation.

    Pins the direction of the fix: a scripted boot on the product adapter
    has an OPEN gate, so reporting 3 ("create / open / send will 409")
    would be a false failure on the e2e lane.
    """
    environ = {"FIN_SCRIPTED_MODEL": "1", "FIN_RETRIEVE_ADAPTER": "mcp"}
    publish_knowledge_mcp_state("missing")
    try:
        assert not knowledge_mcp_gate_closed(environ=environ), (
            "the product gate must be OPEN for a scripted boot"
        )
        assert resolve_bound_retrieve_adapter_kind(environ=environ) == "fake", (
            "a scripted boot binds the fake port whatever "
            "FIN_RETRIEVE_ADAPTER names, and that is what the status "
            "route must publish"
        )
    finally:
        reset_knowledge_mcp_state()

    stub = StatusStub("missing", "fake")
    try:
        result = run_fin_status(stub.port)
    finally:
        stub.close()
    assert result.returncode == EXIT_READY, (
        "a scripted boot with FIN_RETRIEVE_ADAPTER=mcp must report ready "
        f"(gate open); got {result.returncode}\nstderr: {result.stderr}"
    )
    assert "GATED" not in result.stderr.upper()


def test_non_json_200_body_is_not_ready_and_names_the_port() -> None:
    """An HTTP 200 that is not the status payload must FAIL CLOSED.

    Whatever else holds the port can answer 200 with an HTML error page.
    Pre-fix, every `jq` in the classifier then failed: under `set -e` the
    verdict was a bare `jq: parse error` with an exit code outside the
    documented 0/1/2/3 table, and in the launcher's `|| body=""` shape the
    substitutions collapsed to empty and the verdict came out `ready` for
    a server that is not fin-serve at all. Either way the operator was
    told something untrue, so the body is pinned against both failure
    modes: the exit code, and a message that names the port.
    """
    html = b"<html><head><title>404 Not Found</title></head></html>"
    stub = StatusStub(
        "reachable", "mcp", raw_body=html, content_type="text/html"
    )
    try:
        result = run_fin_status(stub.port)
    finally:
        stub.close()

    assert result.returncode == EXIT_NOT_READY, (
        "a 200 that is not the status JSON must classify as not-ready (2); "
        f"got {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert str(stub.port) in combined, (
        f"the message must name port {stub.port} so the operator can find "
        f"the process holding it; got:\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "ready" not in result.stdout.lower() or "not ready" in combined, (
        "a non-status body must never be reported as ready"
    )


def test_non_json_200_body_never_prints_a_bare_jq_error() -> None:
    """The failure must be a contract verdict, not a leaked `jq` error.

    `jq: parse error` on stderr is the observable that pre-fix escaped
    the documented exit-code table; it must not come back.
    """
    stub = StatusStub(
        "reachable", "mcp", raw_body=b"not json at all", content_type="text/plain"
    )
    try:
        result = run_fin_status(stub.port)
    finally:
        stub.close()

    assert result.returncode in (EXIT_READY, EXIT_USAGE, EXIT_NOT_READY, EXIT_GATED), (
        f"exit {result.returncode} is outside the documented table\n"
        f"stderr: {result.stderr}"
    )
    assert not re.search(r"jq:.*parse error", result.stderr), (
        f"a bare jq parse error leaked to the operator: {result.stderr!r}"
    )


def test_gate_derivation_is_not_vacuous() -> None:
    """Guard the guard: the derived expectation must distinguish states.

    If `knowledge_mcp_gate_closed` ever stopped depending on the
    (adapter, state) pair — or the fixture stopped threading that pair
    into it — every derived expectation would collapse to one value and
    the parametrized test above would still pass. Pin that the two
    answers differ for two boots this launcher must treat differently.
    """
    gated = _expected_exit_code(
        {"FIN_RETRIEVE_ADAPTER": "mcp"},
        {"knowledge_mcp": "missing", "retrieve_adapter_kind": "mcp"},
    )
    open_gate = _expected_exit_code(
        {"FIN_RETRIEVE_ADAPTER": "fake"},
        {"knowledge_mcp": "missing", "retrieve_adapter_kind": "fake"},
    )
    assert gated == EXIT_GATED
    assert open_gate == EXIT_READY
    assert gated != open_gate


def test_status_reports_not_ready_when_nothing_answers() -> None:
    """A closed port is 2 (not ready), distinct from 3 (gated)."""
    # Bind then release a port so nothing is listening on it.
    probe = StatusStub("reachable", "mcp")
    free_port = probe.port
    probe.close()

    result = run_fin_status(free_port)
    assert result.returncode == EXIT_NOT_READY, result.stderr
    assert "not ready" in result.stderr.lower()


def test_unknown_option_is_a_usage_error() -> None:
    result = subprocess.run(
        ["bash", str(FIN), "--bogus"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == EXIT_USAGE
    assert "unknown option" in result.stderr


def test_help_succeeds_and_documents_the_exit_codes() -> None:
    result = subprocess.run(
        ["bash", str(FIN), "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == EXIT_READY
    assert "fin up" in result.stdout
    # The exit-code contract is part of the help text an operator reads.
    assert "gated" in result.stdout


def test_status_rejects_a_directory_that_is_not_a_fin_checkout(
    tmp_path: Path,
) -> None:
    """A path with no checkout above it is a usage error, not a crash."""
    result = subprocess.run(
        ["bash", str(FIN), "status", str(tmp_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == EXIT_USAGE
    assert "no fin checkout" in result.stderr


def test_readiness_module_is_the_single_source_of_classification() -> None:
    """Both callers must source the shared module rather than re-derive it.

    Duplicating the three-state classification is exactly how the boot
    reporter and `fin status` would drift apart, so pin the sharing.
    """
    assert READINESS.is_file(), "scripts/lib/readiness.sh must exist"
    for script in (FIN, REPO_ROOT / "scripts" / "dev-up.sh"):
        text = script.read_text(encoding="utf-8")
        assert "scripts/lib/readiness.sh" in text, (
            f"{script.name} must source the shared readiness module"
        )


# ----- up / down resolve the SAME iknow root -----
#
# `dev-down.sh` used to carry the pre-worktree copy of `resolve_iknow_root`,
# so from a linked worktree it computed `.iknow/worktrees/iknow-memory`
# (which does not exist), printed "not found — skipping", and exited 0 —
# reporting success while leaving the infra `dev-up.sh` had started still
# running. The README's own remedy for a cold boot (`fin down && fin up`)
# was inoperative from a worktree for exactly that reason. One resolver,
# two callers, driven here through the SHARED module.

_MAIN_CHECKOUT = Path(__file__).resolve().parents[2]


def _git(*args: str, cwd: Path) -> str:
    """Run git for the fixture. Real git, not a hand-written layout.

    `main_checkout_root` asks git itself (`rev-parse --git-common-dir`),
    and a hand-written `.git` / `commondir` pair is NOT accepted by git —
    it answers `fatal: not a git repository`. A fixture that fakes the
    layout would therefore exercise the "not a repo" branch and silently
    test nothing, so the fixture creates a real repository.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(cwd),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout.strip()


class _PathStub:
    """A main checkout with a LINKED WORKTREE and an optional iknow checkout.

    ``is_iknow_checkout`` requires BOTH ``package.json`` and
    ``bin/iknow-memory.js``, so the fixture builds real files — a
    directory that merely shares the name must not resolve.
    """

    def __init__(self, root: Path, *, with_iknow: bool) -> None:
        self.root = root
        self.main = root / "main"
        # The real layout: worktrees live under the main checkout's
        # `.iknow/worktrees/`, which is what makes the worktree's own
        # sibling an `iknow-memory` directory that does not exist.
        self.worktree = self.main / ".iknow" / "worktrees" / "wt"

        self.main.mkdir(parents=True, exist_ok=True)
        _git("init", "-q", "-b", "main", cwd=self.main)
        _git(
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "user.name=fixture",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
            cwd=self.main,
        )
        (self.main / ".iknow" / "worktrees").mkdir(parents=True, exist_ok=True)
        _git("worktree", "add", "-q", "--detach", str(self.worktree), cwd=self.main)

        for path in (self.main, self.worktree):
            (path / "scripts" / "lib").mkdir(parents=True, exist_ok=True)
            (path / "src" / "fin" / "host").mkdir(parents=True, exist_ok=True)
            (path / "scripts" / "dev-up.sh").write_text("", encoding="utf-8")
            (path / "src" / "fin" / "host" / "http.py").write_text(
                "", encoding="utf-8"
            )
            # Only the resolver module is copied: this fixture drives the
            # shared resolver, never the lifecycle scripts (which would
            # want Docker).
            (path / "scripts" / "lib" / "fin-root.sh").write_text(
                (REPO_ROOT / "scripts" / "lib" / "fin-root.sh").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )

        self.sibling_iknow = self.main.parent / "iknow-memory"
        if with_iknow:
            self.write_iknow_checkout(self.sibling_iknow)

    @staticmethod
    def write_iknow_checkout(path: Path) -> None:
        """Both markers, so `is_iknow_checkout` accepts it."""
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text("{}", encoding="utf-8")
        (path / "bin" / "iknow-memory.js").write_text("", encoding="utf-8")


def _resolve_iknow_root(repo_root: Path, cwd: Path) -> str:
    """Run the SHARED resolver the way dev-up.sh / dev-down.sh do.

    Same contract as both callers: `REPO_ROOT` set to the checkout the
    call is for, `source scripts/lib/fin-root.sh`, then call the
    function. The resolver must not depend on $PWD, which is why ``cwd``
    is a parameter.
    """
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$REPO_ROOT/scripts/lib/fin-root.sh" && resolve_iknow_root',
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "REPO_ROOT": str(repo_root)},
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_dev_up_and_dev_down_resolve_the_same_iknow_root(
    tmp_path: Path,
) -> None:
    """From a worktree, both callers must land on the same checkout.

    The pre-fix `dev-down.sh` computed `.iknow/worktrees/iknow-memory`
    (the sibling of a worktree is the worktrees directory) and skipped
    the infra silently. Driving BOTH scripts through the shared resolver
    is what makes that class of drift fail here.
    """
    stub = _PathStub(tmp_path, with_iknow=True)

    from_worktree = _resolve_iknow_root(stub.worktree, cwd=stub.worktree)
    from_main = _resolve_iknow_root(stub.main, cwd=stub.main)

    assert from_worktree == from_main, (
        "dev-up.sh and dev-down.sh must resolve the SAME iknow-memory "
        f"checkout; worktree gave {from_worktree!r}, main gave {from_main!r}"
    )
    assert Path(from_worktree) == stub.sibling_iknow, (
        f"expected the main checkout's sibling {stub.sibling_iknow}, "
        f"got {from_worktree}"
    )
    # The pre-fix answer, named so the regression cannot come back as a
    # plausible-looking path.
    assert ".iknow/worktrees/iknow-memory" not in from_worktree


def test_iknow_resolver_is_cwd_independent(tmp_path: Path) -> None:
    """The verdict is a property of REPO_ROOT, not of where bash stands."""
    stub = _PathStub(tmp_path, with_iknow=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    assert _resolve_iknow_root(stub.worktree, cwd=elsewhere) == _resolve_iknow_root(
        stub.worktree, cwd=stub.worktree
    )


def test_iknow_resolver_prefers_the_main_checkout_from_a_worktree(
    tmp_path: Path,
) -> None:
    """A worktree's sibling iknow checkout also wins (candidate order).

    `FIN_IKNOW_ROOT` is unset here, so the main-checkout fallback is what
    supplies the answer — the branch a worktree depends on.
    """
    stub = _PathStub(tmp_path, with_iknow=True)
    # Give the worktree its own sibling `iknow-memory` too; it must win,
    # because it is the nearer candidate and the resolver's documented
    # order puts it first.
    worktree_sibling = stub.worktree.parent / "iknow-memory"
    stub.write_iknow_checkout(worktree_sibling)

    assert _resolve_iknow_root(stub.worktree, cwd=stub.worktree) == str(
        worktree_sibling
    )


def test_iknow_resolver_reports_a_named_path_when_no_candidate_matches(
    tmp_path: Path,
) -> None:
    """It never fails: the caller must get a path it can name in an error."""
    stub = _PathStub(tmp_path, with_iknow=False)

    resolved = _resolve_iknow_root(stub.worktree, cwd=stub.worktree)

    assert resolved, "the resolver must always print a candidate"
    assert resolved.endswith("iknow-memory")


def test_lifecycle_scripts_share_one_iknow_resolver() -> None:
    """Neither lifecycle script may carry its own copy of the resolver.

    The drift that this module exists to prevent was a private copy in
    `dev-down.sh`; pin that both callers source the shared module and
    that no second definition creeps back in.
    """
    assert FIN_ROOT.is_file(), "scripts/lib/fin-root.sh must exist"
    for name in ("dev-up.sh", "dev-down.sh"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "scripts/lib/fin-root.sh" in text, (
            f"{name} must source the shared fin-root module"
        )
        assert "resolve_iknow_root()" not in text, (
            f"{name} defines its own resolve_iknow_root; the shared "
            "resolver in scripts/lib/fin-root.sh is the single source"
        )


# The four attachment bounds `serve()` requires before it binds a port.
BOUND_KEYS = (
    "FIN_ATTACHMENT_MAX_BYTES",
    "FIN_ATTACHMENT_MAX_PDF_PAGES",
    "FIN_ATTACHMENT_VISION_CALLS_PER_RUN",
    "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN",
)
ATTACHMENT_BOUNDS_BASH = REPO_ROOT / "scripts" / "lib" / "attachment-bounds.sh"
ATTACHMENT_BOUNDS_NODE = REPO_ROOT / "scripts" / "lib" / "attachment-bounds.mjs"
BOUNDS_LANES = (
    "scripts/dev-up.sh",
    "scripts/cursor-start.sh",
    "scripts/run-scripted-e2e.mjs",
)


def _bound_literals(text: str) -> dict[str, list[str]]:
    """Every `KEY=<digits>` assignment in `text`, quoted or not."""
    found: dict[str, list[str]] = {}
    for key in BOUND_KEYS:
        values = re.findall(rf"{key}\s*=\s*[\"']?(\d+)", text)
        if values:
            found[key] = values
    return found


def test_no_lane_repeats_an_attachment_bound_literal() -> None:
    """Each boot lane must resolve the bounds, not re-type them.

    The host accepts any positive integer, so a lane carrying a stale cap
    boots happily and then measures the wrong thing. Four copies agreeing
    by hand is how the cap drifted from the `.env.example` value this
    round started; the template is the only place a number may appear.
    """
    offenders = {
        name: _bound_literals((REPO_ROOT / name).read_text(encoding="utf-8"))
        for name in BOUNDS_LANES
    }

    assert {name: found for name, found in offenders.items() if found} == {}, (
        "attachment bound values belong in .env.example, read through "
        "scripts/lib/attachment-bounds.{sh,mjs}"
    )
    for name in BOUNDS_LANES[:2]:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "scripts/lib/attachment-bounds.sh" in text, (
            f"{name} must source the shared bounds resolver"
        )
    assert "attachment-bounds.mjs" in (REPO_ROOT / BOUNDS_LANES[2]).read_text(
        encoding="utf-8"
    ), f"{BOUNDS_LANES[2]} must import the shared bounds reader"


def test_attachment_bound_resolver_reads_the_template_and_lets_env_win(
    tmp_path: Path,
) -> None:
    """The bash resolver: unset keys come from `.env.example`, a value
    already in the environment is never overridden, and the node reader
    agrees with it key for key.
    """
    template = tmp_path / ".env.example"
    template.write_text(
        "# comment line, ignored\n"
        f"{BOUND_KEYS[0]}=111\n"
        f"export {BOUND_KEYS[1]}='222'\n"
        f"{BOUND_KEYS[2]}=333\n"
        f"{BOUND_KEYS[3]}=444\n",
        encoding="utf-8",
    )

    resolved = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/scripts/lib/attachment-bounds.sh" && '
            "export REPO_ROOT=$2 && export_attachment_bounds probe && "
            "for k in ${ATTACHMENT_BOUND_KEYS[@]}; do "
            'printf "%s=%s\\n" "$k" "${!k}"; done',
            "-",
            str(REPO_ROOT),
            str(tmp_path),
        ],
        env={**_BASE_ENV, BOUND_KEYS[0]: "999"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
    # The first key was already exported, so it wins over the template; the
    # rest come through, with `export` and quotes stripped.
    assert resolved.stdout.splitlines() == [
        f"{BOUND_KEYS[0]}=999",
        f"{BOUND_KEYS[1]}=222",
        f"{BOUND_KEYS[2]}=333",
        f"{BOUND_KEYS[3]}=444",
    ]

    node = subprocess.run(
        [
            "node",
            "-e",
            "import('file://' + process.argv[1]).then((m) => "
            "console.log(JSON.stringify(m.attachmentBoundsFromExample("
            "process.argv[2]))));",
            str(ATTACHMENT_BOUNDS_NODE),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert node.returncode == 0, node.stderr
    assert json.loads(node.stdout) == {
        BOUND_KEYS[0]: "111",
        BOUND_KEYS[1]: "222",
        BOUND_KEYS[2]: "333",
        BOUND_KEYS[3]: "444",
    }


def test_attachment_bound_resolver_fails_loudly_on_a_missing_key(
    tmp_path: Path,
) -> None:
    """A bound neither set nor in the template must stop the boot, by name.

    Falling back to a number here is the drift this module removes, and a
    lane that invented one would report a measurement it never made.
    """
    hollow = tmp_path / "checkout"
    hollow.mkdir()
    (hollow / ".env.example").write_text(
        f"{BOUND_KEYS[0]}=1\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/scripts/lib/attachment-bounds.sh" && '
            "export REPO_ROOT=$2 && export_attachment_bounds probe && "
            "echo UNREACHED",
            "-",
            str(REPO_ROOT),
            str(hollow),
        ],
        env=_BASE_ENV,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "UNREACHED" not in result.stdout
    assert "probe" in result.stderr
    for key in BOUND_KEYS[1:]:
        assert key in result.stderr, f"{key} must be named in the failure"


# ----- the PATH shim -----

INSTALLER = REPO_ROOT / "scripts" / "install-fin-shim.sh"


def _install(bin_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALLER)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={**_BASE_ENV, "BIN_DIR": str(bin_dir), "HOME": str(bin_dir)},
        timeout=60,
        check=False,
    )


def test_shim_install_is_idempotent_and_uninstallable(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "fin"

    first = _install(bin_dir)
    assert first.returncode == 0, first.stderr
    assert shim.is_file()
    body_after_first = shim.read_text(encoding="utf-8")

    second = _install(bin_dir)
    assert second.returncode == 0, second.stderr
    assert shim.read_text(encoding="utf-8") == body_after_first, (
        "re-installing must rewrite the same shim, not stack a second one"
    )

    removed = subprocess.run(
        ["bash", str(INSTALLER), "--uninstall"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env={**_BASE_ENV, "BIN_DIR": str(bin_dir), "HOME": str(bin_dir)},
        timeout=60,
        check=False,
    )
    assert removed.returncode == 0, removed.stderr
    assert not shim.exists()


def test_shim_refuses_to_clobber_a_foreign_fin(tmp_path: Path) -> None:
    """A pre-existing `fin` that is not ours must not be overwritten."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    foreign = bin_dir / "fin"
    foreign.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")

    result = _install(bin_dir)
    assert result.returncode != 0
    assert "not this installer's shim" in result.stderr
    assert "foreign" in foreign.read_text(encoding="utf-8")


def test_shim_drives_a_checkout_from_any_directory(tmp_path: Path) -> None:
    """The whole point of the shim: bare `fin` works outside the repo.

    Run from a directory with no checkout above it, the shim must fall
    back to the installed checkout, say so, and still reach `fin check`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert _install(bin_dir).returncode == 0

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # `fin check` preflights docker / node / uv, which are not in the
    # minimal _BASE_ENV PATH, so inherit the ambient one here.
    result = subprocess.run(
        ["bash", str(bin_dir / "fin"), "check"],
        cwd=str(elsewhere),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "not inside a fin checkout" in result.stderr
    assert "check ok" in result.stdout


def test_shim_announces_the_fallback_checkout(tmp_path: Path) -> None:
    """The fallback must be ANNOUNCED with the path it chose.

    A bare `fin` from an unrelated directory acts on a tree the operator
    did not name; saying which one is the difference between a helpful
    default and a surprise. The announcement lives in `scripts/fin`
    (the shim no longer walks or cds), so this also pins that the shim's
    install-time checkout actually reaches the launcher.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert _install(bin_dir).returncode == 0

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    stub = StatusStub("reachable", "mcp")
    try:
        result = subprocess.run(
            ["bash", str(bin_dir / "fin"), "status"],
            cwd=str(elsewhere),
            capture_output=True,
            text=True,
            env={**_BASE_ENV, "WORKBENCH_SERVE_PORT": str(stub.port)},
            timeout=60,
            check=False,
        )
    finally:
        stub.close()

    assert result.returncode == EXIT_READY, (
        f"the shim must still reach `fin status`; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "not inside a fin checkout" in result.stderr
    assert str(REPO_ROOT) in result.stderr, (
        "the announcement must name the checkout it fell back to; got "
        f"{result.stderr!r}"
    )


def test_shim_prefers_the_nearer_checkout_over_its_install_time_one(
    tmp_path: Path,
) -> None:
    """A checkout at or above $PWD wins, and no fallback is announced.

    This is what makes `fin` in a worktree drive that worktree. The
    walk-up is `scripts/fin`'s single implementation; the shim only
    supplies the fallback, so a nearest-match checkout must be chosen
    silently.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert _install(bin_dir).returncode == 0

    nested = REPO_ROOT / "scripts" / "lib"
    stub = StatusStub("reachable", "mcp")
    try:
        result = subprocess.run(
            ["bash", str(bin_dir / "fin"), "status"],
            cwd=str(nested),
            capture_output=True,
            text=True,
            env={**_BASE_ENV, "WORKBENCH_SERVE_PORT": str(stub.port)},
            timeout=60,
            check=False,
        )
    finally:
        stub.close()

    assert result.returncode == EXIT_READY, result.stderr
    assert "not inside a fin checkout" not in result.stderr, (
        "a directory INSIDE a checkout must not trigger the fallback "
        f"announcement; got {result.stderr!r}"
    )


def test_shim_forwards_the_command_word(tmp_path: Path) -> None:
    """`--checkout-for-shim` must not swallow the command that follows it.

    Regression: the shim calls `fin --checkout-for-shim <path> <command>`,
    and the parser used to shift only the first argv element. `status` /
    `--help` then landed in the optional-checkout-path slot and died as
    `not a directory: status` — the shim could not run a single command.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert _install(bin_dir).returncode == 0

    helped = subprocess.run(
        ["bash", str(bin_dir / "fin"), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=dict(_BASE_ENV),
        timeout=60,
        check=False,
    )
    assert helped.returncode == EXIT_READY, helped.stderr
    assert "fin status" in helped.stdout, (
        f"the help text must survive the shim; got {helped.stdout!r}"
    )

    bogus = subprocess.run(
        ["bash", str(bin_dir / "fin"), "--bogus"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=dict(_BASE_ENV),
        timeout=60,
        check=False,
    )
    assert bogus.returncode == EXIT_USAGE
    assert "unknown option" in bogus.stderr


def test_shim_does_not_carry_the_checkout_walk_up(tmp_path: Path) -> None:
    """The shim must delegate, not re-implement discovery.

    Its hand-maintained copy is what diverged (its markers were
    `scripts/fin` + `src/fin/host/http.py` while the launcher's were
    `scripts/dev-up.sh` + `src/fin/host/http.py`), so `fin` run through
    the shim and `fin` run directly could pick different checkouts from
    the same cwd. Pin that the generated file has exactly one hand-off
    and no second discovery implementation.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert _install(bin_dir).returncode == 0
    body = (bin_dir / "fin").read_text(encoding="utf-8")

    assert "find_checkout" not in body, (
        "the shim must not walk the tree itself; scripts/fin owns the "
        "single walk-up"
    )
    assert "cd " not in body, (
        "the shim must not cd; scripts/fin resolves the checkout from the "
        "target directory it is given"
    )
    execs = [line for line in body.splitlines() if line.startswith("exec ")]
    assert len(execs) == 1, f"expected one hand-off, got {execs!r}"
    assert "--checkout-for-shim" in execs[0]
    # The hand-off must name the install-time checkout, and the shim must
    # carry it as a variable rather than a hardcoded path in the exec.
    assignment = [
        line for line in body.splitlines() if line.startswith("DEFAULT_CHECKOUT=")
    ]
    assert len(assignment) == 1, f"expected one DEFAULT_CHECKOUT, got {assignment!r}"
    assert str(REPO_ROOT) in assignment[0], assignment[0]
    assert '$DEFAULT_CHECKOUT' in execs[0], execs[0]


def test_installer_requires_both_checkout_markers(tmp_path: Path) -> None:
    """A tree missing either marker must not get a shim installed.

    `scripts/fin` refuses such a directory as a checkout, so installing a
    shim that points at one would produce a PATH entry that can never
    work. The installer validates at install time instead.
    """
    fake = tmp_path / "fake"
    (fake / "scripts").mkdir(parents=True)
    (fake / "scripts" / "dev-up.sh").write_text("", encoding="utf-8")
    (fake / "scripts" / "fin").write_text("", encoding="utf-8")
    (fake / "scripts" / "install-fin-shim.sh").write_text(
        INSTALLER.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # src/fin/host/http.py deliberately absent.

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = subprocess.run(
        ["bash", str(fake / "scripts" / "install-fin-shim.sh")],
        cwd=str(fake),
        capture_output=True,
        text=True,
        env={**_BASE_ENV, "BIN_DIR": str(bin_dir), "HOME": str(tmp_path)},
        timeout=60,
        check=False,
    )

    assert result.returncode != 0, (
        "installing against a tree without both markers must fail; got "
        f"exit 0\nstdout: {result.stdout}"
    )
    assert "src/fin/host/http.py" in result.stderr
    assert not (bin_dir / "fin").exists()


def test_shim_reinstall_does_not_stack_a_second_copy(tmp_path: Path) -> None:
    """Re-running the installer must leave ONE shim, byte-identical."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "fin"

    for _ in range(3):
        assert _install(bin_dir).returncode == 0
        body = shim.read_text(encoding="utf-8")
        assert body.count("fin entrypoint (scripts/install-fin-shim.sh)") == 1, (
            "the marker must appear exactly once — a second copy would "
            "mean the installer appended instead of rewriting"
        )
        assert body.count("--checkout-for-shim") == 1
