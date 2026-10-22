"""Project ``~/.fin/settings.yaml`` into the ``FIN_*`` names readers already use.

There is no dotenv step. A missing file applies nothing, so CI and a
checkout without operator settings still boot from the process
environment. Keys already present are left alone: monkeypatch, CI, and
an explicit launcher export win over the file.

``${NAME}`` is a whole-string placeholder. It expands from the process
environment only; an unset name is a config error, not a prompt to read
another file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from fin.host.fin_home import checkout_root, resolve_fin_home

SETTINGS_FILE_ENV = "FIN_SETTINGS_FILE"

_PLACEHOLDER_PREFIX = "${"
_PLACEHOLDER_SUFFIX = "}"

_LLM_FIELDS: dict[str, str] = {
    "model": "FIN_MODEL",
    "baseUrl": "FIN_BASE_URL",
    "apiKey": "FIN_API_KEY",
}
_CHECKPOINT_FIELDS: dict[str, str] = {"url": "FIN_CHECKPOINT_URL"}
_OBJECT_STORE_FIELDS: dict[str, str] = {
    "url": "FIN_OBJECT_STORE_URL",
    "bucket": "FIN_OBJECT_STORE_BUCKET",
    "accessKey": "FIN_OBJECT_STORE_ACCESS_KEY",
    "secretKey": "FIN_OBJECT_STORE_SECRET_KEY",
    "region": "FIN_OBJECT_STORE_REGION",
}
_JEV_FIELDS: dict[str, str] = {
    "apiKey": "FIN_JEV_API_KEY",
    "model": "FIN_JEV_MODEL",
    "baseUrl": "FIN_JEV_BASE_URL",
}
_CORPUS_FIELDS: dict[str, str] = {"scopeId": "FIN_CORPUS_SCOPE_ID"}
_ATTACHMENT_INTS: tuple[tuple[str, str], ...] = (
    ("maxBytes", "FIN_ATTACHMENT_MAX_BYTES"),
    ("maxPdfPages", "FIN_ATTACHMENT_MAX_PDF_PAGES"),
    ("visionCallsPerRun", "FIN_ATTACHMENT_VISION_CALLS_PER_RUN"),
    ("visionCropCallsPerRun", "FIN_ATTACHMENT_VISION_CROP_CALLS_PER_RUN"),
)
_HERON_PATHS: tuple[tuple[str, str], ...] = (
    ("script", "FIN_ATTACHMENT_HERON_SCRIPT"),
    ("stagingDir", "FIN_ATTACHMENT_HERON_STAGING_DIR"),
    ("modelCache", "FIN_ATTACHMENT_HERON_MODEL_CACHE"),
)
_ROOT_KEYS = frozenset(
    {
        "llm",
        "checkpoint",
        "objectStore",
        "retrieve",
        "jev",
        "memory",
        "attachment",
        "corpus",
    }
)
_ATTACHMENT_KEYS = frozenset(name for name, _env in _ATTACHMENT_INTS) | {"heron"}
_HERON_KEYS = frozenset(
    {"timeoutS", "slicePass", "image", "script", "stagingDir", "modelCache"}
)
_ADAPTERS = frozenset({"mcp", "fake"})
_EXAMPLE_NAME = "settings.example.yaml"
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
_ALWAYS_SHOWN_SECRETS = ("FIN_API_KEY", "FIN_JEV_API_KEY")
# Same strings scripts/dev-up.sh exports when neither the process nor
# settings.yaml set the key. Importing compile would load the host graph.
_LAUNCHER_DEFAULTS = {
    "FIN_BASE_URL": "https://api.minimaxi.com/v1",
    "FIN_RETRIEVE_ADAPTER": "mcp",
}


class OperatorSettingsConfigError(Exception):
    """A present settings file is unusable. ``field`` names the key."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        super().__init__(f"{field}: {detail}")


def resolve_settings_path(*, environ: Mapping[str, str]) -> Path:
    """``FIN_SETTINGS_FILE`` when set, otherwise ``<FIN_HOME>/settings.yaml``."""
    override = environ.get(SETTINGS_FILE_ENV, "").strip()
    if override:
        return Path(override)
    return resolve_fin_home(environ=environ) / "settings.yaml"


