"""Check that normal fin settings resolve a checkpoint URL without displaying it."""

from __future__ import annotations

import os

from fin.host.operator_settings import apply_operator_settings

EXIT_CONFIGURED = 0
EXIT_NOT_CONFIGURED = 1


def main() -> int:
    settings = dict(os.environ)
    apply_operator_settings(environ=settings)
    if (settings.get("FIN_CHECKPOINT_URL") or "").strip():
        return EXIT_CONFIGURED
    return EXIT_NOT_CONFIGURED


if __name__ == "__main__":
    raise SystemExit(main())
