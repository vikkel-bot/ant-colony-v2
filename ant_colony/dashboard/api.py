"""
ant_colony/dashboard/api.py

Dashboard REST API — leest colony state en serveert JSON aan de frontend.

Endpoints:
  GET  /api/status      — colony status + laatste tick timestamp
  GET  /api/metrics     — kapitaal totalen + actieve agents
  GET  /api/performance — PnL dag/week/maand/jaar/alltime (uit trade logs)
  GET  /api/biomes      — allocatie per biome
  GET  /api/ants        — actieve agents met TTL countdown
  GET  /api/brokers     — broker connecties en ingezet kapitaal (+ data-leeftijd)
  GET  /api/capital     — totaal/beschikbaar kapitaal per broker, inclusief holdings
  GET  /api/positions   — open paper posities met live PnL + SL/TP progress
  GET  /api/ants/{ant_type}/events — laatste N events per ant-type (uitklap)
  GET  /api/ticker      — laatste 20 audit log events
  POST /api/missions    — geef een mission uit via de echte colony Queen
  POST /api/killswitch  — level 1/2/3 + scope, vereist operator_confirm=true

Regels:
  - Dashboard leest — schrijft nooit (behalve missions/killswitch via Queen)
  - Alle endpoints gooien nooit — retourneren altijd geldig JSON
  - Ticker leest JSONL append-only logs van disk (nooit schrijven)
  - Geen code wordt uitgevoerd bij import (P7)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.queen.queen import Queen
from ant_colony.schemas.mission import Mission

# Referentie-markt per biome voor live prijsweergave in het dashboard
_BIOME_REFERENCE_MARKET: dict[str, str] = {
    "crypto": "BTC-EUR",
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Colony context — geïnjecteerd bij startup door server.py
# ---------------------------------------------------------------------------

@dataclass
class ColonyContext:
    """
    Houdt verwijzingen naar alle levende colony-objecten.

    Wordt één keer aangemaakt door server.py en gedeeld door alle endpoints.
    Alle velden zijn optioneel zodat het dashboard ook zonder volledige colony
    kan starten (bijv. tijdens tests of vroege bootstrap).

    queen:           Queen-instantie — kapitaal, missions, kill-switch.
    scheduler:       ColonyScheduler — status, tick timestamp.
    logs_root:       Root van ANT_LOGS — voor ticker en performance data.
    broker_names:    Mapping biome_id → leesbare naam (bijv. "crypto" → "Bitvavo").
    biome_registry:  BiomeRegistry met live adapters voor echte balans/posities.
    """
    queen: Queen | None = None
    scheduler: ColonyScheduler | None = None
    logs_root: Path | None = None
    broker_names: dict[str, str] = field(default_factory=dict)
    biome_registry: BiomeRegistry | None = None
    paper_ledgers: list = field(default_factory=list)  # list[PaperLedger] — optioneel in-memory


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    status: str
    last_tick: str | None
    seconds_ago: float | None
    server_time: str


class MetricsResponse(BaseModel):
    capital_total: float
    capital_allocated: float
    capital_available: float
    capital_in_use: float | None = None   # virtueel: entry_price × qty open paper posities
    active_ants: int
    utilization_pct: float
    live_eur_balance: float | None = None   # echt EUR saldo bij broker (bijv. Bitvavo)
    live_eur_source:  str   | None = None   # naam van de broker


class PerformanceResponse(BaseModel):
    day: float
    week: float
    month: float
    year: float
    alltime: float


class PositionEntry(BaseModel):
    symbol: str
    quantity: float
    current_price: float
    market_value: float


class BiomeEntry(BaseModel):
    biome_id: str
    display_name: str
    limit: float
    allocated: float
    available: float
    fraction: float             # allocated / limit, 0–1
    positions: list[PositionEntry] = []
    reference_price: float | None = None   # bijv. BTC-EUR slotkoers


class BiomesResponse(BaseModel):
    biomes: list[BiomeEntry]


class AntEntry(BaseModel):
    ant_id: str
    ant_type: str
    mission_id: str
    node_id: str
    biome: str
    capital_limit: float
    ttl_remaining: int | None   # seconden; None als onbekend
    is_live: bool               # True als ant_type == "execution_ant"


class AntsResponse(BaseModel):
    ants: list[AntEntry]


class BrokerEntry(BaseModel):
    name: str
    biome_id: str
    status: str                              # "connected" | "disconnected" | "standby"
    capital_deployed: float
    balance_available: float | None = None   # vrij beschikbaar saldo bij exchange
    balance_in_orders: float | None = None   # vergrendeld in open orders
    paper_mode: bool | None = None           # True = paper trading, False = live
    last_updated: str | None = None          # ISO timestamp laatste succesvolle API-call
    data_age_seconds: float | None = None    # seconden geleden; 0 = zojuist opgehaald


class BrokersResponse(BaseModel):
    brokers: list[BrokerEntry]
    fetched_at: str = ""                     # ISO timestamp waarop de response is gebouwd


class CapitalBrokerEntry(BaseModel):
    name: str
    biome_id: str
    status: str
    total_eur: float               # balance + marktwaarde holdings
    available_eur: float           # vrij EUR saldo bij exchange
    holdings_eur: float            # EUR-waarde van openstaande posities
    last_updated: str | None = None
    data_age_seconds: float | None = None


class CapitalSummaryResponse(BaseModel):
    total_eur: float               # som over alle brokers
    available_eur: float           # som vrij EUR over alle brokers
    holdings_eur: float            # som holdings-waarde over alle brokers
    brokers: list[CapitalBrokerEntry]
    fetched_at: str                # ISO timestamp van deze response


class OpenPositionEntry(BaseModel):
    position_id: str
    symbol: str
    biome: str
    side: str                              # "long" | "short"
    entry_price: float
    current_price: float | None = None     # None als live prijs niet beschikbaar
    quantity: float
    stop_loss_price: float
    take_profit_price: float
    pnl_eur: float | None = None           # None als current_price ontbreekt
    pnl_pct: float | None = None           # None als current_price ontbreekt
    opened_at: str                         # ISO timestamp
    age_seconds: float
    strategy_type: str | None = None
    sl_tp_progress: float | None = None    # 0.0=bij SL, 1.0=bij TP; None als geen prijs


class OpenPositionsResponse(BaseModel):
    positions: list[OpenPositionEntry]
    total_pnl_eur: float | None = None
    total_pnl_pct: float | None = None
    count: int
    fetched_at: str


class AntEventEntry(BaseModel):
    timestamp: str
    action: str
    summary: str
    payload: dict[str, Any]


class AntEventsResponse(BaseModel):
    ant_type: str
    events: list[AntEventEntry]
    count: int
    has_more: bool


class TickerEvent(BaseModel):
    event_type: str
    source: str
    timestamp: str
    mission_id: str | None
    payload: dict[str, Any]


class TickerResponse(BaseModel):
    events: list[TickerEvent]


class MissionResponse(BaseModel):
    accepted: bool
    mission_id: str
    rejection_reason: str | None = None
    rejection_detail: str = ""


class OperatorInputRequest(BaseModel):
    type: str                   # "url", "text", "code", "image"
    content: str = ""
    image_data: str | None = None   # base64 encoded image (only for type="image")
    media_type: str = "image/jpeg"  # MIME type of image


class OperatorInputResponse(BaseModel):
    accepted: bool
    filename: str | None = None
    message: str = ""


class KillSwitchRequest(BaseModel):
    level: int              # 1 = agent, 2 = node, 3 = colony
    scope: str | None = None
    operator_confirm: bool


class KillSwitchResponse(BaseModel):
    executed: bool
    level: int
    scope: str | None
    message: str


class ChartPoint(BaseModel):
    time: str
    pnl: float


class PerformanceChartResponse(BaseModel):
    points: list[ChartPoint]
    total_pnl: float
    curve_label: str = "vandaag"   # "vandaag" | "alltime"
    dag: float = 0.0
    week: float = 0.0
    maand: float = 0.0
    jaar: float = 0.0
    alltime: float = 0.0


class QueenStrategyEntry(BaseModel):
    strategy_type: str
    symbol: str
    sharpe: float
    best_regime: str


class QueenStatusResponse(BaseModel):
    regime: str | None
    top_strategies: list[QueenStrategyEntry]
    last_decision: dict[str, Any] | None


class QueenDecisionEntry(BaseModel):
    timestamp:     str
    decision_type: str
    mission_id:    str | None
    title:         str
    summary:       str
    payload:       dict[str, Any]


class QueenDecisionsResponse(BaseModel):
    decisions: list[QueenDecisionEntry]
    count:     int
    has_more:  bool


class ActivityFeedEntry(BaseModel):
    timestamp: str
    ant_type:  str
    action:    str
    summary:   str
    level:     str   # "info" | "warning" | "error"


class ActivityFeedResponse(BaseModel):
    events:     list[ActivityFeedEntry]
    count:      int
    filter:     str
    fetched_at: str


class AntStatsEntry(BaseModel):
    signals_found:              int   | None = None
    candidates_above_threshold: int   | None = None
    open_trades:                int   | None = None
    total_pnl:                  float | None = None
    win_rate:                   float | None = None
    warnings_today:             int   | None = None
    anomalies_today:            int   | None = None
    repos_ingested:             int   | None = None
    variants_generated:         int   | None = None
    orders_placed:              int   | None = None
    orders_rejected:            int   | None = None
    inputs_processed:           int   | None = None


class AntActivityEntry(BaseModel):
    ant_type:      str
    last_seen:     str | None
    summary:       str
    recent_events: list[str]
    stats:         AntStatsEntry


class AntActivityResponse(BaseModel):
    ants: list[AntActivityEntry]


class EquitiesSectorEntry(BaseModel):
    symbol: str
    sector: str
    return_3mo: float
    rank: int
    signal: str            # "LONG" | "NEUTRAL"
    emitted_at: str | None = None


class EquitiesPiotroskiEntry(BaseModel):
    symbol: str
    f_score: int
    evaluated_at: str | None = None


class EquitiesBreakoutEntry(BaseModel):
    symbol: str
    entry_price: float
    sl_price: float
    tp_price: float
    distance_to_high: float
    emitted_at: str | None = None


class EquitiesDividendEntry(BaseModel):
    symbol: str
    dividend_yield: float
    consecutive_years: int
    payout_ratio: float
    emitted_at: str | None = None


class EquitiesStatusResponse(BaseModel):
    enabled: bool
    sector_long: list[EquitiesSectorEntry]      # top 3 LONG sectoren
    sector_neutral: list[EquitiesSectorEntry]   # bottom 3 NEUTRAL
    piotroski_candidates: list[EquitiesPiotroskiEntry]
    breakout_signals: list[EquitiesBreakoutEntry]
    dividend_candidates: list[EquitiesDividendEntry]
    vix_level: float | None = None
    vix_signal: str | None = None               # "HEDGE" | "NORMAL" | None
    last_sector_ts: str | None = None
    last_piotroski_ts: str | None = None
    last_breakout_ts: str | None = None
    last_dividend_ts: str | None = None


_BITVAVO_TICKER = "https://api.bitvavo.com/v2/{market}/ticker/price"


def _get_live_price(market: str, registry=None) -> float | None:
    """
    Haal live sluitingsprijs op voor market.

    Volgorde:
      1. BiomeRegistry crypto-adapter → get_market_data(market, "1m").close
      2. Bitvavo public REST API (geen auth) als fallback
    """
    if registry is not None:
        try:
            adapter = registry.get("crypto")
            if adapter is not None:
                md = adapter.get_market_data(market, "1m")
                if md is not None and md.close > 0:
                    return md.close
        except Exception:
            pass

    try:
        url = _BITVAVO_TICKER.format(market=market)
        req = urllib.request.Request(url, headers={"User-Agent": "ant-colony-dashboard/2"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        price = data.get("price")
        return float(price) if price is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_router(ctx: ColonyContext) -> APIRouter:
    """
    Maak een APIRouter met alle dashboard endpoints.

    Args:
        ctx: ColonyContext met verwijzingen naar Queen en Scheduler.

    Returns:
        Geconfigureerde APIRouter — te mounten in de FastAPI app.
    """
    router = APIRouter(prefix="/api")

    # ------------------------------------------------------------------
    # GET /api/status
    # ------------------------------------------------------------------

    @router.get("/status", response_model=StatusResponse)
    def get_status() -> StatusResponse:
        """Colony status en laatste tick timestamp."""
        now = datetime.now(tz=timezone.utc)

        if ctx.scheduler is None:
            return StatusResponse(
                status="UNKNOWN",
                last_tick=None,
                seconds_ago=None,
                server_time=_to_local_str(now),
            )

        status = ctx.scheduler.status.value.upper()
        last_tick = _last_tick_from_logs(ctx.logs_root)

        seconds_ago: float | None = None
        last_tick_str: str | None = None
        if last_tick is not None:
            seconds_ago = round((now - last_tick).total_seconds(), 1)
            last_tick_str = _to_local_str(last_tick)

        return StatusResponse(
            status=status,
            last_tick=last_tick_str,
            seconds_ago=seconds_ago,
            server_time=_to_local_str(now),
        )

    # ------------------------------------------------------------------
    # GET /api/metrics
    # ------------------------------------------------------------------

    @router.get("/metrics", response_model=MetricsResponse)
    def get_metrics() -> MetricsResponse:
        """Kapitaal totalen en actief agent-aantal."""
        if ctx.queen is None:
            return MetricsResponse(
                capital_total=0.0,
                capital_allocated=0.0,
                capital_available=0.0,
                active_ants=0,
                utilization_pct=0.0,
            )

        # Reëel totaal = live broker-saldi + marktwaarde holdings.
        # Valt terug op queen.capital_total als adapters niet beschikbaar zijn.
        live_total = _real_equity(ctx.biome_registry)
        total = live_total if live_total is not None else ctx.queen.capital_total

        allocated = ctx.queen.capital_allocated
        available = max(0.0, total - allocated)
        active    = len(ctx.queen.active_missions)
        util      = round(allocated / total * 100.0, 1) if total > 0 else 0.0

        # Virtuele inzet: entry_price × quantity voor openstaande paper posities
        in_use = _read_paper_capital_in_use(ctx.logs_root) if ctx.logs_root else None

        # Echt EUR saldo: rechtstreeks van de crypto-adapter (Bitvavo)
        live_eur_balance: float | None = None
        live_eur_source:  str   | None = None
        if ctx.biome_registry is not None:
            _crypto = ctx.biome_registry.get("crypto")
            if _crypto is not None:
                try:
                    _account = _crypto.get_account_state()
                    if _account is not None:
                        live_eur_balance = _account.balance
                        live_eur_source  = ctx.broker_names.get("crypto", "Bitvavo")
                except Exception:
                    logger.exception("/api/metrics: live EUR saldo ophalen mislukt")

        return MetricsResponse(
            capital_total=total,
            capital_allocated=allocated,
            capital_available=available,
            capital_in_use=in_use,
            active_ants=active,
            utilization_pct=util,
            live_eur_balance=live_eur_balance,
            live_eur_source=live_eur_source,
        )

    # ------------------------------------------------------------------
    # GET /api/performance
    # ------------------------------------------------------------------

    @router.get("/performance", response_model=PerformanceResponse)
    def get_performance() -> PerformanceResponse:
        """PnL dag/week/maand/jaar/alltime gelezen uit paper trade logs."""
        if ctx.logs_root is None:
            return PerformanceResponse(day=0.0, week=0.0, month=0.0, year=0.0, alltime=0.0)

        now = datetime.now(tz=timezone.utc)
        trades = _read_all_trades(ctx.logs_root)

        def pnl_since(cutoff: datetime) -> float:
            return round(sum(
                t.get("realized_pnl") or 0.0
                for t in trades
                if _parse_ts(t.get("closed_at")) is not None
                and _parse_ts(t.get("closed_at")) >= cutoff
            ), 2)

        return PerformanceResponse(
            day=pnl_since(now - timedelta(days=1)),
            week=pnl_since(now - timedelta(weeks=1)),
            month=pnl_since(now - timedelta(days=30)),
            year=pnl_since(now - timedelta(days=365)),
            alltime=round(sum(t.get("realized_pnl") or 0.0 for t in trades), 2),
        )

    # ------------------------------------------------------------------
    # GET /api/biomes
    # ------------------------------------------------------------------

    @router.get("/biomes", response_model=BiomesResponse)
    def get_biomes() -> BiomesResponse:
        """Allocatie per biome, aangevuld met live posities en referentieprijs."""
        if ctx.queen is None:
            return BiomesResponse(biomes=[])

        snapshot = ctx.queen.allocation_snapshot()
        _DISPLAY = {"crypto": "Crypto", "equities": "Equities", "commodities": "Commodities"}

        entries = []
        for b in snapshot.biomes:
            fraction = round(b.allocated / b.limit, 4) if b.limit and b.limit > 0 else 0.0

            positions: list[PositionEntry] = []
            reference_price: float | None = None

            if ctx.biome_registry is not None:
                adapter = ctx.biome_registry.get(b.biome_id)
                if adapter is not None:
                    try:
                        live_positions = adapter.get_positions()
                        if live_positions:
                            positions = [
                                PositionEntry(
                                    symbol=p.symbol,
                                    quantity=p.quantity,
                                    current_price=p.current_price,
                                    market_value=p.market_value,
                                )
                                for p in live_positions
                            ]
                    except Exception:
                        logger.exception("/api/biomes: get_positions() mislukt voor %s", b.biome_id)

                    try:
                        ref_market = _BIOME_REFERENCE_MARKET.get(b.biome_id)
                        if ref_market:
                            md = adapter.get_market_data(ref_market, "1m")
                            if md and md.is_valid_price:
                                reference_price = md.close
                    except Exception:
                        logger.exception("/api/biomes: get_market_data() mislukt voor %s", b.biome_id)

            entries.append(BiomeEntry(
                biome_id=b.biome_id,
                display_name=_DISPLAY.get(b.biome_id, b.biome_id.capitalize()),
                limit=b.limit or 0.0,
                allocated=b.allocated or 0.0,
                available=b.available or 0.0,
                fraction=fraction,
                positions=positions,
                reference_price=reference_price,
            ))

        return BiomesResponse(biomes=entries)

    # ------------------------------------------------------------------
    # GET /api/ants
    # ------------------------------------------------------------------

    @router.get("/ants", response_model=AntsResponse)
    def get_ants() -> AntsResponse:
        """Actieve agents met TTL countdown."""
        if ctx.queen is None:
            return AntsResponse(ants=[])

        now = datetime.now(tz=timezone.utc)
        ants = []
        for mission in ctx.queen.active_missions.values():
            issued_at = mission.issued_at
            ttl_remaining: int | None = None
            if issued_at is not None:
                elapsed = int((now - issued_at).total_seconds())
                ttl_remaining = max(0, mission.ttl - elapsed)

            ants.append(AntEntry(
                ant_id=f"{mission.ant_type}_{mission.mission_id[:8]}",
                ant_type=mission.ant_type,
                mission_id=mission.mission_id,
                node_id=mission.allowed_node,
                biome=mission.market_scope.biome,
                capital_limit=mission.capital_limit,
                ttl_remaining=ttl_remaining,
                is_live=(mission.ant_type == "execution_ant"),
            ))

        return AntsResponse(ants=ants)

    # ------------------------------------------------------------------
    # GET /api/brokers
    # ------------------------------------------------------------------

    @router.get("/brokers", response_model=BrokersResponse)
    def get_brokers() -> BrokersResponse:
        """Broker connecties, live saldo en ingezet kapitaal per biome."""
        import os

        fetched_at = datetime.now(tz=timezone.utc).isoformat()

        if ctx.queen is None:
            return BrokersResponse(brokers=[], fetched_at=fetched_at)

        equities_enabled = os.getenv("EQUITIES_ENABLED", "false").lower() == "true"

        snapshot = ctx.queen.allocation_snapshot()
        _DEFAULT_BROKERS = {
            "crypto": "Bitvavo",
            "equities": "Interactive Brokers",
            "commodities": "Saxo Bank",
        }

        brokers = []
        seen_biomes: set[str] = set()

        for b in snapshot.biomes:
            name = ctx.broker_names.get(b.biome_id) or _DEFAULT_BROKERS.get(b.biome_id, b.biome_id)

            # Standaard: status volgt kapitaal-inzet als er geen adapter beschikbaar is.
            # Met adapter overschrijft de werkelijke connectiviteit dit oordeel.
            deployed = b.allocated or 0.0
            status = "connected" if deployed > 0 else "standby"
            balance_available: float | None = None
            balance_in_orders: float | None = None
            paper_mode: bool | None = None
            last_updated: str | None = None
            data_age_seconds: float | None = None

            if ctx.biome_registry is not None:
                adapter = ctx.biome_registry.get(b.biome_id)
                if adapter is not None:
                    try:
                        connected = adapter.is_available()
                        if connected:
                            account = adapter.get_account_state()
                            if account is not None:
                                balance_available = account.balance
                                balance_in_orders = account.positions_value
                            status = "connected"
                            last_updated = fetched_at
                            data_age_seconds = 0.0
                        else:
                            status = "disconnected"
                        pm = getattr(adapter, "_paper_mode", None)
                        if pm is not None:
                            paper_mode = bool(pm)
                    except Exception:
                        logger.exception("/api/brokers: adapter check mislukt voor %s", b.biome_id)
                        status = "disconnected"

            brokers.append(BrokerEntry(
                name=name,
                biome_id=b.biome_id,
                status=status,
                capital_deployed=b.allocated or 0.0,
                balance_available=balance_available,
                balance_in_orders=balance_in_orders,
                paper_mode=paper_mode,
                last_updated=last_updated,
                data_age_seconds=data_age_seconds,
            ))
            seen_biomes.add(b.biome_id)

        # IBKR extra entry: EQUITIES_ENABLED=true maar equities nog niet in queen snapshot
        if equities_enabled and "equities" not in seen_biomes and ctx.biome_registry is not None:
            adapter = ctx.biome_registry.get("equities")
            if adapter is not None:
                status = "disconnected"
                balance_available = None
                balance_in_orders = None
                paper_mode = bool(getattr(adapter, "_paper_mode", True))
                last_updated = None
                data_age_seconds = None
                try:
                    if adapter.is_available():
                        status = "connected"
                        account = adapter.get_account_state()
                        if account is not None:
                            balance_available = account.balance
                            balance_in_orders = account.positions_value
                        last_updated = fetched_at
                        data_age_seconds = 0.0
                except Exception:
                    logger.exception("/api/brokers: IBKR fallback check mislukt")

                brokers.append(BrokerEntry(
                    name="Interactive Brokers",
                    biome_id="equities",
                    status=status,
                    capital_deployed=0.0,
                    balance_available=balance_available,
                    balance_in_orders=balance_in_orders,
                    paper_mode=paper_mode,
                    last_updated=last_updated,
                    data_age_seconds=data_age_seconds,
                ))

        return BrokersResponse(brokers=brokers, fetched_at=fetched_at)

    # ------------------------------------------------------------------
    # GET /api/capital
    # ------------------------------------------------------------------

    @router.get("/capital", response_model=CapitalSummaryResponse)
    def get_capital() -> CapitalSummaryResponse:
        """
        Totaal en beschikbaar kapitaal, uitgesplitst per broker.

        Per broker:
          total_eur     = vrij saldo + marktwaarde open posities
          available_eur = vrij EUR saldo
          holdings_eur  = marktwaarde van crypto/equity holdings
        """
        fetched_at = datetime.now(tz=timezone.utc).isoformat()

        if ctx.biome_registry is None:
            return CapitalSummaryResponse(
                total_eur=0.0,
                available_eur=0.0,
                holdings_eur=0.0,
                brokers=[],
                fetched_at=fetched_at,
            )

        _DEFAULT_NAMES = {
            "crypto": "Bitvavo",
            "equities": "Interactive Brokers",
            "commodities": "Saxo Bank",
        }

        cap_brokers: list[CapitalBrokerEntry] = []
        grand_total    = 0.0
        grand_available = 0.0
        grand_holdings  = 0.0

        for biome_id in ctx.biome_registry.list_biomes():
            adapter = ctx.biome_registry.get(biome_id)
            if adapter is None:
                continue

            name = ctx.broker_names.get(biome_id) or _DEFAULT_NAMES.get(biome_id, biome_id)
            status = "disconnected"
            available_eur = 0.0
            holdings_eur  = 0.0
            last_updated: str | None = None
            data_age_seconds: float | None = None

            try:
                if adapter.is_available():
                    account = adapter.get_account_state()
                    if account is not None:
                        available_eur = account.balance or 0.0

                    positions = adapter.get_positions()
                    if positions:
                        holdings_eur = sum(p.market_value for p in positions)

                    status       = "connected"
                    last_updated = fetched_at
                    data_age_seconds = 0.0
            except Exception:
                logger.exception("/api/capital: fout voor biome_id=%s", biome_id)

            total_eur = available_eur + holdings_eur
            grand_total     += total_eur
            grand_available += available_eur
            grand_holdings  += holdings_eur

            cap_brokers.append(CapitalBrokerEntry(
                name=name,
                biome_id=biome_id,
                status=status,
                total_eur=round(total_eur, 2),
                available_eur=round(available_eur, 2),
                holdings_eur=round(holdings_eur, 2),
                last_updated=last_updated,
                data_age_seconds=data_age_seconds,
            ))

        return CapitalSummaryResponse(
            total_eur=round(grand_total, 2),
            available_eur=round(grand_available, 2),
            holdings_eur=round(grand_holdings, 2),
            brokers=cap_brokers,
            fetched_at=fetched_at,
        )

    # ------------------------------------------------------------------
    # GET /api/positions
    # ------------------------------------------------------------------

    @router.get("/positions", response_model=OpenPositionsResponse)
    def get_positions() -> OpenPositionsResponse:
        """
        Open paper posities met live PnL en SL/TP progress.

        Data bronnen (prioriteit):
          1. ctx.paper_ledgers — directe in-memory toegang (als geïnjecteerd)
          2. ctx.logs_root     — scan ANT_LOGS/paper/*.jsonl

        Fail-closed: als live prijs niet beschikbaar is, worden current_price,
        pnl_eur, pnl_pct en sl_tp_progress gevuld met None.
        """
        fetched_at = datetime.now(tz=timezone.utc).isoformat()
        now        = datetime.now(tz=timezone.utc)

        # --- Verzamel ruwe positiedata ---
        raw: list[dict] = []

        if ctx.paper_ledgers:
            for ledger in ctx.paper_ledgers:
                for pos in ledger.open_positions:
                    raw.append({
                        "position_id":      pos.position_id,
                        "symbol":           pos.symbol,
                        "biome":            pos.biome,
                        "side":             pos.side.value,
                        "entry_price":      pos.entry_price,
                        "quantity":         pos.quantity,
                        "stop_loss_price":  pos.stop_loss_price,
                        "take_profit_price": pos.take_profit_price,
                        "opened_at":        pos.opened_at.isoformat(),
                        "strategy_type":    None,  # niet beschikbaar via ledger
                    })
        elif ctx.logs_root is not None:
            raw = _read_open_positions_from_logs(ctx.logs_root)

        # --- Verrijk met live prijs en bereken PnL ---
        entries: list[OpenPositionEntry] = []
        total_pnl_eur = 0.0
        has_any_pnl   = False

        for r in raw:
            opened_at_str = r.get("opened_at") or ""
            opened_dt     = _parse_ts(opened_at_str)
            age_sec       = (now - opened_dt).total_seconds() if opened_dt else 0.0

            entry    = float(r.get("entry_price") or 0)
            qty      = float(r.get("quantity")    or 0)
            sl_price = float(r.get("stop_loss_price") or r.get("stop_loss") or 0)
            tp_price = float(r.get("take_profit_price") or r.get("take_profit") or 0)
            side     = str(r.get("side") or "long")
            biome_id = str(r.get("biome") or "crypto")

            # Live prijs ophalen
            current: float | None = None
            if ctx.biome_registry is not None and r.get("symbol"):
                current = _get_live_price(ctx.biome_registry, biome_id, str(r["symbol"]))

            # PnL berekenen
            pnl_eur: float | None = None
            pnl_pct: float | None = None
            if current is not None and entry > 0 and qty > 0:
                raw_pnl = (current - entry) * qty if side == "long" else (entry - current) * qty
                pnl_eur = round(raw_pnl, 4)
                pnl_pct = round(raw_pnl / (entry * qty) * 100, 4)
                total_pnl_eur += pnl_eur
                has_any_pnl = True

            # SL/TP progress
            progress = _compute_sl_tp_progress(side, current, sl_price, tp_price)

            entries.append(OpenPositionEntry(
                position_id=str(r.get("position_id") or ""),
                symbol=str(r.get("symbol") or ""),
                biome=biome_id,
                side=side,
                entry_price=entry,
                current_price=current,
                quantity=qty,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                pnl_eur=pnl_eur,
                pnl_pct=pnl_pct,
                opened_at=opened_at_str,
                age_seconds=round(age_sec, 0),
                strategy_type=r.get("strategy_type"),
                sl_tp_progress=progress,
            ))

        # Totaal PnL als percentage van ingezette waarde
        total_entry_val = sum(
            float(r.get("entry_price") or 0) * float(r.get("quantity") or 0)
            for r in raw
        )
        total_pnl_pct: float | None = None
        if has_any_pnl and total_entry_val > 0:
            total_pnl_pct = round(total_pnl_eur / total_entry_val * 100, 4)

        return OpenPositionsResponse(
            positions=entries,
            total_pnl_eur=round(total_pnl_eur, 4) if has_any_pnl else None,
            total_pnl_pct=total_pnl_pct,
            count=len(entries),
            fetched_at=fetched_at,
        )

    # ------------------------------------------------------------------
    # GET /api/ticker
    # ------------------------------------------------------------------

    @router.get("/ticker", response_model=TickerResponse)
    def get_ticker() -> TickerResponse:
        """Laatste 20 audit log events van disk."""
        if ctx.logs_root is None:
            return TickerResponse(events=[])

        events = _read_recent_events(ctx.logs_root, limit=20)
        return TickerResponse(events=events)

    # ------------------------------------------------------------------
    # POST /api/missions
    # ------------------------------------------------------------------

    @router.post("/missions", response_model=MissionResponse)
    def post_mission(mission: Mission) -> MissionResponse:
        """
        Geef een mission uit via de echte colony Queen.

        De Mission wordt volledig gevalideerd door Pydantic (422 bij ongeldig
        schema) en daarna beoordeeld door de Queen (kapitaal, node, biome).

        Response:
          accepted=True  → mission actief, verschijnt direct op het dashboard.
          accepted=False → zakelijke afwijzing; rejection_reason bevat de reden.
          503            → colony niet geïnitialiseerd.
        """
        if ctx.queen is None:
            raise HTTPException(
                status_code=503,
                detail="colony not initialised — queen unavailable",
            )

        try:
            result = ctx.queen.issue_mission(mission)
        except Exception:
            logger.exception("POST /api/missions: onverwachte fout bij uitgifte %s", mission.mission_id)
            raise HTTPException(status_code=500, detail="mission issuance failed unexpectedly")

        if result.accepted:
            logger.info("POST /api/missions: accepted mission_id=%s ant_type=%s", mission.mission_id, mission.ant_type)
        else:
            logger.warning(
                "POST /api/missions: rejected mission_id=%s reason=%s detail=%s",
                mission.mission_id,
                result.rejection_reason,
                result.rejection_detail,
            )

        return MissionResponse(
            accepted=result.accepted,
            mission_id=mission.mission_id,
            rejection_reason=result.rejection_reason.value if result.rejection_reason else None,
            rejection_detail=result.rejection_detail,
        )

    # ------------------------------------------------------------------
    # POST /api/killswitch
    # ------------------------------------------------------------------

    @router.post("/killswitch", response_model=KillSwitchResponse)
    def post_killswitch(req: KillSwitchRequest) -> KillSwitchResponse:
        """
        Activeer de kill-switch.

        Vereist operator_confirm=true — zonder die vlag wordt het verzoek
        geweigerd zonder actie.
        """
        if not req.operator_confirm:
            raise HTTPException(
                status_code=400,
                detail="operator_confirm must be true to activate kill-switch",
            )

        if req.level not in (1, 2, 3):
            raise HTTPException(
                status_code=400,
                detail=f"level must be 1, 2 or 3 — got {req.level}",
            )

        if ctx.queen is None or ctx.scheduler is None:
            raise HTTPException(
                status_code=503,
                detail="colony not initialised — queen or scheduler unavailable",
            )

        from ant_colony.colony.scheduler.colony_scheduler import KillLevel
        level_map = {1: KillLevel.AGENT, 2: KillLevel.NODE, 3: KillLevel.COLONY}
        kill_level = level_map[req.level]

        try:
            ctx.queen.kill_switch(level=kill_level, scope=req.scope)
        except Exception:
            logger.exception("Kill-switch failed")
            raise HTTPException(status_code=500, detail="kill-switch execution failed")

        level_names = {1: "AGENT", 2: "NODE", 3: "COLONY"}
        msg = f"Kill-switch level {level_names[req.level]} activated"
        if req.scope:
            msg += f" (scope={req.scope})"

        logger.warning("Dashboard kill-switch: %s", msg)
        return KillSwitchResponse(
            executed=True,
            level=req.level,
            scope=req.scope,
            message=msg,
        )

    # ------------------------------------------------------------------
    # POST /api/operator/input
    # ------------------------------------------------------------------

    @router.post("/operator/input", response_model=OperatorInputResponse)
    def post_operator_input(req: OperatorInputRequest) -> OperatorInputResponse:
        """
        Schrijf menselijke input naar ANT_LOGS/operator/input/.

        De OperatorAnt pikt het bestand op bij de volgende tick.
        """
        if ctx.logs_root is None:
            raise HTTPException(status_code=503, detail="logs_root niet geconfigureerd")

        input_type = req.type.strip().lower()
        if input_type not in ("url", "text", "code", "image"):
            raise HTTPException(status_code=400, detail="type moet url, text, code of image zijn")

        if input_type == "image":
            if not req.image_data:
                raise HTTPException(status_code=400, detail="image_data vereist voor type=image")
            payload: dict = {"type": "image", "content": "", "image_data": req.image_data, "media_type": req.media_type}
        else:
            content = req.content.strip()
            if not content:
                raise HTTPException(status_code=400, detail="content mag niet leeg zijn")
            payload = {"type": input_type, "content": content}

        ts       = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        filename = f"{ts}_{input_type}.json"

        input_dir = ctx.logs_root / "operator" / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            file_path = input_dir / filename
            with file_path.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
        except OSError:
            logger.exception("POST /api/operator/input: kon bestand niet schrijven")
            raise HTTPException(status_code=500, detail="failed to write input file")

        logger.info("Operator input geschreven | file=%s type=%s", filename, payload["type"])
        return OperatorInputResponse(
            accepted=True,
            filename=filename,
            message=f"Input opgeslagen als {filename}",
        )

    # ------------------------------------------------------------------
    # GET /api/ants/activity
    # ------------------------------------------------------------------

    @router.get("/ants/activity", response_model=AntActivityResponse)
    def get_ant_activity() -> AntActivityResponse:
        """Activiteitsoverzicht per mier — gelezen uit ANT_LOGS subdirs."""
        if ctx.logs_root is None:
            return AntActivityResponse(ants=[])

        today  = _today_cutoff()
        result: list[AntActivityEntry] = []

        for ant_type, subdir in _ANT_LOG_DIRS.items():
            records, dir_exists = _read_ant_dir(ctx.logs_root, subdir)

            last_seen:     str | None  = None
            recent_events: list[str]   = []
            stats   = AntStatsEntry()

            if not dir_exists:
                summary = "Nog geen activiteit."
            elif not records:
                summary = "Actief maar nog geen events gelogd."
            else:
                # Gebruik alleen huidige sessie (meest recente ant_id) voor display.
                # Zo lekken timestamps van vorige sessies niet door in last_seen/summary.
                session_recs = _latest_session_records(records) or records

                last_rec  = session_recs[-1]
                last_ts   = _parse_ts(last_rec.get("timestamp"))
                last_seen = _to_local_str(last_ts) if last_ts else None

                recent_events = [_event_short(r) for r in session_recs[-3:]]

                today_recs = [
                    r for r in records
                    if (_parse_ts(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= today
                ]

                stats   = _build_ant_stats(ant_type, today_recs)
                summary = _build_ant_summary(ant_type, session_recs, stats)

            result.append(AntActivityEntry(
                ant_type=ant_type,
                last_seen=last_seen,
                summary=summary,
                recent_events=recent_events,
                stats=stats,
            ))

        return AntActivityResponse(ants=result)

    # ------------------------------------------------------------------
    # GET /api/ants/{ant_type}/events
    # ------------------------------------------------------------------

    @router.get("/ants/{ant_type}/events", response_model=AntEventsResponse)
    def get_ant_events(ant_type: str, limit: int = 10) -> AntEventsResponse:
        """
        Laatste N events voor een specifiek ant-type.

        ant_type: korte naam zonder _ant suffix (bijv. "scout", "research",
                  "paper") of equities sub-type ("sector_scout", "rs_regime").
        limit:    aantal events (default 10, max 100).

        Events zijn gesorteerd op timestamp descending (nieuwste eerst).
        Alleen events van de laatste 24 uur worden teruggegeven.
        """
        if ant_type not in _ANT_EVENTS_DIRS:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Onbekend ant_type: {ant_type!r}")

        limit = max(1, min(limit, 100))

        if ctx.logs_root is None:
            return AntEventsResponse(ant_type=ant_type, events=[], count=0, has_more=False)

        subdir = _ANT_EVENTS_DIRS[ant_type]
        records, _ = _read_ant_dir(ctx.logs_root, subdir)

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
        recent = [
            r for r in records
            if (_parse_ts(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
        ]

        # Nieuwste eerst
        recent.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

        has_more  = len(recent) > limit
        page      = recent[:limit]

        events: list[AntEventEntry] = []
        for r in page:
            payload = r.get("payload") or {}
            action  = _action_of(r)
            events.append(AntEventEntry(
                timestamp=r.get("timestamp") or "",
                action=action,
                summary=_build_event_summary(action, payload),
                payload=payload,
            ))

        return AntEventsResponse(
            ant_type=ant_type,
            events=events,
            count=len(events),
            has_more=has_more,
        )

    # ------------------------------------------------------------------
    # GET /api/performance/chart
    # ------------------------------------------------------------------

    @router.get("/performance/chart", response_model=PerformanceChartResponse)
    def get_performance_chart() -> PerformanceChartResponse:
        """Cumulatief PnL curve + periode-totalen uit alle paper trade logs."""
        if ctx.logs_root is None:
            return PerformanceChartResponse(points=[], total_pnl=0.0)

        now    = datetime.now(tz=timezone.utc)
        trades = _read_all_trades(ctx.logs_root)

        def _pnl_since(cutoff: datetime) -> float:
            total = 0.0
            for t in trades:
                ts = _parse_ts(t.get("closed_at"))
                if ts is not None and ts >= cutoff:
                    total += float(t.get("realized_pnl") or 0.0)
            return round(total, 2)

        today   = _today_cutoff()
        dag     = _pnl_since(today)
        week    = _pnl_since(now - timedelta(weeks=1))
        maand   = _pnl_since(now - timedelta(days=30))
        jaar    = _pnl_since(now - timedelta(days=365))
        alltime = round(sum(float(t.get("realized_pnl") or 0.0) for t in trades), 2)

        # Build day curve
        day_pairs: list[tuple[datetime, float]] = []
        for t in trades:
            ts = _parse_ts(t.get("closed_at"))
            if ts and ts >= today:
                day_pairs.append((ts, float(t.get("realized_pnl") or 0.0)))
        day_pairs.sort(key=lambda x: x[0])

        # Fall back to alltime curve when fewer than 2 day trades
        if len(day_pairs) >= 2:
            curve_pairs = day_pairs
            curve_label = "vandaag"
        else:
            all_pairs: list[tuple[datetime, float]] = []
            for t in trades:
                ts = _parse_ts(t.get("closed_at"))
                if ts:
                    all_pairs.append((ts, float(t.get("realized_pnl") or 0.0)))
            all_pairs.sort(key=lambda x: x[0])
            curve_pairs = all_pairs
            curve_label = "alltime"

        points: list[ChartPoint] = []
        cumulative = 0.0
        for ts, pnl in curve_pairs:
            cumulative = round(cumulative + pnl, 2)
            points.append(ChartPoint(time=_to_local_str(ts), pnl=cumulative))

        if points:
            points = [ChartPoint(time=points[0].time, pnl=0.0)] + points

        return PerformanceChartResponse(
            points=points,
            total_pnl=round(cumulative, 2),
            curve_label=curve_label,
            dag=dag,
            week=week,
            maand=maand,
            jaar=jaar,
            alltime=alltime,
        )

    # ------------------------------------------------------------------
    # GET /api/queen/status
    # ------------------------------------------------------------------

    @router.get("/queen/status", response_model=QueenStatusResponse)
    def get_queen_status() -> QueenStatusResponse:
        """Marktregime, top strategieën en laatste Queen beslissing."""
        if ctx.logs_root is None:
            return QueenStatusResponse(regime=None, top_strategies=[], last_decision=None)

        data = _read_queen_status_data(ctx.logs_root)
        return QueenStatusResponse(
            regime=data["regime"],
            top_strategies=[QueenStrategyEntry(**s) for s in data["top_strategies"]],
            last_decision=data["last_decision"],
        )

    # ------------------------------------------------------------------
    # GET /api/queen/decisions
    # ------------------------------------------------------------------

    @router.get("/queen/decisions", response_model=QueenDecisionsResponse)
    def get_queen_decisions(limit: int = 10) -> QueenDecisionsResponse:
        """Laatste N Queen beslissingen, nieuwste eerst, max 7 dagen terug."""
        if ctx.logs_root is None:
            return QueenDecisionsResponse(decisions=[], count=0, has_more=False)
        return _read_queen_decisions(ctx.logs_root, max(1, min(limit, 100)))

    # ------------------------------------------------------------------
    # GET /api/activity
    # ------------------------------------------------------------------

    @router.get("/activity", response_model=ActivityFeedResponse)
    def get_activity_feed(
        limit: int = 50,
        filter: str = "all",
    ) -> ActivityFeedResponse:
        """
        Gecombineerde activity feed van alle ants, nieuwste eerst.

        limit:  aantal events (10–200, default 50)
        filter: "all" | "errors" | ant_type (scout, research, paper, …)
        """
        from datetime import datetime, timezone as _tz
        limit_  = max(10, min(limit, 200))
        filter_ = (filter or "all").lower().strip()

        if ctx.logs_root is None:
            return ActivityFeedResponse(
                events=[],
                count=0,
                filter=filter_,
                fetched_at=datetime.now(tz=_tz.utc).isoformat(),
            )
        return _read_activity_feed(ctx.logs_root, limit_, filter_)

    # ------------------------------------------------------------------
    # GET /api/equities/status
    # ------------------------------------------------------------------

    @router.get("/equities/status", response_model=EquitiesStatusResponse)
    def get_equities_status() -> EquitiesStatusResponse:
        """Equities biome status: sector rotatie, Piotroski, breakout en dividend."""
        import os
        enabled = os.getenv("EQUITIES_ENABLED", "false").lower() == "true"

        if ctx.logs_root is None:
            return EquitiesStatusResponse(
                enabled=enabled,
                sector_long=[], sector_neutral=[],
                piotroski_candidates=[], breakout_signals=[], dividend_candidates=[],
            )

        data = _read_equities_data(ctx.logs_root)
        return EquitiesStatusResponse(
            enabled=enabled,
            sector_long=[EquitiesSectorEntry(**e) for e in data["sector_long"]],
            sector_neutral=[EquitiesSectorEntry(**e) for e in data["sector_neutral"]],
            piotroski_candidates=[EquitiesPiotroskiEntry(**e) for e in data["piotroski_candidates"]],
            breakout_signals=[EquitiesBreakoutEntry(**e) for e in data["breakout_signals"]],
            dividend_candidates=[EquitiesDividendEntry(**e) for e in data["dividend_candidates"]],
            vix_level=data["vix_level"],
            vix_signal=data["vix_signal"],
            last_sector_ts=data["last_sector_ts"],
            last_piotroski_ts=data["last_piotroski_ts"],
            last_breakout_ts=data["last_breakout_ts"],
            last_dividend_ts=data["last_dividend_ts"],
        )

    return router


# ---------------------------------------------------------------------------
# Intern — open posities helpers
# ---------------------------------------------------------------------------

_POSITION_ZOMBIE_SECONDS: int = 24 * 3600   # posities ouder dan 24u = zombie


def _read_open_positions_from_logs(logs_root: Path) -> list[dict]:
    """
    Lees openstaande paper posities uit ANT_LOGS/paper/*.jsonl.

    Strategie:
      - Verzamel alle trade_opened en trade_closed events uit de meest recente
        ant-sessie (latest source UUID — voorkomt zombie-posities van vorige runs).
      - Retourneer posities met trade_opened maar zonder matching trade_closed.
      - Posities ouder dan 24u worden als zombie beschouwd en overgeslagen.

    Retourneert een lijst van payload-dicts (keys: position_id, symbol, biome,
    side, entry_price, quantity, stop_loss, take_profit, strategy_type, opened_at).
    """
    paper_dir = logs_root / "paper"
    if not paper_dir.exists():
        return []

    records: list[dict] = []
    for path in paper_dir.glob("*.jsonl"):
        if "_trades" in path.name:
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    session_recs = _latest_session_records(records)
    now          = time.time()

    opened:     dict[str, dict]  = {}   # position_id → payload
    opened_age: dict[str, float] = {}   # position_id → unix timestamp (from event ts)
    closed_ids: set[str]         = set()

    for r in session_recs:
        payload = r.get("payload") or {}
        action  = payload.get("action")
        pos_id  = str(payload.get("position_id") or "")
        if not pos_id:
            continue

        if action == "trade_opened":
            rec_ts = _parse_ts(r.get("timestamp"))
            opened[pos_id] = {
                "position_id":       pos_id,
                "symbol":            str(payload.get("symbol") or ""),
                "biome":             str(payload.get("biome") or "crypto"),
                "side":              str(payload.get("side") or "long"),
                "entry_price":       float(payload.get("entry_price") or 0),
                "quantity":          float(payload.get("quantity") or 0),
                "stop_loss_price":   float(payload.get("stop_loss") or 0),
                "take_profit_price": float(payload.get("take_profit") or 0),
                "strategy_type":     payload.get("strategy_type"),
                "opened_at":         r.get("timestamp") or "",
            }
            if rec_ts is not None:
                opened_age[pos_id] = rec_ts.timestamp()

        elif action == "trade_closed":
            closed_ids.add(pos_id)

    result: list[dict] = []
    for pos_id, pos in opened.items():
        if pos_id in closed_ids:
            continue
        age_sec = now - opened_age.get(pos_id, now)
        if age_sec > _POSITION_ZOMBIE_SECONDS:
            continue
        result.append(pos)

    return result


def _get_live_price(
    registry: BiomeRegistry,
    biome_id: str,
    symbol: str,
) -> float | None:
    """
    Haal de meest recente close-prijs op voor een symbool.

    Probeert eerst de opgegeven biome_id, daarna alle andere geregistreerde biomes.
    Retourneert None als geen adapter beschikbaar is of data ontbreekt.
    """
    biomes_to_try = [biome_id] + [b for b in registry.list_biomes() if b != biome_id]
    for bid in biomes_to_try:
        adapter = registry.get(bid)
        if adapter is None:
            continue
        try:
            md = adapter.get_market_data(symbol, "1m")
            if md is not None and md.close > 0:
                return md.close
        except Exception:
            logger.exception("_get_live_price: fout voor %s/%s", bid, symbol)
    return None


def _compute_sl_tp_progress(
    side: str,
    current: float | None,
    sl: float,
    tp: float,
) -> float | None:
    """
    Positie van de huidige prijs tussen SL (0.0) en TP (1.0).

    LONG : progress = (current - sl) / (tp - sl)
    SHORT: progress = (sl - current) / (sl - tp)

    Geclamped naar [0.0, 1.0]. Retourneert None als current ontbreekt of
    de range nul is (zou duiden op ongeldige SL/TP).
    """
    if current is None:
        return None
    if side == "long":
        denom = tp - sl
        if denom <= 0:
            return None
        return max(0.0, min(1.0, (current - sl) / denom))
    else:  # short
        denom = sl - tp
        if denom <= 0:
            return None
        return max(0.0, min(1.0, (sl - current) / denom))


# ---------------------------------------------------------------------------
# Intern — reële equity berekening
# ---------------------------------------------------------------------------

def _real_equity(registry: BiomeRegistry | None) -> float | None:
    """
    Som van echte broker-equity over alle geregistreerde adapters.

    equity per adapter = EUR beschikbaar  +  marktwaarde crypto holdings

    Returns None als registry leeg is of alle adapters falen, zodat de
    aanroeper kan terugvallen op queen.capital_total.
    """
    if registry is None:
        return None

    total = 0.0
    found = False

    for biome_id in registry.list_biomes():
        adapter = registry.get(biome_id)
        if adapter is None:
            continue
        try:
            state = adapter.get_account_state()
            if state is None:
                continue

            holdings_value = 0.0
            positions = adapter.get_positions()
            if positions:
                holdings_value = sum(p.market_value for p in positions)

            total += state.balance + holdings_value
            found = True
        except Exception:
            logger.exception("_real_equity: fout voor biome_id=%s", biome_id)

    return total if found else None


# ---------------------------------------------------------------------------
# Intern — log helpers
# ---------------------------------------------------------------------------

def _last_tick_from_logs(logs_root: Path | None) -> datetime | None:
    """Lees de timestamp van het laatste scheduler-tick event uit disk."""
    if logs_root is None:
        return None
    log_file = logs_root / "colony" / "scheduler.jsonl"
    return _last_timestamp_in_file(log_file)


def _last_timestamp_in_file(path: Path) -> datetime | None:
    """Lees de laatste niet-lege regel van een JSONL bestand en parseer timestamp."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            last_line = None
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
        if last_line is None:
            return None
        record = json.loads(last_line)
        return _parse_ts(record.get("timestamp"))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def _read_all_trades(logs_root: Path) -> list[dict]:
    """Lees alle gesloten paper trades uit ANT_LOGS/paper/*_trades.jsonl."""
    trades: list[dict] = []
    paper_dir = logs_root / "paper"
    if not paper_dir.exists():
        return trades
    for path in paper_dir.glob("*_trades.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            trades.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
    return trades


def _read_recent_events(logs_root: Path, limit: int = 20) -> list[TickerEvent]:
    """
    Lees de meest recente audit events uit alle JSONL logs.

    Scant recursief alle *.jsonl bestanden in logs_root, verzamelt alle
    events, sorteert op timestamp en retourneert de laatste `limit` events.
    """
    records: list[tuple[datetime, dict]] = []

    for path in logs_root.rglob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        ts = _parse_ts(record.get("timestamp"))
                        if ts is not None:
                            records.append((ts, record))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    records.sort(key=lambda x: x[0])
    recent = records[-limit:]

    events = []
    for ts, rec in recent:
        events.append(TickerEvent(
            event_type=rec.get("event_type", "unknown"),
            source=rec.get("source", "unknown"),
            timestamp=_to_local_str(ts),
            mission_id=rec.get("mission_id"),
            payload=rec.get("payload", {}),
        ))
    return events


_LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def _to_local_str(dt: datetime) -> str:
    """Formatteer UTC datetime als HH:MM:SS in Europe/Amsterdam tijdzone."""
    return dt.astimezone(_LOCAL_TZ).strftime("%H:%M:%S")


def _parse_ts(value: str | None) -> datetime | None:
    """Parseer een ISO-8601 timestamp string naar een timezone-aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Intern — mieren activiteit helpers
# ---------------------------------------------------------------------------

_ANT_LOG_DIRS: dict[str, str] = {
    "scout_ant":     "scouts",
    "research_ant":  "research",
    "paper_ant":     "paper",
    "audit_ant":     "audit",
    "ingestion_ant": "ingestion",
    "strategy_ant":  "strategy",
    "execution_ant": "execution",
    "operator_ant":  "operator",
    "claude_ant":    "claude",
}

# Kortere namen voor GET /api/ants/{ant_type}/events
_ANT_EVENTS_DIRS: dict[str, str] = {
    # Core ants (zonder _ant suffix)
    "scout":        "scouts",
    "research":     "research",
    "paper":        "paper",
    "audit":        "audit",
    "ingestion":    "ingestion",
    "strategy":     "strategy",
    "execution":    "execution",
    "operator":     "operator",
    "claude":       "claude",
    # Equities sub-ants
    "sector_scout":  "equities/sector_scout",
    "rs_regime":     "rs_regime",
    "dividend":      "equities/dividend",
    "fundamental":   "equities/fundamental",
    "piotroski":     "equities/piotroski",
    "breakout":      "equities/breakout",
    "momentum_rank": "equities/momentum_rank",
    "rotation":      "equities/rotation",
    "rebalance":     "equities/rebalance",
    "volatility":    "equities/volatility",
}


def _today_cutoff() -> datetime:
    now = datetime.now(tz=timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _read_ant_dir(logs_root: Path, subdir: str) -> tuple[list[dict], bool]:
    """
    Lees alle niet-trades JSONL records uit een ant-subdir.

    Returns:
        (records, dir_exists) — dir_exists=False als de map nog niet bestaat.
        records gesorteerd op timestamp.
    """
    ant_dir = logs_root / subdir
    if not ant_dir.exists():
        return [], False
    records: list[dict] = []
    for path in ant_dir.glob("*.jsonl"):
        if "_trades" in path.name:
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
    records.sort(key=lambda r: r.get("timestamp", ""))
    return records, True


def _action_of(rec: dict) -> str:
    return (rec.get("payload") or {}).get("action") or rec.get("event_type", "")


def _build_event_summary(action: str, payload: dict) -> str:
    """
    Genereer een mensleesbare samenvatting van een log event.

    Produceert een compacte string per action-type, bijv.:
      trade_opened        → "BTC-EUR LONG 0.00075 @ 66402"
      trade_closed        → "BTC-EUR stop_loss pnl=+0.54"
      candidate_accepted  → "BTC-EUR sharpe=0.245 win_rate=0.587"
      opportunity_detected→ "ETH-EUR confidence=0.83"
    """
    sym  = str(payload.get("symbol") or "")
    side = str(payload.get("side") or "").upper()

    if action == "trade_opened":
        entry = payload.get("entry_price")
        qty   = payload.get("quantity")
        entry_s = f"{entry:.2f}" if entry is not None else "?"
        qty_s   = f"{qty:.6g}" if qty is not None else "?"
        return f"{sym} {side} {qty_s} @ {entry_s}".strip()

    if action == "trade_closed":
        reason = str(payload.get("exit_reason") or "")
        pnl    = payload.get("realized_pnl")
        pnl_s  = (f"pnl={'+' if pnl >= 0 else ''}{pnl:.2f}" if pnl is not None else "")
        return " ".join(p for p in [sym, reason, pnl_s] if p)

    if action in ("candidate_accepted", "candidate_evaluated", "candidate_proposed"):
        sharpe = payload.get("sharpe")
        wr     = payload.get("win_rate")
        st     = str(payload.get("strategy_type") or "")
        parts  = [p for p in [sym, st] if p]
        if sharpe is not None:
            parts.append(f"sharpe={sharpe:.3f}")
        if wr is not None:
            parts.append(f"win_rate={wr:.3f}")
        return " ".join(parts)

    if action in ("opportunity_detected", "signal_detected", "price_move", "volume_spike"):
        conf = payload.get("confidence")
        conf_s = f"confidence={conf:.2f}" if conf is not None else ""
        sig_type = str(payload.get("signal_type") or "")
        return " ".join(p for p in [sym, sig_type, conf_s] if p)

    if action == "repo_ingested":
        repo  = str(payload.get("repo") or payload.get("repo_name") or "")
        stars = payload.get("stars")
        stars_s = f"stars={stars}" if stars is not None else ""
        return " ".join(p for p in [repo, stars_s] if p)

    if action == "pnl_summary":
        count = payload.get("trade_count")
        pnl   = payload.get("total_realized_pnl")
        wr    = payload.get("win_rate")
        parts = []
        if count is not None:
            parts.append(f"{count} closed")
        if pnl is not None:
            parts.append(f"pnl={'+' if pnl >= 0 else ''}{pnl:.2f}")
        if wr is not None:
            parts.append(f"winrate={wr * 100:.0f}%")
        return " ".join(parts)

    if action == "audit_finding":
        severity   = str(payload.get("severity") or "")
        component  = str(payload.get("component") or "")
        check_name = str(payload.get("check_name") or "")
        detail     = str(payload.get("detail") or "")
        label = " ".join(p for p in [severity, component, check_name] if p)
        return f"{label} — {detail}" if detail else label

    if action == "variant_generated":
        st    = str(payload.get("strategy_type") or "")
        grade = str(payload.get("grade") or "")
        sharpe = payload.get("sharpe")
        parts = [p for p in [sym, st, grade] if p]
        if sharpe is not None:
            parts.append(f"sharpe={sharpe:.3f}")
        return " ".join(parts)

    # Standaard: symbol (indien aanwezig), anders de action zelf
    return sym if sym else action.replace("_", " ")


def _event_short(rec: dict) -> str:
    """Formatteer een log record als korte event string voor de tijdlijn."""
    ts      = _parse_ts(rec.get("timestamp"))
    ts_str  = _to_local_str(ts) if ts else "?"
    action  = _action_of(rec).replace("_", " ")
    payload = rec.get("payload") or {}
    symbol        = payload.get("symbol") or ""
    sharpe        = payload.get("sharpe")
    grade         = payload.get("grade") or ""
    strategy_type = payload.get("strategy_type") or ""
    best_regime   = payload.get("best_regime") or ""
    parts = [ts_str, action]
    if symbol:
        parts.append(symbol)
    if strategy_type:
        parts.append(strategy_type)
    if sharpe is not None:
        parts.append(f"sharpe={sharpe:.2f}")
    if grade:
        parts.append(f"grade={grade}")
    if best_regime:
        parts.append(f"regime={best_regime}")
    return " · ".join(parts)


def _count_actions(recs: list[dict], *keywords: str) -> int:
    return sum(1 for r in recs if any(kw in _action_of(r) for kw in keywords))


def _latest_session_records(records: list[dict]) -> list[dict]:
    """
    Filter records op de meest recente source (ant_id).

    Bij een herstart krijgt elke ant een nieuw UUID als ant_id (= source veld).
    Door alleen de meest recente source te bewaren, tellen zombie-posities van
    een vorige sessie (geopend maar nooit gesloten) niet mee als 'open'.
    """
    if not records:
        return records
    latest_ts: datetime = datetime.min.replace(tzinfo=timezone.utc)
    latest_source: str | None = None
    for r in records:
        ts  = _parse_ts(r.get("timestamp"))
        src = r.get("source")
        if src and ts and ts > latest_ts:
            latest_ts = ts
            latest_source = src
    if latest_source is None:
        return records
    return [r for r in records if r.get("source") == latest_source]


def _build_ant_stats(ant_type: str, today_recs: list[dict]) -> AntStatsEntry:
    if ant_type == "scout_ant":
        return AntStatsEntry(
            signals_found=_count_actions(today_recs, "signal", "opportunity", "detected"),
        )
    if ant_type == "research_ant":
        return AntStatsEntry(
            candidates_above_threshold=_count_actions(today_recs, "candidate_accepted"),
        )
    if ant_type == "paper_ant":
        # Open posities: gebruik alleen huidige sessie (source = meest recente ant_id)
        # zodat zombie-posities van een vorige sessie niet meetellen.
        session_recs = _latest_session_records(today_recs)
        opened_ids = {
            (r.get("payload") or {}).get("position_id")
            for r in session_recs
            if _action_of(r) in ("trade_opened", "position_opened")
        } - {None}
        closed_ids = {
            (r.get("payload") or {}).get("position_id")
            for r in session_recs
            if _action_of(r) in ("trade_closed", "position_closed")
        } - {None}
        # PnL: alle gesloten trades van vandaag (ook eerdere sessies)
        closed_recs = [r for r in today_recs if _action_of(r) in ("trade_closed", "position_closed")]
        pnl_vals    = [float((r.get("payload") or {}).get("realized_pnl") or 0) for r in closed_recs]
        total_pnl   = round(sum(pnl_vals), 2)
        wins        = sum(1 for v in pnl_vals if v > 0)
        win_rate    = round(wins / len(pnl_vals), 2) if pnl_vals else None
        return AntStatsEntry(
            open_trades=len(opened_ids - closed_ids),
            total_pnl=total_pnl,
            win_rate=win_rate,
        )
    if ant_type == "audit_ant":
        return AntStatsEntry(
            warnings_today=_count_actions(today_recs, "warning", "anomaly", "error"),
            anomalies_today=_count_actions(today_recs, "anomaly"),
        )
    if ant_type == "ingestion_ant":
        return AntStatsEntry(
            repos_ingested=_count_actions(today_recs, "candidate_ingested", "ingested"),
        )
    if ant_type == "strategy_ant":
        return AntStatsEntry(
            variants_generated=_count_actions(today_recs, "variant_emitted", "emitted"),
        )
    if ant_type == "execution_ant":
        return AntStatsEntry(
            orders_placed=_count_actions(today_recs, "order_placed", "position_opened"),
            orders_rejected=_count_actions(today_recs, "order_rejected", "rejected", "startup_failed"),
        )
    if ant_type == "operator_ant":
        return AntStatsEntry(
            inputs_processed=_count_actions(today_recs, "operator_input_processed"),
        )
    if ant_type == "claude_ant":
        return AntStatsEntry(
            variants_generated=_count_actions(today_recs, "claude_analysis_complete"),
        )
    return AntStatsEntry()


def _build_ant_summary(ant_type: str, all_recs: list[dict], stats: AntStatsEntry) -> str:
    if not all_recs:
        return "Geen activiteit geregistreerd."
    action = _action_of(all_recs[-1]).replace("_", " ")
    if ant_type == "scout_ant":
        n = stats.signals_found or 0
        return f"{n} signalen vandaag — laatste: {action}"
    if ant_type == "research_ant":
        n = stats.candidates_above_threshold or 0
        grades = {"A": 0, "B": 0, "C": 0}
        for r in all_recs:
            g = (r.get("payload") or {}).get("grade") or ""
            if g in grades:
                grades[g] += 1
        grade_str = f"A:{grades['A']} B:{grades['B']} C:{grades['C']}"
        return f"{n} kandidaten vandaag ({grade_str}) — laatste: {action}"
    if ant_type == "paper_ant":
        pnl  = stats.total_pnl or 0.0
        sign = "+" if pnl >= 0 else ""
        wr   = f"{int((stats.win_rate or 0) * 100)}%" if stats.win_rate is not None else "—"
        return f"PnL vandaag: {sign}€{pnl:.2f} · winrate: {wr}"
    if ant_type == "audit_ant":
        w = stats.warnings_today or 0
        return f"{w} waarschuwingen vandaag — laatste: {action}"
    if ant_type == "ingestion_ant":
        n = stats.repos_ingested or 0
        return f"{n} repos geïngesteerd vandaag — laatste: {action}"
    if ant_type == "strategy_ant":
        n = stats.variants_generated or 0
        return f"{n} varianten gegenereerd vandaag — laatste: {action}"
    if ant_type == "execution_ant":
        p = stats.orders_placed or 0
        r = stats.orders_rejected or 0
        return f"{p} orders geplaatst, {r} geweigerd — laatste: {action}"
    if ant_type == "operator_ant":
        n = stats.inputs_processed or 0
        return f"{n} inputs verwerkt vandaag — laatste: {action}"
    if ant_type == "claude_ant":
        n = stats.variants_generated or 0
        # Toon budget info uit meest recente log record
        month_cost = 0.0
        budget = 10.0
        for r in reversed(all_recs):
            p = r.get("payload") or {}
            if "month_cost_eur" in p:
                month_cost = p["month_cost_eur"]
                budget     = p.get("budget_eur", budget)
                break
            if "total_cost_usd" in p:
                month_cost = p.get("month_cost_eur", p.get("total_cost_usd", 0.0))
                budget     = p.get("budget_eur", budget)
                break
        return f"Claude Ant · €{month_cost:.2f} gebruikt van €{budget:.2f} budget · {n} analyses"
    return f"Laatste actie: {action}"


# ---------------------------------------------------------------------------
# Intern — paper kapitaal in gebruik
# ---------------------------------------------------------------------------

def _read_paper_capital_in_use(logs_root: Path) -> float:
    """
    Som van entry_price × quantity voor openstaande paper posities (huidige sessie).

    Leest niet-trades JSONL records uit ANT_LOGS/paper/, filtert op vandaag en
    de meest recente ant_id (sessie), en berekent de marktwaarde van posities die
    geopend maar nog niet gesloten zijn.
    """
    paper_dir = logs_root / "paper"
    if not paper_dir.exists():
        return 0.0

    records: list[dict] = []
    for path in paper_dir.glob("*.jsonl"):
        if "_trades" in path.name:
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass

    today = _today_cutoff()
    today_recs = [
        r for r in records
        if (_parse_ts(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= today
    ]
    session_recs = _latest_session_records(today_recs)

    opened: dict[str, dict] = {}
    for r in session_recs:
        if _action_of(r) in ("trade_opened", "position_opened"):
            payload = r.get("payload") or {}
            pid = payload.get("position_id")
            if pid:
                opened[pid] = {
                    "entry_price": float(payload.get("entry_price") or 0),
                    "quantity": float(payload.get("quantity") or 0),
                }

    for r in session_recs:
        if _action_of(r) in ("trade_closed", "position_closed"):
            pid = (r.get("payload") or {}).get("position_id")
            if pid:
                opened.pop(pid, None)

    return round(sum(v["entry_price"] * v["quantity"] for v in opened.values()), 2)


# ---------------------------------------------------------------------------
# Intern — Queen status
# ---------------------------------------------------------------------------

def _read_queen_status_data(logs_root: Path) -> dict:
    """Lees regime, top strategieën en laatste Queen beslissing uit logs."""
    from collections import Counter

    # Kandidaten uit research logs
    research_dir = logs_root / "research"
    candidates: list[dict] = []
    if research_dir.exists():
        for path in research_dir.glob("*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            payload = rec.get("payload") or {}
                            if payload.get("action") == "candidate_accepted":
                                candidates.append(payload)
                        except json.JSONDecodeError:
                            pass
            except OSError:
                pass

    # Regime: meest voorkomend best_regime
    regime: str | None = None
    regimes = [c.get("best_regime") for c in candidates if c.get("best_regime")]
    if regimes:
        regime = Counter(regimes).most_common(1)[0][0]

    # Top 3 by sharpe — diversiteit: max 1 per symbool, max 1 per strategy_type
    from ant_colony.queen.queen_advisor import select_diverse_top_n
    top_3 = select_diverse_top_n(candidates, n=3)
    top_strategies = [
        {
            "strategy_type": c.get("strategy_type") or c.get("strategy") or "unknown",
            "symbol": c.get("symbol") or "—",
            "sharpe": round(float(c.get("sharpe_ratio") or c.get("sharpe") or 0), 2),
            "best_regime": c.get("best_regime") or "—",
        }
        for c in top_3
    ]

    # Laatste Queen beslissing uit ANT_LOGS/queen/decisions.jsonl
    last_decision: dict | None = None
    decisions_path = logs_root / "queen" / "decisions.jsonl"
    if decisions_path.exists():
        try:
            last_line: str | None = None
            with decisions_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last_line = line
            if last_line:
                rec = json.loads(last_line)
                verhogen   = rec.get("kapitaal_verhogen") or []
                verlagen   = rec.get("kapitaal_verlagen") or []
                prioriteit = rec.get("prioriteit_kandidaten") or []
                gevolgd    = rec.get("adviezen_gevolgd") or []
                genegeerd  = rec.get("adviezen_genegeerd") or []

                if verhogen:
                    action = f"Kapitaal verhogen ({len(verhogen)} mission{'s' if len(verhogen) != 1 else ''})"
                elif verlagen:
                    action = f"Kapitaal verlagen ({len(verlagen)} mission{'s' if len(verlagen) != 1 else ''})"
                elif prioriteit:
                    action = f"Prioriteit aanpassen ({len(prioriteit)} kandidaat{'en' if len(prioriteit) != 1 else ''})"
                elif gevolgd:
                    action = "Aanpassingen doorgevoerd"
                elif genegeerd:
                    action = "Neutraal — onvoldoende bewijs"
                else:
                    action = "Neutraal"

                # Eerste niet-lege reden als toelichting
                reason = next((s for s in gevolgd + genegeerd if s), "")

                last_decision = {
                    "timestamp": rec.get("timestamp"),
                    "action": action,
                    "reason": reason,
                }
        except (OSError, json.JSONDecodeError):
            pass

    return {"regime": regime, "top_strategies": top_strategies, "last_decision": last_decision}


def _parse_queen_decision(rec: dict) -> QueenDecisionEntry:
    """Vertaal één raw decisions.jsonl record naar een QueenDecisionEntry."""
    verhogen    = rec.get("kapitaal_verhogen")   or []
    verlagen    = rec.get("kapitaal_verlagen")   or []
    prioriteit  = rec.get("prioriteit_kandidaten") or []
    deprio      = rec.get("deprioriteer_kandidaten") or []
    gevolgd     = rec.get("adviezen_gevolgd")    or []
    genegeerd   = rec.get("adviezen_genegeerd")  or []
    alloc       = rec.get("allocatie_aanpassingen") or {}

    # decision_type: meest specifiek aanwezige actie
    if verhogen:
        decision_type = "capital_increase"
        n = len(verhogen)
        title   = f"Kapitaal verhogen ({n} mission{'s' if n != 1 else ''})"
        summary = "; ".join(gevolgd[:2]) if gevolgd else verhogen[0]
        mission_id = verhogen[0] if verhogen else None
    elif verlagen:
        decision_type = "capital_decrease"
        n = len(verlagen)
        title   = f"Kapitaal verlagen ({n} mission{'s' if n != 1 else ''})"
        summary = "; ".join(gevolgd[:2]) if gevolgd else verlagen[0]
        mission_id = verlagen[0] if verlagen else None
    elif prioriteit:
        decision_type = "mission_promote"
        n = len(prioriteit)
        title   = f"Missie geprioriteerd ({n} kandidaat{'en' if n != 1 else ''})"
        summary = "; ".join(gevolgd[:2]) if gevolgd else prioriteit[0]
        mission_id = prioriteit[0] if prioriteit else None
    elif deprio:
        decision_type = "mission_demote"
        n = len(deprio)
        title   = f"Missie gedeprioriteerd ({n} kandidaat{'en' if n != 1 else ''})"
        summary = "; ".join(genegeerd[:2]) if genegeerd else deprio[0]
        mission_id = deprio[0] if deprio else None
    elif alloc:
        decision_type = "regime_change"
        biomes  = ", ".join(alloc.keys())
        title   = f"Allocatie aangepast ({biomes})"
        summary = "; ".join(gevolgd[:2]) if gevolgd else f"biomes: {biomes}"
        mission_id = None
    elif gevolgd:
        decision_type = "strategy_select"
        title   = "Aanpassingen doorgevoerd"
        summary = "; ".join(gevolgd[:2])
        mission_id = None
    elif genegeerd:
        decision_type = "mission_pause"
        title   = "Neutraal — onvoldoende bewijs"
        summary = "; ".join(genegeerd[:2])
        mission_id = None
    else:
        decision_type = "neutral"
        title   = "Neutraal"
        summary = "Geen aanpassingen"
        mission_id = None

    return QueenDecisionEntry(
        timestamp     = rec.get("timestamp") or "",
        decision_type = decision_type,
        mission_id    = mission_id,
        title         = title,
        summary       = summary[:200],
        payload       = rec,
    )


def _read_queen_decisions(logs_root: Path, limit: int) -> QueenDecisionsResponse:
    """Lees laatste `limit` Queen beslissingen uit decisions.jsonl (nieuwste eerst)."""
    decisions_path = logs_root / "queen" / "decisions.jsonl"
    if not decisions_path.exists():
        return QueenDecisionsResponse(decisions=[], count=0, has_more=False)

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    raw: list[dict] = []
    try:
        with decisions_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(rec.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                raw.append(rec)
    except OSError:
        return QueenDecisionsResponse(decisions=[], count=0, has_more=False)

    # Sorteer descending op timestamp
    raw.sort(key=lambda r: r.get("timestamp") or "", reverse=True)

    has_more = len(raw) > limit
    selected = raw[:limit]
    entries  = [_parse_queen_decision(r) for r in selected]
    return QueenDecisionsResponse(decisions=entries, count=len(entries), has_more=has_more)


# ---------------------------------------------------------------------------
# Intern — activity feed
# ---------------------------------------------------------------------------

_ACTIVITY_DIRS: dict[str, str] = {
    "scout":     "scouts",
    "research":  "research",
    "paper":     "paper",
    "audit":     "audit",
    "ingestion": "ingestion",
    "strategy":  "strategy",
    "execution": "execution",
    "operator":  "operator",
    "claude":    "claude",
    "queen":     "queen",
}


def _read_ant_dir_recent(logs_root: Path, subdir: str, n_files: int = 2) -> list[dict]:
    """Lees records uit de N meest recente JSONL files in een ant subdir."""
    ant_dir = logs_root / subdir
    if not ant_dir.exists():
        return []
    files = sorted(
        [p for p in ant_dir.glob("*.jsonl") if "_trades" not in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:n_files]
    records: list[dict] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
    return records


def _event_level(rec: dict) -> str:
    """Bepaal het log level van een event record."""
    raw = str(rec.get("level") or "").lower()
    if raw in ("error", "critical"):
        return "error"
    if raw == "warning":
        return "warning"
    return "info"


def _read_activity_feed(logs_root: Path, limit: int, filter_: str) -> ActivityFeedResponse:
    """Gecombineerde activity feed: alle ant log dirs, 24u, nieuwste eerst."""
    now     = datetime.now(tz=timezone.utc)
    cutoff  = now - timedelta(hours=24)

    if filter_ in ("all", "errors"):
        dirs_to_scan: list[tuple[str, str]] = list(_ACTIVITY_DIRS.items())
    elif filter_ in _ACTIVITY_DIRS:
        dirs_to_scan = [(filter_, _ACTIVITY_DIRS[filter_])]
    else:
        dirs_to_scan = list(_ACTIVITY_DIRS.items())

    all_entries: list[ActivityFeedEntry] = []

    for ant_type, subdir in dirs_to_scan:
        records = _read_ant_dir_recent(logs_root, subdir)
        for rec in records:
            rec_ts = _parse_ts(rec.get("timestamp"))
            if rec_ts is None or rec_ts < cutoff:
                continue
            if ant_type == "queen":
                parsed = _parse_queen_decision(rec)
                all_entries.append(ActivityFeedEntry(
                    timestamp=rec.get("timestamp") or "",
                    ant_type="queen",
                    action=parsed.decision_type,
                    summary=parsed.summary,
                    level=_event_level(rec),
                ))
            else:
                action  = _action_of(rec)
                payload = rec.get("payload") or {}
                all_entries.append(ActivityFeedEntry(
                    timestamp=rec.get("timestamp") or "",
                    ant_type=ant_type,
                    action=action,
                    summary=_build_event_summary(action, payload),
                    level=_event_level(rec),
                ))

    if filter_ == "errors":
        all_entries = [e for e in all_entries if e.level in ("error", "warning")]

    all_entries.sort(key=lambda e: e.timestamp, reverse=True)
    capped = all_entries[:200]
    page   = capped[:limit]

    return ActivityFeedResponse(
        events=page,
        count=len(page),
        filter=filter_ or "all",
        fetched_at=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# Intern — equities log reader
# ---------------------------------------------------------------------------

def _scan_jsonl_dir(log_dir: Path, action: str) -> list[dict]:
    """
    Lees alle payloads met het gegeven action uit *.jsonl bestanden in log_dir.
    Retourneert lijst van payload-dicts (ongesorteerd).
    """
    results: list[dict] = []
    if not log_dir.exists():
        return results
    try:
        for path in sorted(log_dir.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            payload = rec.get("payload") or {}
                            if payload.get("action") == action:
                                results.append(payload)
                        except json.JSONDecodeError:
                            pass
            except OSError:
                pass
    except Exception:
        pass
    return results


def _latest_per_symbol(entries: list[dict], ts_key: str = "emitted_at") -> dict[str, dict]:
    """Dedupliceer op 'symbol', behoud de meest recente entry per symbol."""
    latest: dict[str, dict] = {}
    for e in entries:
        sym = e.get("symbol", "")
        if not sym:
            continue
        existing = latest.get(sym)
        if existing is None:
            latest[sym] = e
        else:
            ts_new = _parse_ts(e.get(ts_key))
            ts_old = _parse_ts(existing.get(ts_key))
            if ts_new and (ts_old is None or ts_new > ts_old):
                latest[sym] = e
    return latest


def _read_equities_data(logs_root: Path) -> dict:
    """
    Lees sector rotatie, Piotroski, breakout en dividend data uit equities logs.

    Directories:
      logs_root/scouts/          → opportunity_detected (sector rotatie)
      logs_root/equities/piotroski/ → piotroski_candidate
      logs_root/equities/breakout/  → breakout_signal
      logs_root/equities/dividend/  → dividend_candidate
    """
    # ── Sector rotatie ──────────────────────────────────────────────────
    sector_raw = _scan_jsonl_dir(logs_root / "scouts", "opportunity_detected")
    sector_by_sym = _latest_per_symbol(sector_raw, ts_key="emitted_at")

    sector_long: list[dict] = []
    sector_neutral: list[dict] = []
    last_sector_ts: str | None = None

    for sym, p in sector_by_sym.items():
        entry = {
            "symbol":     sym,
            "sector":     p.get("sector_name") or p.get("sector") or sym,
            "return_3mo": round(float(p.get("change_pct") or p.get("return_3mo") or 0), 6),
            "rank":       int(p.get("momentum_rank") or p.get("rank") or 0),
            "signal":     p.get("signal_type") or p.get("signal") or "NEUTRAL",
            "emitted_at": p.get("emitted_at"),
        }
        if entry["signal"] == "LONG":
            sector_long.append(entry)
        else:
            sector_neutral.append(entry)
        ts = p.get("emitted_at")
        if ts and (last_sector_ts is None or ts > last_sector_ts):
            last_sector_ts = ts

    sector_long    = sorted(sector_long,    key=lambda x: x["rank"])[:3]
    sector_neutral = sorted(sector_neutral, key=lambda x: x["rank"])[-3:]

    # ── Piotroski ───────────────────────────────────────────────────────
    piotroski_raw = _scan_jsonl_dir(logs_root / "equities" / "piotroski", "piotroski_candidate")
    piotroski_by_sym = _latest_per_symbol(piotroski_raw, ts_key="evaluated_at")

    piotroski_candidates: list[dict] = []
    last_piotroski_ts: str | None = None

    for sym, p in piotroski_by_sym.items():
        piotroski_candidates.append({
            "symbol":       sym,
            "f_score":      int(p.get("f_score") or 0),
            "evaluated_at": p.get("evaluated_at"),
        })
        ts = p.get("evaluated_at")
        if ts and (last_piotroski_ts is None or ts > last_piotroski_ts):
            last_piotroski_ts = ts

    piotroski_candidates = sorted(piotroski_candidates, key=lambda x: x["f_score"], reverse=True)

    # ── Breakout ────────────────────────────────────────────────────────
    breakout_raw = _scan_jsonl_dir(logs_root / "equities" / "breakout", "breakout_signal")
    breakout_by_sym = _latest_per_symbol(breakout_raw, ts_key="emitted_at")

    breakout_signals: list[dict] = []
    last_breakout_ts: str | None = None

    for sym, p in breakout_by_sym.items():
        breakout_signals.append({
            "symbol":           sym,
            "entry_price":      float(p.get("entry_price") or 0),
            "sl_price":         float(p.get("sl_price") or 0),
            "tp_price":         float(p.get("tp_price") or 0),
            "distance_to_high": float(p.get("distance_to_high") or 0),
            "emitted_at":       p.get("emitted_at"),
        })
        ts = p.get("emitted_at")
        if ts and (last_breakout_ts is None or ts > last_breakout_ts):
            last_breakout_ts = ts

    breakout_signals = sorted(breakout_signals, key=lambda x: x["emitted_at"] or "", reverse=True)

    # ── Dividend ────────────────────────────────────────────────────────
    dividend_raw = _scan_jsonl_dir(logs_root / "equities" / "dividend", "dividend_candidate")
    dividend_by_sym = _latest_per_symbol(dividend_raw, ts_key="emitted_at")

    dividend_candidates: list[dict] = []
    vix_level: float | None = None
    vix_signal: str | None = None
    last_dividend_ts: str | None = None

    for sym, p in dividend_by_sym.items():
        dividend_candidates.append({
            "symbol":            sym,
            "dividend_yield":    float(p.get("dividend_yield") or 0),
            "consecutive_years": int(p.get("consecutive_years") or 0),
            "payout_ratio":      float(p.get("payout_ratio") or 0),
            "emitted_at":        p.get("emitted_at"),
        })
        # VIX uit willekeurige kandidaat (alle entries hebben dezelfde VIX snapshot)
        if vix_level is None and p.get("vix_level") is not None:
            try:
                vix_level = float(p["vix_level"])
                vix_signal = str(p.get("vix_signal") or "NORMAL")
            except (TypeError, ValueError):
                pass
        ts = p.get("emitted_at")
        if ts and (last_dividend_ts is None or ts > last_dividend_ts):
            last_dividend_ts = ts

    dividend_candidates = sorted(dividend_candidates, key=lambda x: x["dividend_yield"], reverse=True)[:5]

    return {
        "sector_long":           sector_long,
        "sector_neutral":        sector_neutral,
        "piotroski_candidates":  piotroski_candidates,
        "breakout_signals":      breakout_signals,
        "dividend_candidates":   dividend_candidates,
        "vix_level":             vix_level,
        "vix_signal":            vix_signal,
        "last_sector_ts":        last_sector_ts,
        "last_piotroski_ts":     last_piotroski_ts,
        "last_breakout_ts":      last_breakout_ts,
        "last_dividend_ts":      last_dividend_ts,
    }