def format_config_show(
    *,
    environ: Mapping[str, str],
    settings_path: Path | None = None,
    defaults: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> str:
    """Each effective ``FIN_*`` value and the layer that won.

    Secret names are ``configured`` or ``not configured``. The value
    itself is never included.
    """
    projected = _settings_projection(environ, settings_path)
    chosen = defaults if defaults is not None else _builtin_defaults(repo_root)
    lines = [
        line
        for key in _candidate_keys(environ, projected, chosen)
        if (
            line := _format_config_line(
                key, *_winner(key, environ, projected, chosen)
            )
        )
        is not None
    ]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _builtin_defaults(repo_root: Path | None) -> dict[str, str]:
    root = repo_root if repo_root is not None else checkout_root()
    defaults = dict(_LAUNCHER_DEFAULTS)
    defaults.update(shipped_attachment_bounds(repo_root=root))
    return defaults


def _settings_projection(
    environ: Mapping[str, str], settings_path: Path | None
) -> dict[str, str]:
    path = (
        settings_path
        if settings_path is not None
        else resolve_settings_path(environ=environ)
    )
    if not path.is_file():
        return {}  # EXIT: a missing settings file contributes no layer
    return project_operator_settings(path, environ=environ)


def _candidate_keys(
    environ: Mapping[str, str],
    projected: Mapping[str, str],
    defaults: Mapping[str, str],
) -> list[str]:
    keys = set(defaults)
    keys.update(projected)
    keys.update(key for key in environ if key.startswith("FIN_"))
    keys.update(_ALWAYS_SHOWN_SECRETS)
    return sorted(keys)


def _present(source: Mapping[str, str], key: str) -> str | None:
    if key not in source:
        return None
    text = source[key].strip()
    if text == "":
        return None  # EXIT: a blank value is unset, so a lower layer can win
    return text


def _winner(
    key: str,
    environ: Mapping[str, str],
    projected: Mapping[str, str],
    defaults: Mapping[str, str],
) -> tuple[str | None, str, bool]:
    env_value = _present(environ, key)
    settings_value = _present(projected, key)
    if env_value is not None:
        if settings_value is None:
            return env_value, "env", False
        if env_value == settings_value:
            return settings_value, "settings.yaml", False
        return env_value, "env", True
    if settings_value is not None:
        return settings_value, "settings.yaml", False
    default_value = _present(defaults, key)
    if default_value is not None:
        return default_value, "default", False
    return None, "", False


def _is_secret_name(name: str) -> bool:
    folded = name.upper()
    return any(marker in folded for marker in _SECRET_MARKERS)


def _redact_url_userinfo(value: str) -> str:
    """Replace ``user:password`` so a printed URL cannot show ak:sk.

    ``FIN_OBJECT_STORE_URL`` is not a secret name, so config show would
    otherwise copy ``http://ak:sk@host`` to stdout. The value kept for
    the process is unchanged; only the display line is rewritten.
    """
    parts = urlsplit(value)
    if parts.username is None and parts.password is None:
        return value
    hostname = parts.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    host = hostname
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    netloc = f"[redacted]@{host}" if host else "[redacted]"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _display_value(key: str, value: str) -> str:
    if key == "FIN_OBJECT_STORE_URL":
        return _redact_url_userinfo(value)
    return value


def _format_config_line(
    key: str, value: str | None, layer: str, overrides: bool
) -> str | None:
    if value is None:
        if key in _ALWAYS_SHOWN_SECRETS:
            return f"{key} not configured"
        return None  # EXIT: a non-secret with no winner is omitted
    if _is_secret_name(key) and overrides:
        return f"{key} configured env overrides settings.yaml"
    if _is_secret_name(key):
        return f"{key} configured {layer}"
    shown = _display_value(key, value)
    if overrides:
        return f"{key}={shown} env overrides settings.yaml"
    return f"{key}={shown} {layer}"


def apply_operator_settings(
    *,
    environ: MutableMapping[str, str] | None = None,
    path: Path | None = None,
) -> Path | None:
    """Fill unset ``FIN_*`` keys from the settings file.

    Returns the path that was applied. A missing file returns ``None``
    and leaves ``environ`` unchanged.
    """
    target: MutableMapping[str, str] = os.environ if environ is None else environ
    resolved = path if path is not None else resolve_settings_path(environ=target)
    if not resolved.is_file():
        return None  # EXIT: missing settings file applies nothing
    projected = project_operator_settings(resolved, environ=target)
    for key, value in projected.items():
        if value == "":
            continue  # EXIT: never write an empty string
        target.setdefault(key, value)
    return resolved


def projected_additions(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """``FIN_*`` keys the settings file adds. Names already set stay out."""
    source: Mapping[str, str] = os.environ if environ is None else environ
    target = dict(source)
    present = set(target)
    apply_operator_settings(environ=target)
    return {key: value for key, value in target.items() if key not in present}


def project_operator_settings(
    path: Path, *, environ: Mapping[str, str]
) -> dict[str, str]:
    """Validate ``path`` and return the ``FIN_*`` projection. Does not apply."""
    document = _load_document(path)
    _reject_unknown("", document, _ROOT_KEYS)
    projected: dict[str, str] = {}
    if "llm" in document:
        projected.update(
            _string_section("llm", document["llm"], _LLM_FIELDS, environ)
        )
    if "checkpoint" in document:
        projected.update(
            _string_section(
                "checkpoint", document["checkpoint"], _CHECKPOINT_FIELDS, environ
            )
        )
    if "objectStore" in document:
        projected.update(
            _string_section(
                "objectStore",
                document["objectStore"],
                _OBJECT_STORE_FIELDS,
                environ,
            )
        )
    if "retrieve" in document:
        projected.update(_retrieve(document["retrieve"], environ))
    if "jev" in document:
        projected.update(_string_section("jev", document["jev"], _JEV_FIELDS, environ))
    if "memory" in document:
        projected.update(_memory(document["memory"]))
    if "corpus" in document:
        projected.update(
            _string_section(
                "corpus", document["corpus"], _CORPUS_FIELDS, environ
            )
        )
    if "attachment" in document:
        projected.update(_attachment(document["attachment"], environ))
    return projected


def shipped_attachment_bounds(*, repo_root: Path) -> dict[str, str]:
    """The four attachment bounds as ``settings.example.yaml`` ships them.

    Decimal strings, same shape ``from_environ`` reads. A missing bound
    is an error that names the ``FIN_ATTACHMENT_*`` key. Other sections
    of the example are not expanded: this helper exists so shell and
    node launchers do not grow a YAML parser.
    """
    path = repo_root / _EXAMPLE_NAME
    bound_names = ", ".join(env for _name, env in _ATTACHMENT_INTS)
    if not path.is_file():
        raise OperatorSettingsConfigError(
            "attachment",
            f"{_EXAMPLE_NAME} does not define {bound_names}",
        )  # EXIT: a launcher must not invent a cap
    document = _load_document(path)
    attachment = document.get("attachment")
    if not isinstance(attachment, dict):
        raise OperatorSettingsConfigError(
            "attachment",
            f"{_EXAMPLE_NAME} does not define {bound_names}",
        )  # EXIT: the template is the only source of the four numbers
    _reject_unknown("attachment", attachment, _ATTACHMENT_KEYS)
    missing = [env for name, env in _ATTACHMENT_INTS if name not in attachment]
    if missing:
        raise OperatorSettingsConfigError(
            "attachment",
            f"{_EXAMPLE_NAME} does not define {', '.join(missing)}",
        )  # EXIT: a partial template is not a deployment
    numbers = {
        name: _positive_int(f"attachment.{name}", attachment[name])
        for name, _env in _ATTACHMENT_INTS
    }
    _check_vision_budget(numbers)
    return {env: str(numbers[name]) for name, env in _ATTACHMENT_INTS}


def _load_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OperatorSettingsConfigError(path.name, "could not be read") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        raise OperatorSettingsConfigError(
            path.name, "malformed YAML"
        ) from None  # EXIT: the parser error must not escape this module
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise OperatorSettingsConfigError(
            path.name, "must be a mapping"
        )  # EXIT: a list or scalar is not a settings file
    return loaded


def _reject_unknown(prefix: str, value: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = [key for key in value if key not in allowed]
    if not unknown:
        return
    name = str(unknown[0])
    field = f"{prefix}.{name}" if prefix else name
    raise OperatorSettingsConfigError(
        field, "is not a settings field"
    )  # EXIT: extra keys are forbidden at every level


def _mapping(field: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OperatorSettingsConfigError(
            field, "must be a mapping"
        )  # EXIT: a section is a mapping or absent
    return value


def _expand(field: str, value: str, environ: Mapping[str, str]) -> str:
    if not (
        value.startswith(_PLACEHOLDER_PREFIX)
        and value.endswith(_PLACEHOLDER_SUFFIX)
        and value.count(_PLACEHOLDER_PREFIX) == 1
    ):
        return value
    name = value[len(_PLACEHOLDER_PREFIX) : -len(_PLACEHOLDER_SUFFIX)]
    if not name.isidentifier():
        return value
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        raise OperatorSettingsConfigError(
            field, f"${{{name}}} is unset in the process environment"
        )  # EXIT: do not read a file to fill a placeholder
    return raw


def _nonempty_str(field: str, value: object, environ: Mapping[str, str]) -> str:
    if not isinstance(value, str):
        raise OperatorSettingsConfigError(
            field, "must be a string"
        )  # EXIT: a numeric model id is not the configured name
    text = _expand(field, value, environ).strip()
    if text == "":
        raise OperatorSettingsConfigError(
            field, "must be non-empty"
        )  # EXIT: an empty string is not a setting
    return text


def _positive_int(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OperatorSettingsConfigError(
            field, "must be a positive integer"
        )  # EXIT: a bound of zero or a boolean admits the wrong work
    return value


def _string_section(
    prefix: str,
    section: object,
    fields: Mapping[str, str],
    environ: Mapping[str, str],
) -> dict[str, str]:
    body = _mapping(prefix, section)
    _reject_unknown(prefix, body, frozenset(fields))
    projected: dict[str, str] = {}
    for name, env_key in fields.items():
        if name not in body:
            continue
        projected[env_key] = _nonempty_str(f"{prefix}.{name}", body[name], environ)
    return projected


def _retrieve(section: object, environ: Mapping[str, str]) -> dict[str, str]:
    body = _mapping("retrieve", section)
    _reject_unknown("retrieve", body, frozenset({"adapter"}))
    if "adapter" not in body:
        return {}
    adapter = _nonempty_str("retrieve.adapter", body["adapter"], environ)
    if adapter not in _ADAPTERS:
        raise OperatorSettingsConfigError(
            "retrieve.adapter", "must be 'mcp' or 'fake'"
        )  # EXIT: compile refuses any other kind
    return {"FIN_RETRIEVE_ADAPTER": adapter}


def _memory(section: object) -> dict[str, str]:
    body = _mapping("memory", section)
    _reject_unknown("memory", body, frozenset({"enabled"}))
    if "enabled" not in body:
        return {}
    enabled = body["enabled"]
    if not isinstance(enabled, bool):
        raise OperatorSettingsConfigError(
            "memory.enabled", "must be a boolean"
        )  # EXIT: only true/false match memory_port's true/false strings
    return {"FIN_MEMORY_ENABLED": "true" if enabled else "false"}


def _attachment_ints(body: Mapping[str, Any]) -> dict[str, int]:
    missing = [name for name, _env in _ATTACHMENT_INTS if name not in body]
    if missing:
        raise OperatorSettingsConfigError(
            f"attachment.{missing[0]}",
            "is required when attachment is present",
        )  # EXIT: a partial attachment section is not a deployment
    return {
        name: _positive_int(f"attachment.{name}", body[name])
        for name, _env in _ATTACHMENT_INTS
    }


def _check_vision_budget(numbers: Mapping[str, int]) -> None:
    minimum = numbers["maxPdfPages"] + numbers["visionCropCallsPerRun"] + 1
    if numbers["visionCallsPerRun"] < minimum:
        raise OperatorSettingsConfigError(
            "attachment.visionCallsPerRun",
            "must be >= maxPdfPages + visionCropCallsPerRun + 1 "
            f"({minimum})",
        )  # EXIT: the run must fit pages, crops, and one retry


def _absolute_path(field: str, value: object, environ: Mapping[str, str]) -> str:
    text = _nonempty_str(field, value, environ)
    if not Path(text).is_absolute():
        raise OperatorSettingsConfigError(
            field, "must be an absolute path"
        )  # EXIT: a relative harness path depends on the launch cwd
    return text


def _heron_paths(body: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    projected: dict[str, str] = {}
    for name, env_key in _HERON_PATHS:
        if name not in body:
            continue
        projected[env_key] = _absolute_path(
            f"attachment.heron.{name}", body[name], environ
        )
    return projected


def _heron_scalars(body: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    projected: dict[str, str] = {}
    if "timeoutS" in body:
        timeout = _positive_int("attachment.heron.timeoutS", body["timeoutS"])
        projected["FIN_ATTACHMENT_HERON_TIMEOUT_S"] = str(timeout)
    if "slicePass" in body:
        projected["FIN_ATTACHMENT_HERON_SLICE_PASS"] = _bool_text(
            "attachment.heron.slicePass", body["slicePass"]
        )
    if "image" in body:
        projected["FIN_ATTACHMENT_HERON_IMAGE"] = _nonempty_str(
            "attachment.heron.image", body["image"], environ
        )
    return projected


def _bool_text(field: str, value: object) -> str:
    if not isinstance(value, bool):
        raise OperatorSettingsConfigError(
            field, "must be a boolean"
        )  # EXIT: yes/no strings are not this schema
    return "true" if value else "false"


def _heron(section: object, environ: Mapping[str, str]) -> dict[str, str]:
    body = _mapping("attachment.heron", section)
    _reject_unknown("attachment.heron", body, _HERON_KEYS)
    projected = _heron_paths(body, environ)
    projected.update(_heron_scalars(body, environ))
    return projected


def _attachment(section: object, environ: Mapping[str, str]) -> dict[str, str]:
    body = _mapping("attachment", section)
    _reject_unknown("attachment", body, _ATTACHMENT_KEYS)
    numbers = _attachment_ints(body)
    _check_vision_budget(numbers)
    projected = {env_key: str(numbers[name]) for name, env_key in _ATTACHMENT_INTS}
    if "heron" in body:
        projected.update(_heron(body["heron"], environ))
    return projected


__all__ = [
    "SETTINGS_FILE_ENV",
    "OperatorSettingsConfigError",
    "apply_operator_settings",
    "format_config_show",
    "project_operator_settings",
    "projected_additions",
    "resolve_settings_path",
    "shipped_attachment_bounds",
]
