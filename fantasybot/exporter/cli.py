"""Command-line entry point for the safe read-only exporter."""

from fantasybot.api import FantasyError

from .builder import build_export
from .client import ReadOnlyFantasyClient
from .security import UnsafeExportError
from .storage import OUTPUT_PATH, write_export


def main() -> int:
    try:
        data = build_export(ReadOnlyFantasyClient())
        write_export(data)
    except (FantasyError, UnsafeExportError, OSError, ValueError):
        # Do not echo remote response bodies or authentication details on failure.
        print(
            "[ERROR] No se pudo crear la exportación segura. "
            "Comprueba la sesión y la conectividad; no se ha modificado la cuenta."
        )
        return 1
    print(f"[OK] Exportación de solo lectura: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
