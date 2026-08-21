"""Entry point for ``python -m fin`` (T7).

Thin shim: delegates to ``fin.host.cli.main`` and exits with its
return code. All logic — argparse, prod-graph resolution, exit-code
mapping — lives in ``fin.host.cli``. This module exists only to
satisfy Python's ``-m`` execution semantics for the ``fin`` package.
"""

from __future__ import annotations

import sys

from fin.host.cli import main


if __name__ == "__main__":
    sys.exit(main())
