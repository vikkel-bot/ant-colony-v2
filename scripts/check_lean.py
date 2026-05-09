#!/usr/bin/env python
"""
Detecteer lokale QuantConnect Lean prerequisites.

Read-only: voert geen installatie uit en wijzigt geen configuratie.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def _run(command: list[str], timeout: int = 10) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, output
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    lean_path = shutil.which("lean")
    docker_path = shutil.which("docker")

    lean_cli_ok = lean_path is not None
    docker_ok = False
    docker_output = "docker niet gevonden"
    if docker_path:
        docker_ok, docker_output = _run(["docker", "--version"])

    package_ok, package_output = _run([sys.executable, "-m", "pip", "show", "lean"])

    print("Lean installatie check")
    print("======================")
    print(f"lean CLI      : {'OK' if lean_cli_ok else 'ONTBREEKT'}")
    print(f"lean path     : {lean_path or '-'}")
    print(f"Docker        : {'OK' if docker_ok else 'ONTBREEKT'}")
    print(f"Docker info   : {docker_output or '-'}")
    print(f"Python package: {'OK' if package_ok else 'ONTBREEKT'}")
    print(f"Package info  : {package_output.splitlines()[0] if package_output else '-'}")
    print("")

    missing = []
    if not lean_cli_ok:
        missing.append("lean CLI")
    if not docker_ok:
        missing.append("Docker")
    if not package_ok:
        missing.append("Python package 'lean'")

    if missing:
        print("Status: Lean validator is optioneel en momenteel niet volledig beschikbaar.")
        print("Ontbreekt: " + ", ".join(missing))
    else:
        print("Status: Lean prerequisites lijken beschikbaar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
