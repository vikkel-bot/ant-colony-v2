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

    # --- Health check (voor process monitors) ---
    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"ok": True}

    logger.info("Dashboard app created — static dir: %s", _STATIC_DIR)
    return app


# ---------------------------------------------------------------------------
# Run helper
# ---------------------------------------------------------------------------

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
    else:
        app = create_app(ctx)
        uvicorn.run(app, host=host, port=port, log_level=log_level)


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
