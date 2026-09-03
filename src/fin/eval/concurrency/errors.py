"""Named T23 driver failure types (except report persistence)."""

from __future__ import annotations


class ConcurrencyRunError(Exception):
    """Base class for named T23 driver failures."""

    code = "concurrency_run_error"


class InvalidConcurrencyInputError(ConcurrencyRunError):
    """A driver received a non-positive duration or request cap."""

    code = "concurrency_input_invalid"


class BlockCountUnavailableError(ConcurrencyRunError):
    """The benchmark could not obtain an integer block count."""

    code = "block_count_unavailable"


class L2CredentialsMissingError(ConcurrencyRunError):
    """The opt-in real LLM lane has no explicitly configured credential."""

    code = "l2_credentials_missing"


class L2BackendSetupError(ConcurrencyRunError):
    """The opt-in real LLM graph could not be constructed."""

    code = "l2_backend_setup_failed"
