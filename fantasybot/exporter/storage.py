"""Validated atomic storage for the current export."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .security import validate_safe


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = PROJECT_ROOT / "exports" / "fantasy-hoy.json"


def write_export(data: dict[str, Any], output_path: Path = OUTPUT_PATH) -> None:
    """Validate again and atomically write a private JSON file."""
    validate_safe(data)
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Check the final serialisable representation too, before touching the output.
    validate_safe(json.loads(encoded))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=".fantasy-hoy-",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(encoded)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, output_path)
        temp_name = None
    finally:
        os.umask(old_umask)
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
