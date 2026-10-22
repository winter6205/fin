"""Operator settings file: ``~/.fin/settings.yaml`` projects into ``FIN_*``.

Boot reads this file and fills names that ``from_environ`` already
understands. A key already in the process environment stays. A missing
file applies nothing. A present file that is not the schema raises
``OperatorSettingsConfigError`` and names the field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fin.host.operator_settings import (
    OperatorSettingsConfigError,
    apply_operator_settings,
    format_config_show,
    projected_additions,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_applies_nothing(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    loaded = apply_operator_settings(
        environ=environ, path=tmp_path / "missing.yaml"
    )
    assert loaded is None
    assert environ == {}


def test_empty_mapping_applies_nothing(tmp_path: Path) -> None:
    path = _write(tmp_path, "{}\n")
    environ: dict[str, str] = {}
    assert apply_operator_settings(environ=environ, path=path) == path
    assert environ == {}


def test_config_show_does_not_invent_a_model(tmp_path: Path) -> None:
    text = format_config_show(
        environ={},
        settings_path=tmp_path / "missing.yaml",
        repo_root=Path(__file__).resolve().parents[2],
    )

    assert "FIN_MODEL" not in text


def test_yaml_fills_an_unset_key(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  model: from-file\n")
    environ: dict[str, str] = {}
    apply_operator_settings(environ=environ, path=path)
    assert environ["FIN_MODEL"] == "from-file"


def test_pre_set_process_key_is_kept(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  model: from-file\n")
    environ = {"FIN_MODEL": "from-process"}
    apply_operator_settings(environ=environ, path=path)
    assert environ["FIN_MODEL"] == "from-process"


def test_placeholder_expands_from_the_process_environment(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  model: ${CHOSEN_MODEL}\n")
    environ = {"CHOSEN_MODEL": "synthetic-configured-model"}
    apply_operator_settings(environ=environ, path=path)
    assert environ["FIN_MODEL"] == "synthetic-configured-model"
    assert environ["CHOSEN_MODEL"] == "synthetic-configured-model"


def test_unset_placeholder_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  apiKey: ${FIN_API_KEY}\n")
    with pytest.raises(OperatorSettingsConfigError, match=r"llm\.apiKey") as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "llm.apiKey"


def test_unknown_key_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  model: synthetic-configured-model\n  extra: 1\n")
    with pytest.raises(OperatorSettingsConfigError, match="extra") as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "llm.extra"


def test_negative_max_bytes_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "\n".join(
            [
                "attachment:",
                "  maxBytes: -1",
                "  maxPdfPages: 1",
                "  visionCallsPerRun: 3",
                "  visionCropCallsPerRun: 1",
                "",
            ]
        ),
    )
    with pytest.raises(OperatorSettingsConfigError, match="maxBytes") as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "attachment.maxBytes"


def test_vision_calls_below_pages_plus_crops_plus_one_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "\n".join(
            [
                "attachment:",
                "  maxBytes: 10",
                "  maxPdfPages: 80",
                "  visionCallsPerRun: 88",
                "  visionCropCallsPerRun: 8",
                "",
            ]
        ),
    )
    with pytest.raises(
        OperatorSettingsConfigError, match="visionCallsPerRun"
    ) as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "attachment.visionCallsPerRun"


def test_malformed_yaml_raises_operator_settings_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm: [\n")
    with pytest.raises(OperatorSettingsConfigError, match="malformed YAML"):
        apply_operator_settings(environ={}, path=path)


def test_bad_adapter_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "retrieve:\n  adapter: http\n")
    with pytest.raises(OperatorSettingsConfigError, match="adapter") as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "retrieve.adapter"


def test_memory_bool_becomes_true_or_false(tmp_path: Path) -> None:
    on = _write(tmp_path, "memory:\n  enabled: true\n")
    enabled: dict[str, str] = {}
    apply_operator_settings(environ=enabled, path=on)
    assert enabled["FIN_MEMORY_ENABLED"] == "true"

    off = tmp_path / "off.yaml"
    off.write_text("memory:\n  enabled: false\n", encoding="utf-8")
    disabled: dict[str, str] = {}
    apply_operator_settings(environ=disabled, path=off)
    assert disabled["FIN_MEMORY_ENABLED"] == "false"


def test_corpus_scope_id_projects_to_its_env_key(tmp_path: Path) -> None:
    path = _write(tmp_path, "corpus:\n  scopeId: team-a\n")
    environ: dict[str, str] = {}

    apply_operator_settings(environ=environ, path=path)

    assert environ["FIN_CORPUS_SCOPE_ID"] == "team-a"


def test_a_blank_corpus_scope_id_is_refused(tmp_path: Path) -> None:
    """A present-but-blank scope is a config error, not a silent default:
    the operator asked for a scope and must not get a different one."""
    path = _write(tmp_path, 'corpus:\n  scopeId: ""\n')

    with pytest.raises(OperatorSettingsConfigError) as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "corpus.scopeId"


def test_heron_relative_path_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "\n".join(
            [
                "attachment:",
                "  maxBytes: 10",
                "  maxPdfPages: 1",
                "  visionCallsPerRun: 3",
                "  visionCropCallsPerRun: 1",
                "  heron:",
                "    script: relative/run.sh",
                "",
            ]
        ),
    )
    with pytest.raises(OperatorSettingsConfigError, match="script") as excinfo:
        apply_operator_settings(environ={}, path=path)
    assert excinfo.value.field == "attachment.heron.script"


def test_fin_settings_file_points_at_the_file_under_test(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  baseUrl: https://example.invalid/v1\n")
    environ = {"FIN_SETTINGS_FILE": str(path)}
    apply_operator_settings(environ=environ)
    assert environ["FIN_BASE_URL"] == "https://example.invalid/v1"


def test_illegal_model_type_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  model: 3\n")
    with pytest.raises(OperatorSettingsConfigError, match=r"llm\.model"):
        apply_operator_settings(environ={}, path=path)


def test_projected_additions_expands_placeholder_from_the_base_mapping(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "llm:\n  apiKey: ${OTHER}\n")
    result = projected_additions(
        environ={"OTHER": "secret", "FIN_SETTINGS_FILE": str(path)}
    )
    assert result["FIN_API_KEY"] == "secret"
    assert "OTHER" not in result


def test_projected_additions_omits_a_key_already_present(tmp_path: Path) -> None:
    path = _write(tmp_path, "llm:\n  apiKey: ${FIN_API_KEY}\n")
    result = projected_additions(
        environ={"FIN_API_KEY": "already-set", "FIN_SETTINGS_FILE": str(path)}
    )
    assert "FIN_API_KEY" not in result


def test_projected_additions_raises_when_placeholder_name_is_absent(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "llm:\n  apiKey: ${UNSET_NAME}\n")
    with pytest.raises(OperatorSettingsConfigError):
        projected_additions(environ={"FIN_SETTINGS_FILE": str(path)})


def test_object_store_section_projects_the_durable_endpoint(tmp_path: Path) -> None:
    """The five agreed objectStore fields become the FIN_OBJECT_STORE_* env names."""
    path = _write(
        tmp_path,
        "\n".join(
            [
                "objectStore:",
                "  url: http://127.0.0.1:8333",
                "  bucket: fin-dev",
                "  accessKey: local-access",
                "  secretKey: local-secret",
                "  region: us-east-1",
                "",
            ]
        ),
    )
    environ: dict[str, str] = {}
    apply_operator_settings(environ=environ, path=path)
    assert environ["FIN_OBJECT_STORE_URL"] == "http://127.0.0.1:8333"
    assert environ["FIN_OBJECT_STORE_BUCKET"] == "fin-dev"
    assert environ["FIN_OBJECT_STORE_ACCESS_KEY"] == "local-access"
    assert environ["FIN_OBJECT_STORE_SECRET_KEY"] == "local-secret"
    assert environ["FIN_OBJECT_STORE_REGION"] == "us-east-1"


def test_object_store_unknown_field_raises(tmp_path: Path) -> None:
    """An extra objectStore key is a config error and writes nothing."""
    path = _write(tmp_path, "objectStore:\n  endpoint: http://127.0.0.1:8333\n")
    environ: dict[str, str] = {}
    with pytest.raises(OperatorSettingsConfigError, match="endpoint") as excinfo:
        apply_operator_settings(environ=environ, path=path)
    assert excinfo.value.field == "objectStore.endpoint"
    assert environ == {}


def test_config_show_hides_credentials_embedded_in_the_object_store_url(
    tmp_path: Path,
) -> None:
    """config show must not print ak:sk carried in FIN_OBJECT_STORE_URL.

    The process environment keeps the original URL. Only the printed line
    drops the userinfo. A URL with no userinfo is printed in full, and the
    accessKey / secretKey fields stay on the configured/not-configured path.
    """
    access = "object-store-access"
    secret = "object-store-secret"
    raw_url = f"http://{access}:{secret}@127.0.0.1:8333"
    plain_url = "http://127.0.0.1:8333"
    path = _write(
        tmp_path,
        "\n".join(
            [
                "objectStore:",
                f"  url: {raw_url}",
                "  bucket: fin-dev",
                "  accessKey: file-access-key",
                "  secretKey: file-secret-key",
                "",
            ]
        ),
    )
    environ: dict[str, str] = {}
    apply_operator_settings(environ=environ, path=path)
    assert environ["FIN_OBJECT_STORE_URL"] == raw_url
    text = format_config_show(environ={}, settings_path=path, defaults={})
    assert access not in text
    assert secret not in text
    assert "file-access-key" not in text
    assert "file-secret-key" not in text
    assert "FIN_OBJECT_STORE_ACCESS_KEY configured settings.yaml" in text
    assert "FIN_OBJECT_STORE_SECRET_KEY configured settings.yaml" in text
    assert "FIN_OBJECT_STORE_URL=" in text
    assert "127.0.0.1:8333" in text
    assert "settings.yaml" in text
    plain = _write(
        tmp_path,
        "\n".join(
            [
                "objectStore:",
                f"  url: {plain_url}",
                "  bucket: fin-dev",
                "",
            ]
        ),
    )
    plain_text = format_config_show(
        environ={}, settings_path=plain, defaults={}
    )
    assert f"FIN_OBJECT_STORE_URL={plain_url} settings.yaml" in plain_text
