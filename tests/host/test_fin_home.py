"""FIN_HOME resolution, config show, and corpus ledger defaults.

A linked worktree (``.git`` is a file) must not create or modify the
operator home. Empty or relative ``FIN_HOME`` is an error. Secret
values are reported only as configured or not configured.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from fin.host.fin_home import (
    FinHomeError,
    profile_path,
    resolve_fin_home,
    run_dir,
)
from fin.host.operator_settings import (
    OperatorSettingsConfigError,
    format_config_show,
)
from fin.host.user_profile import UserProfileStore
from fin.host.workspace_registry import workspace_root_for_fin_home

_FAKE_SECRET = "fake-secret-do-not-print"
_REPO_CORPUS = Path(__file__).resolve().parents[2] / "data" / "corpus"


def _user_home(tmp_path: Path) -> Path:
    home = tmp_path / "user-home"
    home.mkdir(exist_ok=True)
    return home


def _primary(tmp_path: Path) -> Path:
    root = tmp_path / "primary"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return root


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir(exist_ok=True)
    git = root / ".git"
    if not git.exists():
        git.write_text("gitdir: /tmp/fin-gitdir\n", encoding="utf-8")
    return root


def test_absolute_fin_home_wins(tmp_path: Path) -> None:
    chosen = tmp_path / "chosen"
    resolved = resolve_fin_home(
        environ={"FIN_HOME": str(chosen)},
        repo_root=_worktree(tmp_path),
        user_home=_user_home(tmp_path),
    )
    assert resolved == chosen
    assert not chosen.exists()
    assert not (_user_home(tmp_path) / ".fin").exists()


@pytest.mark.parametrize("raw", ["", "   ", "relative", "./relative", "~/fin"])
def test_empty_or_relative_fin_home_raises(tmp_path: Path, raw: str) -> None:
    user_home = _user_home(tmp_path)
    with pytest.raises(FinHomeError):
        resolve_fin_home(
            environ={"FIN_HOME": raw},
            repo_root=_primary(tmp_path),
            user_home=user_home,
        )
    assert not (user_home / ".fin").exists()


def test_unset_linked_worktree_uses_checkout_home_not_user_home(
    tmp_path: Path,
) -> None:
    user_home = _user_home(tmp_path)
    root = _worktree(tmp_path)
    resolved = resolve_fin_home(
        environ={},
        repo_root=root,
        user_home=user_home,
    )
    assert resolved == root / ".fin-home"
    assert not resolved.exists()
    assert not (user_home / ".fin").exists()


def test_unset_primary_checkout_uses_user_fin_dir(tmp_path: Path) -> None:
    user_home = _user_home(tmp_path)
    resolved = resolve_fin_home(
        environ={},
        repo_root=_primary(tmp_path),
        user_home=user_home,
    )
    assert resolved == user_home / ".fin"
    assert not resolved.exists()


def test_profile_path_is_callable_under_the_resolved_home(tmp_path: Path) -> None:
    home = tmp_path / "fin-home"
    assert callable(profile_path)
    assert profile_path(home) == home / "user.md"


def test_run_dir_is_under_fin_home(tmp_path: Path) -> None:
    home = tmp_path / "fin-home"
    assert run_dir(home) == home / "run"


def test_workspace_root_is_default_under_fin_home(tmp_path: Path) -> None:
    home = tmp_path / "fin-home"
    root = workspace_root_for_fin_home(home)
    assert root == home / "default"
    assert root.is_dir()


def test_omitted_profile_home_follows_fin_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fin_home = tmp_path / "fin-home"
    operator = tmp_path / "operator"
    operator.mkdir()
    monkeypatch.setenv("FIN_HOME", str(fin_home))
    monkeypatch.setenv("HOME", str(operator))
    store = UserProfileStore.from_env(workspace_root=tmp_path / "ws")
    assert store.global_path == fin_home / "user.md"
    assert not (operator / ".fin").exists()


def test_fin_home_ledger_defaults_start_empty(tmp_path: Path) -> None:
    from fin.corpus.catalog import default_corpus_ledger_path

    home = tmp_path / "fin-home"
    pushed = default_corpus_ledger_path(
        "pushed_records.jsonl",
        environ={"FIN_HOME": str(home)},
        repo_root=_primary(tmp_path),
        user_home=_user_home(tmp_path),
    )
    index = default_corpus_ledger_path(
        "index_jobs.jsonl",
        environ={"FIN_HOME": str(home)},
        repo_root=_primary(tmp_path),
        user_home=_user_home(tmp_path),
    )
    assert pushed == home / "corpus" / "pushed_records.jsonl"
    assert index == home / "corpus" / "index_jobs.jsonl"
    assert not pushed.exists()
    assert not index.exists()
    assert pushed != _REPO_CORPUS / "pushed_records.jsonl"


def test_linked_worktree_ledgers_do_not_copy_the_repo(
    tmp_path: Path,
) -> None:
    from fin.corpus.catalog import default_corpus_ledger_path

    user_home = _user_home(tmp_path)
    root = _worktree(tmp_path)
    path = default_corpus_ledger_path(
        "pushed_records.jsonl",
        environ={},
        repo_root=root,
        user_home=user_home,
    )
    assert path == root / ".fin-home" / "corpus" / "pushed_records.jsonl"
    assert not path.exists()
    assert not (user_home / ".fin").exists()


def test_primary_unset_keeps_repo_corpus_ledgers(tmp_path: Path) -> None:
    from fin.corpus.catalog import default_corpus_ledger_path

    user_home = _user_home(tmp_path)
    path = default_corpus_ledger_path(
        "index_jobs.jsonl",
        environ={},
        repo_root=_primary(tmp_path),
        user_home=user_home,
    )
    assert path == user_home / ".fin" / "corpus" / "index_jobs.jsonl"
    assert not path.exists()
    assert not (user_home / ".fin").exists()


def test_relative_fin_home_does_not_fall_back_to_repo_corpus(
    tmp_path: Path,
) -> None:
    from fin.corpus.catalog import default_corpus_ledger_path

    user_home = _user_home(tmp_path)
    with pytest.raises(FinHomeError):
        default_corpus_ledger_path(
            "pushed_records.jsonl",
            environ={"FIN_HOME": "relative"},
            repo_root=_primary(tmp_path),
            user_home=user_home,
        )
    assert not (user_home / ".fin").exists()


def test_explicit_catalog_path_keeps_sibling_ledgers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host.workspace_http import _ingest_components

    for name in (
        "FIN_CORPUS_PUSHED_RECORDS_PATH",
        "FIN_CORPUS_INDEX_JOB_LEDGER_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FIN_HOME", str(tmp_path / "fin-home"))
    catalog = tmp_path / "custom" / "catalog.json"
    components = _ingest_components(catalog_path=catalog)
    assert components["pushed_records_path"] == (
        catalog.parent / "pushed_records.jsonl"
    )
    assert components["index_job_ledger_path"] == (
        catalog.parent / "index_jobs.jsonl"
    )


def test_unset_catalog_ledgers_follow_fin_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host.workspace_http import _ingest_components

    for name in (
        "FIN_CORPUS_PUSH_JOURNAL_PATH",
        "FIN_CORPUS_INVALIDATION_LEDGER_PATH",
        "FIN_CORPUS_PUSHED_RECORDS_PATH",
        "FIN_CORPUS_INDEX_JOB_LEDGER_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    fin_home = tmp_path / "fin-home"
    monkeypatch.setenv("FIN_HOME", str(fin_home))
    components = _ingest_components()
    assert components["pushed_records_path"] == (
        fin_home / "corpus" / "pushed_records.jsonl"
    )
    assert components["index_job_ledger_path"] == (
        fin_home / "corpus" / "index_jobs.jsonl"
    )
    assert not components["pushed_records_path"].exists()
    assert not components["index_job_ledger_path"].exists()
    assert components["push_journal_path"] == _REPO_CORPUS / "push_journal.jsonl"


def _settings(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_show_reports_checkpoint_from_settings(tmp_path: Path) -> None:
    path = _settings(
        tmp_path,
        "checkpoint:\n  url: postgresql://from-settings/fin\n",
    )
    text = format_config_show(
        environ={},
        settings_path=path,
        defaults={},
    )
    assert (
        "FIN_CHECKPOINT_URL=postgresql://from-settings/fin settings.yaml"
        in text
    )


def test_config_show_reports_checkpoint_from_settings_when_env_matches(
    tmp_path: Path,
) -> None:
    url = "postgresql://from-settings/fin"
    path = _settings(tmp_path, f"checkpoint:\n  url: {url}\n")
    text = format_config_show(
        environ={"FIN_CHECKPOINT_URL": url},
        settings_path=path,
        defaults={},
    )
    assert f"FIN_CHECKPOINT_URL={url} settings.yaml" in text
    assert "env overrides" not in text


def test_config_show_names_an_env_override_and_hides_the_secret(
    tmp_path: Path,
) -> None:
    path = _settings(
        tmp_path,
        "\n".join(
            [
                "llm:",
                "  model: from-file",
                f"  apiKey: {_FAKE_SECRET}",
                "jev:",
                f"  apiKey: {_FAKE_SECRET}",
                "",
            ]
        ),
    )
    text = format_config_show(
        environ={
            "FIN_MODEL": "from-env",
            "FIN_OTHER_TOKEN": _FAKE_SECRET,
        },
        settings_path=path,
        defaults={},
    )
    assert "FIN_MODEL=from-env env overrides settings.yaml" in text
    assert "FIN_API_KEY configured settings.yaml" in text
    assert "FIN_JEV_API_KEY configured settings.yaml" in text
    assert "FIN_OTHER_TOKEN configured env" in text
    assert _FAKE_SECRET not in text


def test_config_show_unreadable_settings_raises(tmp_path: Path) -> None:
    path = _settings(tmp_path, "checkpoint:\n  url: postgresql://from-settings/fin\n")
    path.chmod(stat.S_IWUSR)
    path.chmod(0)
    try:
        with pytest.raises(OperatorSettingsConfigError):
            format_config_show(environ={}, settings_path=path, defaults={})
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_process_workspace_follows_fin_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host.workspace_http import default_workspace_root_for_process

    operator = tmp_path / "operator"
    operator.mkdir()
    fin_home = tmp_path / "fin-home"
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("FIN_HOME", str(fin_home))
    root = default_workspace_root_for_process()
    assert root == fin_home / "default"
    assert root.is_dir()
    assert not (operator / ".fin").exists()


def test_cli_omitted_home_follows_fin_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fin.host.cli import run

    operator = tmp_path / "operator"
    operator.mkdir()
    fin_home = tmp_path / "fin-home"
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("FIN_HOME", str(fin_home))

    class _Graph:
        def __init__(self) -> None:
            self.calls: list[tuple[object, dict]] = []

        def invoke(self, payload: object, config: dict | None = None, **kwargs: object):
            self.calls.append((payload, config or {}))
            return {"status": "done", "answer": "", "citations": ()}

    graph = _Graph()
    run("q", graph=graph, thread_id="t-home")
    configurable = graph.calls[0][1]["configurable"]
    assert configurable["workspace_root"] == str(fin_home / "default")
    assert not (operator / ".fin").exists()


def test_settings_path_follows_absolute_fin_home(tmp_path: Path) -> None:
    from fin.host.operator_settings import resolve_settings_path

    home = tmp_path / "fin-home"
    path = resolve_settings_path(environ={"FIN_HOME": str(home)})
    assert path == home / "settings.yaml"
