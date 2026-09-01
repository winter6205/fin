"""Shared append-only JSONL ledger (Plan A A1 / maintainability T1).

The five domain ledgers must keep their named error types and codes.
The append / replay / I/O wrap lives in one internal primitive.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from fin.retrieve.degradation import DegradationLedgerCorruptError
from fin.retrieve.input_validation import InputValidationLedgerUnwritableError
from fin.retrieve.push_batching import (
    PushJournalCorruptError,
    PushJournalUnreadableError,
    PushJournalUnwritableError,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DOMAIN_LEDGER_FILES = (
    "src/fin/retrieve/input_validation",
    "src/fin/retrieve/degradation.py",
    "src/fin/retrieve/hit_mapping.py",
    "src/fin/retrieve/invalidation_port",
    "src/fin/retrieve/push_batching.py",
)
_PRIMITIVE = (
    _PROJECT_ROOT / "src" / "fin" / "retrieve" / "_ledger" / "jsonl.py"
)

_STABLE_CODES = (
    "push_journal_corrupt",
    "invalidation_ledger_unwritable",
    "degradation_ledger_corrupt",
    "mapping_failure_ledger_corrupt",
    "input_validation_ledger_corrupt",
)


def _call_func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _domain_python_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for relative in _DOMAIN_LEDGER_FILES:
        path = _PROJECT_ROOT / relative
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        else:
            files.append(path)
    return tuple(files)


def test_domain_ledgers_do_not_reimplement_jsonl_append_or_replay() -> None:
    offenders: list[str] = []
    for path in _domain_python_files():
        relative = path.relative_to(_PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_func_name(node.func) == "dumps":
                offenders.append(f"{relative}:{node.lineno}")
            if (
                isinstance(node, ast.Call)
                and _call_func_name(node.func) == "open"
                and any(
                    isinstance(keyword, ast.keyword)
                    and keyword.arg == "mode"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "a"
                    for keyword in node.keywords
                )
            ):
                offenders.append(f"{relative}:{node.lineno}:open-a")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"
                and any(
                    isinstance(arg, ast.Constant) and arg.value == "a"
                    for arg in node.args
                )
            ):
                offenders.append(f"{relative}:{node.lineno}:open-a")
    assert offenders == []


def test_domain_ledgers_have_no_jsonl_append_source_matches() -> None:
    """rg-shaped companion to the AST proof (Plan A A1 dual self-check)."""
    hits: list[str] = []
    for path in _domain_python_files():
        relative = path.relative_to(_PROJECT_ROOT).as_posix()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "json.dumps" in line or '.open("a"' in line or ".open('a'" in line:
                hits.append(f"{relative}:{number}")
    assert hits == []


def test_shared_jsonl_primitive_module_exists() -> None:
    assert _PRIMITIVE.is_file()


def test_stable_ledger_error_codes_remain_verbatim() -> None:
    from fin.retrieve.degradation import DegradationLedgerCorruptError
    from fin.retrieve.hit_mapping import MappingFailureLedgerCorruptError
    from fin.retrieve.input_validation import InputValidationLedgerCorruptError
    from fin.retrieve.invalidation_port import InvalidationLedgerUnwritableError
    from fin.retrieve.push_batching import PushJournalCorruptError

    assert PushJournalCorruptError.code == "push_journal_corrupt"
    assert InvalidationLedgerUnwritableError.code == "invalidation_ledger_unwritable"
    assert DegradationLedgerCorruptError.code == "degradation_ledger_corrupt"
    assert MappingFailureLedgerCorruptError.code == "mapping_failure_ledger_corrupt"
    assert InputValidationLedgerCorruptError.code == "input_validation_ledger_corrupt"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in _domain_python_files()
    )
    for code in _STABLE_CODES:
        assert f'"{code}"' in source or f"'{code}'" in source


def test_append_jsonl_is_a_noop_without_a_path() -> None:
    from fin.retrieve._ledger.jsonl import append_jsonl_object

    append_jsonl_object(
        None,
        {"k": "v"},
        unwritable=InputValidationLedgerUnwritableError,
        message="unused",
    )


def test_append_jsonl_wraps_oserror_in_domain_unwritable(tmp_path: Path) -> None:
    from fin.retrieve._ledger.jsonl import append_jsonl_object

    blocked = tmp_path / "missing-parent-is-a-file"
    blocked.write_text("x", encoding="utf-8")
    path = blocked / "ledger.jsonl"
    with pytest.raises(InputValidationLedgerUnwritableError) as excinfo:
        append_jsonl_object(
            path,
            {"code": "empty_query"},
            unwritable=InputValidationLedgerUnwritableError,
            message=f"cannot append the input-validation record to {path}",
        )
    assert "cannot append" in str(excinfo.value)


def test_append_jsonl_writes_sorted_object_line(tmp_path: Path) -> None:
    from fin.retrieve._ledger.jsonl import append_jsonl_object

    path = tmp_path / "ledgers" / "n.jsonl"
    append_jsonl_object(
        path,
        {"b": 1, "a": 2},
        unwritable=InputValidationLedgerUnwritableError,
        message="unused",
        ensure_ascii=False,
    )
    assert path.read_text(encoding="utf-8") == '{"a": 2, "b": 1}\n'


def test_replay_jsonl_skips_blank_lines_and_parses_objects(tmp_path: Path) -> None:
    from fin.retrieve._ledger.jsonl import replay_jsonl_objects

    path = tmp_path / "ledger.jsonl"
    path.write_text('\n{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
    rows = replay_jsonl_objects(
        path,
        parse=lambda event, _number: event["a"],
        on_read_oserror=lambda exc: DegradationLedgerCorruptError(str(exc)),
        on_unicode=lambda exc: DegradationLedgerCorruptError(str(exc)),
        on_json=lambda exc, number: DegradationLedgerCorruptError(
            f"line {number}: {exc}"
        ),
    )
    assert rows == [1, 2]


def test_replay_jsonl_wraps_invalid_json_as_corrupt(tmp_path: Path) -> None:
    from fin.retrieve._ledger.jsonl import replay_jsonl_objects

    path = tmp_path / "journal.jsonl"
    path.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(PushJournalCorruptError):
        replay_jsonl_objects(
            path,
            parse=lambda event, _number: event,
            on_read_oserror=lambda exc: PushJournalUnreadableError(str(exc)),
            on_unicode=lambda exc: PushJournalCorruptError(str(exc)),
            on_json=lambda exc, number: PushJournalCorruptError(
                f"push journal {path} line {number} is not valid JSON: {exc}"
            ),
        )
