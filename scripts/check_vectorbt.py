#!/usr/bin/env python
"""
Detecteer lokale VectorBT prerequisites.

Read-only: voert geen installatie uit en wijzigt geen configuratie.
"""

from __future__ import annotations

import importlib.util


def _available(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


def main() -> int:
    checks = {
        "vectorbt": _available("vectorbt"),
        "pandas": _available("pandas"),
        "numpy": _available("numpy"),
    }

    print("VectorBT installatie check")
    print("==========================")
    for package, ok in checks.items():
        print(f"{package:<10}: {'OK' if ok else 'ONTBREEKT'}")
    print("")

    missing = [package for package, ok in checks.items() if not ok]
    if missing:
        print("Status: VectorBT validator is optioneel en momenteel niet volledig beschikbaar.")
        print("Ontbreekt: " + ", ".join(missing))
        print("Installatie op PC1: pip install vectorbt")
    else:
        print("Status: VectorBT prerequisites lijken beschikbaar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
