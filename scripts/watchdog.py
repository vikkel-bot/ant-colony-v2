"""
Standalone ANT COLONY watchdog service.

Runs as an NSSM Windows service named AntWatchdog. Every 60 seconds it checks
the dashboard status endpoint and restarts the AntColony NSSM service when the
endpoint is unreachable or the reported colony tick is stale.

No third-party dependencies: stdlib only.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STATUS_URL = "http://127.0.0.1:8000/api/status"
CHECK_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10
STALE_TICK_SECONDS = 300
NSSM_EXE = r"C:\Trading\nssm.exe"
COLONY_SERVICE_NAME = "AntColony"
LOG_PATH = Path(r"C:\Trading\ANT_LOGS\watchdog.log")


logger = logging.getLogger("ant_watchdog")


def setup_logging(log_path: Path = LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def fetch_status(
    url: str = STATUS_URL,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "AntWatchdog/1.0"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local health endpoint
        status_code = getattr(response, "status", response.getcode())
        if status_code != 200:
            raise RuntimeError(f"unexpected HTTP status {status_code}")
        raw = response.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("status response is not a JSON object")
    return data


def _tick_age_seconds(status_payload: dict[str, Any]) -> float | None:
    for key in ("seconds_ago", "tick_age_seconds", "colony_tick_seconds"):
        value = status_payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def evaluate_status(status_payload: dict[str, Any]) -> tuple[bool, str]:
    tick_age = _tick_age_seconds(status_payload)
    if tick_age is None:
        return False, "tick_age_missing"
    if tick_age > STALE_TICK_SECONDS:
        return False, f"tick_age={tick_age:.1f}s>{STALE_TICK_SECONDS}s"
    status = status_payload.get("status") or "UNKNOWN"
    colony_status = status_payload.get("colony_status") or "UNKNOWN"
    return True, f"status={status} colony_status={colony_status} tick_age={tick_age:.1f}s"


def check_colony(
    url: str = STATUS_URL,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    try:
        status_payload = fetch_status(url=url, timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
        return False, f"status_request_failed:{type(exc).__name__}:{exc}"
    except Exception as exc:  # defensive: service must keep running
        return False, f"status_request_failed:{type(exc).__name__}:{exc}"
    return evaluate_status(status_payload)


def restart_colony(reason: str) -> bool:
    command = [NSSM_EXE, "restart", COLONY_SERVICE_NAME]
    logger.warning("AntColony restart aangevraagd | reason=%s", reason)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        logger.error("AntColony restart mislukt | reason=%s error=%s", reason, exc)
        return False

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    logger.info(
        "AntColony restart uitgevoerd | returncode=%s stdout=%s stderr=%s",
        result.returncode,
        stdout,
        stderr,
    )
    return result.returncode == 0


def run_forever() -> None:
    setup_logging()
    logger.info(
        "AntWatchdog gestart | url=%s interval=%ss stale_after=%ss",
        STATUS_URL,
        CHECK_INTERVAL_SECONDS,
        STALE_TICK_SECONDS,
    )
    while True:
        healthy, reason = check_colony()
        if healthy:
            logger.info("Colony OK | %s", reason)
        else:
            restart_colony(reason)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
