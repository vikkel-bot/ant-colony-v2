"""
ant_colony/dashboard/api.py

Dashboard REST API — leest colony state en serveert JSON aan de frontend.

Endpoints:
  GET  /api/status      — colony status + laatste tick timestamp
  GET  /api/metrics     — kapitaal totalen + actieve agents
  GET  /api/performance — PnL dag/week/maand/jaar/alltime (uit trade logs)
  GET  /api/biomes      — allocatie per biome
  GET  /api/ants        — actieve agents met TTL countdown
  GET  /api/brokers     — broker connecties en ingezet kapitaal
  GET  /api/ticker      — laatste 20 audit log events
  GET  /api/v1status    — heartbeat van Colony v1 (ANT_LIVE/heartbeat.json)
  GET  /api/v1positions — open posities van Colony v1 (ANT_LIVE/live_test/*.json)
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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
    v1_live_root: Path | None = None   # ANT_LIVE root van Colony v1


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
    active_ants: int
    utilization_pct: float


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
    status: str                         # "connected" | "disconnected" | "standby"
    capital_deployed: float
    balance_available: float | None = None   # vrij beschikbaar saldo bij exchange
    balance_in_orders: float | None = None   # vergrendeld in open orders


class BrokersResponse(BaseModel):
    brokers: list[BrokerEntry]


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


class KillSwitchRequest(BaseModel):
    level: int              # 1 = agent, 2 = node, 3 = colony
    scope: str | None = None
    operator_confirm: bool


class KillSwitchResponse(BaseModel):
    executed: bool
    level: int
    scope: str | None
    message: str


class V1StatusResponse(BaseModel):
    ok: bool
    component: str | None = None
    last_heartbeat: str | None = None
    last_status: str | None = None
    lane: str | None = None


class V1PositionEntry(BaseModel):
    symbol: str
    side: str
    entry_price: float
    quantity: float
    current_price: float | None
    unrealized_pnl: float | None
    pnl_pct: float | None
    trigger_high: float | None
    trigger_low: float | None


class V1PositionsResponse(BaseModel):
    positions: list[V1PositionEntry]
    scanned_at: str


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


class AntActivityEntry(BaseModel):
    ant_type:      str
    last_seen:     str | None
    summary:       str
    recent_events: list[str]
    stats:         AntStatsEntry


class AntActivityResponse(BaseModel):
    ants: list[AntActivityEntry]


# ---------------------------------------------------------------------------
# Colony v1 — scan helpers
# ---------------------------------------------------------------------------

_ANT_LIVE_ROOT = Path(r"C:\Trading\ANT_LIVE")
_BITVAVO_TICKER = "https://api.bitvavo.com/v2/{market}/ticker/price"


def _read_broker_artifacts(live_root: Path) -> list[dict]:
    """
    Lees alle LIVE-*.json bestanden uit live_test/broker/.

    Filtert op status=="filled". Dedupliceert op orderId zodat meerdere
    artifacts voor dezelfde order (bijv. per fill) als één trade tellen.
    Retourneert gesorteerd op ts_utc (oud→nieuw) zodat buy/sell pairing
    op volgorde werkt.
    """
    broker_dir = live_root / "live_test" / "broker"
    if not broker_dir.exists():
        return []

    seen_order_ids: set[str] = set()
    records: list[dict] = []

    for path in sorted(broker_dir.glob("LIVE-*.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
            data_block = artifact.get("data") or {}
            raw_block  = data_block.get("raw") or {}

            if raw_block.get("status") != "filled":
                continue

            # Dedupliceer op orderId — meerdere bestanden per order tellen als één.
            order_id = str(raw_block.get("orderId") or raw_block.get("order_id") or "")
            if order_id and order_id in seen_order_ids:
                continue
            if order_id:
                seen_order_ids.add(order_id)

            records.append(artifact)
        except (OSError, json.JSONDecodeError):
            pass

    records.sort(key=lambda r: r.get("ts_utc", ""))
    return records


def _scan_open_positions(live_root: Path) -> list[dict]:
    """
    Groepeer broker artifacts per market en retourneer open posities.

    Een "buy" zonder opvolgende "sell" voor dezelfde market = open positie.
    Een "buy" gevolgd door een "sell" = gesloten — niet tonen.
    """
    artifacts = _read_broker_artifacts(live_root)

    # Stack per market: elke buy pushed een entry, elke sell popt er één.
    stacks: dict[str, list[dict]] = {}
    for artifact in artifacts:
        data = artifact.get("data") or {}
        market = str(data.get("market") or "")
        side   = str(data.get("side") or "").lower()
        if not market or side not in ("buy", "sell"):
            continue

        if side == "buy":
            stacks.setdefault(market, []).append(artifact)
        else:
            if stacks.get(market):
                stacks[market].pop()

    # Wat overblijft in de stacks zijn open posities.
    open_entries: list[dict] = []
    for market, stack in stacks.items():
        for artifact in stack:
            open_entries.append(artifact)

    return open_entries


def _scan_execution_triggers(live_root: Path) -> dict[str, dict]:
    """
    Zoek trigger_high / trigger_low per market in live_test/execution/.

    Veldnamen die herkend worden (in volgorde van voorkeur):
      trigger_high, tp_price, take_profit_price
      trigger_low,  sl_price, stop_loss_price

    Retourneert: {market: {"trigger_high": float|None, "trigger_low": float|None}}
    """
    exec_dir = live_root / "live_test" / "execution"
    if not exec_dir.exists():
        return {}

    _HIGH_KEYS = ("trigger_high", "tp_price", "take_profit_price")
    _LOW_KEYS  = ("trigger_low",  "sl_price", "stop_loss_price")

    def _first_float(d: dict, keys: tuple) -> float | None:
        for k in keys:
            v = d.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    triggers: dict[str, dict] = {}
    for path in sorted(exec_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            market = str(data.get("market") or data.get("symbol") or "")
            if not market:
                continue
            th = _first_float(data, _HIGH_KEYS)
            tl = _first_float(data, _LOW_KEYS)
            if th is not None or tl is not None:
                existing = triggers.get(market, {})
                triggers[market] = {
                    "trigger_high": th if th is not None else existing.get("trigger_high"),
                    "trigger_low":  tl if tl is not None else existing.get("trigger_low"),
                }
        except (OSError, json.JSONDecodeError):
            pass

    return triggers


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
                server_time=now.strftime("%H:%M:%S"),
            )

        status = ctx.scheduler.status.value.upper()
        last_tick = _last_tick_from_logs(ctx.logs_root)

        seconds_ago: float | None = None
        last_tick_str: str | None = None
        if last_tick is not None:
            seconds_ago = round((now - last_tick).total_seconds(), 1)
            last_tick_str = last_tick.strftime("%H:%M:%S")

        return StatusResponse(
            status=status,
            last_tick=last_tick_str,
            seconds_ago=seconds_ago,
            server_time=now.strftime("%H:%M:%S"),
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

        return MetricsResponse(
            capital_total=total,
            capital_allocated=allocated,
            capital_available=available,
            active_ants=active,
            utilization_pct=util,
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
        if ctx.queen is None:
            return BrokersResponse(brokers=[])

        snapshot = ctx.queen.allocation_snapshot()
        _DEFAULT_BROKERS = {
            "crypto": "Bitvavo",
            "equities": "Interactive Brokers",
            "commodities": "Saxo Bank",
        }

        brokers = []
        for b in snapshot.biomes:
            name = ctx.broker_names.get(b.biome_id) or _DEFAULT_BROKERS.get(b.biome_id, b.biome_id)

            # Standaard: status volgt kapitaal-inzet als er geen adapter beschikbaar is.
            # Met adapter overschrijft de werkelijke connectiviteit dit oordeel.
            deployed = b.allocated or 0.0
            status = "connected" if deployed > 0 else "standby"
            balance_available: float | None = None
            balance_in_orders: float | None = None

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
                            else:
                                # API bereikbaar maar auth mislukt (geen keys)
                                status = "connected"
                        else:
                            status = "disconnected"
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
            ))

        return BrokersResponse(brokers=brokers)

    # ------------------------------------------------------------------
    # GET /api/v1status
    # ------------------------------------------------------------------

    @router.get("/v1status", response_model=V1StatusResponse)
    def get_v1status() -> V1StatusResponse:
        """Heartbeat van Colony v1 — leest ANT_LIVE/heartbeat.json."""
        live_root = ctx.v1_live_root or Path(r"C:\Trading\ANT_LIVE")
        hb_path   = live_root / "heartbeat.json"

        if not hb_path.exists():
            return V1StatusResponse(ok=False)

        try:
            data = json.loads(hb_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return V1StatusResponse(ok=False)

        last_hb_str: str | None = data.get("last_heartbeat")
        ok = _v1_heartbeat_ok(last_hb_str)

        return V1StatusResponse(
            ok=ok,
            component=data.get("component"),
            last_heartbeat=last_hb_str,
            last_status=data.get("last_status"),
            lane=data.get("lane"),
        )

    # ------------------------------------------------------------------
    # GET /api/v1positions
    # ------------------------------------------------------------------

    @router.get("/v1positions", response_model=V1PositionsResponse)
    def get_v1positions() -> V1PositionsResponse:
        """Open posities van Colony v1 — broker artifacts + live Bitvavo prijs."""
        now_str   = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        live_root = ctx.v1_live_root or _ANT_LIVE_ROOT

        open_artifacts = _scan_open_positions(live_root)
        trigger_map    = _scan_execution_triggers(live_root)

        entries: list[V1PositionEntry] = []
        for artifact in open_artifacts:
            data_block = artifact.get("data") or {}
            raw_block  = data_block.get("raw") or {}

            market   = str(data_block.get("market") or "")
            raw_side = str(data_block.get("side") or "").lower()
            side     = "long" if raw_side == "buy" else "short"

            # entry_price: eerste fill, anders price veld
            fills = raw_block.get("fills") or []
            if fills and fills[0].get("price") is not None:
                entry_price = float(fills[0]["price"])
            else:
                entry_price = float(raw_block.get("price") or 0)

            quantity = float(raw_block.get("filledAmount") or 0)

            trig     = trigger_map.get(market, {})
            trigger_high = trig.get("trigger_high")
            trigger_low  = trig.get("trigger_low")

            current_price = _get_live_price(market, ctx.biome_registry) if market else None

            unrealized_pnl: float | None = None
            pnl_pct:        float | None = None
            if current_price is not None and entry_price > 0 and quantity > 0:
                if side == "long":
                    unrealized_pnl = round((current_price - entry_price) * quantity, 2)
                else:
                    unrealized_pnl = round((entry_price - current_price) * quantity, 2)
                pnl_pct = round(unrealized_pnl / (entry_price * quantity) * 100, 2)

            entries.append(V1PositionEntry(
                symbol=market,
                side=side,
                entry_price=entry_price,
                quantity=quantity,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                pnl_pct=pnl_pct,
                trigger_high=trigger_high,
                trigger_low=trigger_low,
            ))

        return V1PositionsResponse(positions=entries, scanned_at=now_str)

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
                last_rec  = records[-1]
                last_ts   = _parse_ts(last_rec.get("timestamp"))
                last_seen = last_ts.strftime("%H:%M:%S") if last_ts else None

                recent_events = [_event_short(r) for r in records[-3:]]

                today_recs = [
                    r for r in records
                    if (_parse_ts(r.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= today
                ]

                stats   = _build_ant_stats(ant_type, today_recs)
                summary = _build_ant_summary(ant_type, records, stats)

            result.append(AntActivityEntry(
                ant_type=ant_type,
                last_seen=last_seen,
                summary=summary,
                recent_events=recent_events,
                stats=stats,
            ))

        return AntActivityResponse(ants=result)

    return router


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
            timestamp=ts.strftime("%H:%M:%S"),
            mission_id=rec.get("mission_id"),
            payload=rec.get("payload", {}),
        ))
    return events


def _v1_heartbeat_ok(last_heartbeat_str: str | None, max_age_seconds: float = 300.0) -> bool:
    """True als heartbeat bestaat en niet ouder is dan max_age_seconds (standaard 5 min)."""
    if not last_heartbeat_str:
        return False
    try:
        dt = datetime.fromisoformat(last_heartbeat_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - dt).total_seconds() <= max_age_seconds
    except (ValueError, TypeError):
        return False


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


def _event_short(rec: dict) -> str:
    """Formatteer een log record als korte event string voor de tijdlijn."""
    ts     = _parse_ts(rec.get("timestamp"))
    ts_str = ts.strftime("%H:%M:%S") if ts else "?"
    action = _action_of(rec).replace("_", " ")
    symbol = (rec.get("payload") or {}).get("symbol") or ""
    parts  = [ts_str, action]
    if symbol:
        parts.append(symbol)
    return " · ".join(parts)


def _count_actions(recs: list[dict], *keywords: str) -> int:
    return sum(1 for r in recs if any(kw in _action_of(r) for kw in keywords))


def _build_ant_stats(ant_type: str, today_recs: list[dict]) -> AntStatsEntry:
    if ant_type == "scout_ant":
        return AntStatsEntry(
            signals_found=_count_actions(today_recs, "signal", "opportunity", "detected"),
        )
    if ant_type == "research_ant":
        return AntStatsEntry(
            candidates_above_threshold=_count_actions(today_recs, "candidate_emitted", "emitted"),
        )
    if ant_type == "paper_ant":
        opened      = _count_actions(today_recs, "trade_opened", "position_opened")
        closed_recs = [r for r in today_recs if _action_of(r) in ("trade_closed", "position_closed")]
        pnl_vals    = [float((r.get("payload") or {}).get("realized_pnl") or 0) for r in closed_recs]
        total_pnl   = round(sum(pnl_vals), 2)
        wins        = sum(1 for v in pnl_vals if v > 0)
        win_rate    = round(wins / len(pnl_vals), 2) if pnl_vals else None
        return AntStatsEntry(
            open_trades=max(0, opened - len(closed_recs)),
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
        return f"{n} kandidaten geëmit vandaag — laatste: {action}"
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
    return f"Laatste actie: {action}"
