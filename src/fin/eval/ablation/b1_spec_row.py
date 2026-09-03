"""Read a single §B1 table row out of the published spec."""

from __future__ import annotations

from pathlib import Path


def _spec_row(label: str, spec_path: Path | None = None) -> str:
    """Return the second cell of the §B1 table row named ``label``."""
    from fin.eval.ablation.matrix import (
        AblationSpecRowMissingError,
        published_spec_path,
    )

    path = Path(spec_path) if spec_path is not None else published_spec_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AblationSpecRowMissingError(
            f"cannot read the published §B1 row from {path}: {exc}"
        ) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == label:
            return "|".join(cells[1:])
    raise AblationSpecRowMissingError(
        f"{path} publishes no 「{label}」 §B1 table row; the ablation matrix "
        "has nothing to take its numbers from"
    )
