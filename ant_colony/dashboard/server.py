"""
ant_colony/dashboard/server.py

Dashboard server — FastAPI app factory voor het Colony Dashboard.

Maakt een FastAPI applicatie die:
  - De REST API router uit api.py monteert op /api/*
  - Statische bestanden serveert vanuit ant_colony/dashboard/static/
  - index.html serveert op GET /

Draait op 0.0.0.0:8000 zodat PC1 het dashboard kan bereiken via het
IP-adres van PC2 op het lokale netwerk.

Gebruik:
  # Vanuit colony startup code (PC2):
  from ant_colony.dashboard.server import create_app, run
  from ant_colony.dashboard.api import ColonyContext

  context = ColonyContext(queen=queen, scheduler=scheduler, logs_root=logs_root)
  app = create_app(context)
  run(context)          # blocking — start uvicorn

  # Of als apart process:
  # python -m ant_colony.dashboard.server

Regels:
  - Crash van het dashboard stopt de colony niet
  - Geen authenticatie — lokaal netwerk only
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import logging
import socket
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ant_colony.dashboard.api import ColonyContext, create_router

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(ctx: ColonyContext | None = None) -> FastAPI:
    """
    Maak de FastAPI applicatie.

    Args:
        ctx: ColonyContext met verwijzingen naar Queen en Scheduler.
             None = lege context (alle endpoints retourneren nul/lege data).

    Returns:
        Geconfigureerde FastAPI app — klaar voor uvicorn.
    """
    if ctx is None:
        ctx = ColonyContext()
    started_monotonic = time.monotonic()

    app = FastAPI(
        title="ANT COLONY v2 Dashboard",
        description="Queen dashboard — read-only colony monitor",
        version="2.0.0",
        docs_url="/docs",
        redoc_url=None,
    )

    # --- API router ---
    router = create_router(ctx)
    app.include_router(router)

    # --- Statische bestanden (CSS, JS als ze later worden toegevoegd) ---
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- Root: serveer index.html ---
    @app.get("/", include_in_schema=False)
    def serve_index() -> FileResponse:
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            from fastapi.responses import HTMLResponse
            return HTMLResponse(
                "<h1>ANT COLONY v2</h1><p>index.html not found in static/</p>",
                status_code=503,
            )
        return FileResponse(str(index))

    @app.get("/journal", include_in_schema=False)
    def serve_journal() -> FileResponse:
        journal = _STATIC_DIR / "journal.html"
        if not journal.exists():
            from fastapi.responses import HTMLResponse
            return HTMLResponse(
                "<h1>ANT COLONY v2</h1><p>journal.html not found in static/</p>",
                status_code=503,
            )
        return FileResponse(str(journal))

    # --- Health check (voor process monitors) ---
    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        agents = 0
        scheduler = getattr(ctx, "scheduler", None)
        if scheduler is not None:
            try:
                records = getattr(scheduler, "_agents", {})
                agents = sum(
                    1
                    for record in records.values()
                    if str(getattr(getattr(record, "status", ""), "value", "")).lower()
                    == "running"
                )
            except Exception:
                agents = 0
        return {
            "status": "ok",
            "uptime_seconds": round(time.monotonic() - started_monotonic, 3),
            "agents": agents,
        }

    logger.info("Dashboard app created — static dir: %s", _STATIC_DIR)
    return app


# ---------------------------------------------------------------------------
# Run helper
# ---------------------------------------------------------------------------

_SOCKET_RETRY_DELAY_S = 5


class DashboardBindError(RuntimeError):
    """Dashboardpoort is al bezet; startup moet fail-closed stoppen."""


def _connect_host_for_bind_check(host: str) -> str:
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _port_has_listener(host: str, port: int) -> bool:
    """Snelle lokale check; doet geen externe netwerkcall."""
    try:
        with socket.create_connection((_connect_host_for_bind_check(host), port), timeout=0.2):
            return True
    except OSError:
        return False


def _is_bind_error(exc: OSError) -> bool:
    text = str(exc).lower()
    return "address already in use" in text or "10048" in text or "adres" in text and "gebruik" in text


def run(
    ctx: ColonyContext | None = None,
    host: str = "0.0.0.0",
    port: int = 8000,
    log_level: str = "warning",
    reload: bool = False,
) -> None:
    """
    Start de dashboard server met uvicorn.

    Blocking call — bedoeld om in een aparte thread of process te draaien
    zodat een crash van het dashboard de colony niet raakt.

    Bij OSError (bijv. WinError 64 — Tailscale reconnect of netwerk-flip)
    wordt de server automatisch herstart na _SOCKET_RETRY_DELAY_S seconden.

    Args:
        ctx:       ColonyContext. None = lege context.
        host:      Bind adres. "0.0.0.0" = bereikbaar vanuit het netwerk (PC1).
        port:      Poort. Standaard 8000.
        log_level: uvicorn log level. Standaard "warning" om kolonie-logs
                   niet te vervuilen.
        reload:    True = herlaad bij wijziging van api.py of index.html (dev mode).
                   Vereist dat de app als string-importpad wordt opgegeven.
    """
    import uvicorn

    logger.warning(
        "Dashboard starting on http://%s:%d — accessible from network%s",
        host, port, " (reload=ON)" if reload else "",
    )

    if _port_has_listener(host, port):
        raise DashboardBindError(f"Dashboard poort {host}:{port} is al bezet")

    if reload:
        # --reload vereist app als import-string, niet als object.
        # _standalone_app() maakt een lege ColonyContext — alleen voor dev.
        uvicorn.run(
            "ant_colony.dashboard.server:_standalone_app",
            host=host,
            port=port,
            log_level=log_level,
            reload=True,
            reload_dirs=[str(Path(__file__).parent)],
            factory=True,
        )
        return

    app = create_app(ctx)
    attempt = 0
    while True:
        attempt += 1
        try:
            uvicorn.run(app, host=host, port=port, log_level=log_level)
            return  # clean exit (bijv. SIGINT / shutdown)
        except OSError as exc:
            if _is_bind_error(exc):
                raise DashboardBindError(f"Dashboard bind faalde op {host}:{port}: {exc}") from exc
            # Windows IocpProactor accept-loop breekt bij transiente netwerkfouten
            # (bijv. WinError 64 — netwerknaam niet beschikbaar).  Retry in plaats
            # van crash zodat de colony het dashboard niet verliest na Tailscale flip.
            logger.warning(
                "Dashboard socket fout (poging %d) — herstart over %ds: %s",
                attempt, _SOCKET_RETRY_DELAY_S, exc,
            )
            time.sleep(_SOCKET_RETRY_DELAY_S)


def _standalone_app() -> "FastAPI":
    """Factory voor uvicorn --reload mode (lege ColonyContext)."""
    return create_app()


# ---------------------------------------------------------------------------
# __main__ — voor standalone start op PC2
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path as _Path

    logging.basicConfig(level=logging.INFO)

    # Standalone start: geen echte Queen/Scheduler beschikbaar.
    # Start altijd met --reload zodat wijzigingen in api.py en index.html
    # direct zichtbaar zijn zonder handmatige herstart.
    logger.info("Starting dashboard in standalone mode (reload=ON)")

    logs_root_env = None
    if len(sys.argv) > 1:
        logs_root_env = _Path(sys.argv[1])
        logger.info("Logs root: %s", logs_root_env)

    run(reload=True)
