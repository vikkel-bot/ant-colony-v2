"""
tests/test_audit_ant.py

AuditAnt — gedrag zonder echte filesystem-toegang op C:\\Trading.

Scenarios:
  1.  Scheduler stale (>2× tick_interval)  → ANOMALY gelogd
  2.  Scheduler vers                        → INFO gelogd
  3.  Scheduler log ontbreekt               → WARNING gelogd, geen crash
  4.  Scheduler log leeg                    → WARNING gelogd, geen crash
  5.  Sequence gap in JSONL                 → ANOMALY gelogd
  6.  Aaneengesloten sequences              → geen ANOMALY
  7.  Minder dan 2 sequence-regels          → geen gap detectie
  8.  Eigen audit-log overgeslagen          → geen zelfdetectie
  9.  BUY-artifact ouder dan max_age        → WARNING gelogd
  10. BUY-artifact vers                     → geen WARNING
  11. SELL-artifact (oud) → niet gedetecteerd als open positie
  12. Broker dir ontbreekt                  → geen crash
  13. V1 heartbeat oud (>10 min)            → WARNING gelogd
  14. V1 heartbeat vers                     → INFO gelogd
  15. V1 heartbeat.json ontbreekt           → geen crash, geen finding
  16. V1 heartbeat zonder timestamp veld    → WARNING gelogd
  17. Heartbeat gerapporteerd na tick
  18. Heartbeat-fout propageert niet
  19. TTL verlopen                          → AntStatus.COMPLETED
  20. Finale heartbeat bij exit
  21. Log geschreven naar ANT_LOGS/audit/
  22. Logrecord bevat vereiste velden
  23. logs_root=None                        → geen crash
  24. _find_sequence_gaps hulpfunctie direct getest
  25. _read_last_nonempty_line hulpfunctie
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ant_colony.ants.audit_ant import (
    AuditAnt,
    AuditFinding,
    _find_sequence_gaps,
    _read_last_nonempty_line,
)
from ant_colony.biome.biome_registry import BiomeRegistry
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.mission import (
    AbortConditions,
    MarketScope,
    Mission,
    RiskLimits,
    SuccessConditions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_mission(*, ttl: int = 86400, heartbeat_interval: int = 300) -> Mission:
    return Mission(
        mission_id="m-audit-001",
        ant_type="audit_ant",
        allowed_node="pc2-desktop",
        allowed_actions=["read_data", "validate", "report"],
        market_scope=MarketScope(
            biome="crypto",
            symbols=["BTC-EUR"],
            timeframes=["1h"],
        ),
        capital_limit=0.0,
        risk_limits=RiskLimits(
            max_drawdown_pct=0.01,
            max_position_size=1.0,
            daily_loss_limit=1.0,
            stop_loss_required=False,
        ),
        ttl=ttl,
        heartbeat_interval=heartbeat_interval,
        success_conditions=SuccessConditions(description="Audit missie"),
        abort_conditions=AbortConditions(),
    )


def make_ant(
    tmp_path: Path,
    *,
    mission: Mission | None = None,
    scheduler: MagicMock | None = None,
    scheduler_tick_interval: int = 5,
) -> AuditAnt:
    return AuditAnt(
        ant_id="ant-audit-test-0001",
        mission=mission or make_mission(),
        scheduler=scheduler or MagicMock(),
        biome_registry=BiomeRegistry(),
        logs_root=tmp_path,
        scheduler_tick_interval=scheduler_tick_interval,
    )


def log_path(tmp_path: Path, ant: AuditAnt) -> Path:
    return tmp_path / "audit" / f"{ant.ant_id}.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def findings_with(records: list[dict], *, check_name: str | None = None,
                  severity: str | None = None) -> list[dict]:
    result = records
    if check_name:
        result = [r for r in result if r["payload"].get("check_name") == check_name]
    if severity:
        result = [r for r in result if r["payload"].get("severity") == severity]
    return result


def write_scheduler_log(logs_root: Path, timestamp: datetime, seq: int = 0) -> None:
    """Schrijf een minimale scheduler tick naar colony/scheduler.jsonl."""
    p = logs_root / "colony" / "scheduler.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"event_type": "tick", "sequence": seq, "timestamp": timestamp.isoformat()}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def write_jsonl_with_sequences(path: Path, sequences: list[int]) -> None:
    """Schrijf een JSONL-bestand met de opgegeven sequence-nummers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for seq in sequences:
            fh.write(json.dumps({"sequence": seq, "event_type": "action_executed"}) + "\n")


# ---------------------------------------------------------------------------
# Hulpfuncties direct getest
# ---------------------------------------------------------------------------

class TestReadLastNonemptyLine:
    def test_returns_last_line(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        assert _read_last_nonempty_line(p) == '{"b": 2}'

    def test_ignores_trailing_blank_lines(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a": 1}\n\n\n', encoding="utf-8")
        assert _read_last_nonempty_line(p) == '{"a": 1}'

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        assert _read_last_nonempty_line(p) is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert _read_last_nonempty_line(tmp_path / "missing.jsonl") is None


class TestFindSequenceGaps:
    def test_contiguous_no_gaps(self, tmp_path):
        p = tmp_path / "log.jsonl"
        write_jsonl_with_sequences(p, [0, 1, 2, 3, 4])
        assert _find_sequence_gaps(p) == []

    def test_single_gap_detected(self, tmp_path):
        p = tmp_path / "log.jsonl"
        write_jsonl_with_sequences(p, [0, 1, 3, 4])
        gaps = _find_sequence_gaps(p)
        assert len(gaps) == 1
        assert gaps[0] == (1, 3)

    def test_multiple_gaps(self, tmp_path):
        p = tmp_path / "log.jsonl"
        write_jsonl_with_sequences(p, [0, 2, 5])
        gaps = _find_sequence_gaps(p)
        assert (0, 2) in gaps
        assert (2, 5) in gaps

    def test_fewer_than_2_entries_no_gaps(self, tmp_path):
        p = tmp_path / "log.jsonl"
        write_jsonl_with_sequences(p, [0])
        assert _find_sequence_gaps(p) == []

    def test_no_sequence_fields_no_gaps(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text('{"event_type": "tick"}\n{"event_type": "tick"}\n', encoding="utf-8")
        assert _find_sequence_gaps(p) == []

    def test_missing_file_returns_empty(self, tmp_path):
        assert _find_sequence_gaps(tmp_path / "missing.jsonl") == []

    def test_unsorted_sequences_still_detected(self, tmp_path):
        p = tmp_path / "log.jsonl"
        write_jsonl_with_sequences(p, [3, 0, 1])  # gap at 1→3 after sorting
        gaps = _find_sequence_gaps(p)
        assert (1, 3) in gaps


# ---------------------------------------------------------------------------
# 1–4  Scheduler heartbeat check
# ---------------------------------------------------------------------------

class TestSchedulerHeartbeat:
    def test_stale_scheduler_emits_anomaly(self, tmp_path):
        """Scenario 1: laatste tick ouder dan 2× tick_interval → ANOMALY."""
        stale_ts = _NOW - timedelta(seconds=100)
        write_scheduler_log(tmp_path, stale_ts)
        ant = make_ant(tmp_path, scheduler_tick_interval=5)

        with patch("ant_colony.ants.audit_ant.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            findings = ant._check_scheduler_heartbeat()

        assert any(f.severity == "ANOMALY" for f in findings)
        assert any(f.check_name == "scheduler_heartbeat" for f in findings)

    def test_fresh_scheduler_emits_info(self, tmp_path):
        """Scenario 2: verse tick → INFO."""
        fresh_ts = _NOW - timedelta(seconds=3)
        write_scheduler_log(tmp_path, fresh_ts)
        ant = make_ant(tmp_path, scheduler_tick_interval=5)

        with patch("ant_colony.ants.audit_ant.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            findings = ant._check_scheduler_heartbeat()

        assert any(f.severity == "INFO" for f in findings)

    def test_missing_scheduler_log_emits_warning(self, tmp_path):
        """Scenario 3: logbestand ontbreekt → WARNING, geen crash."""
        ant = make_ant(tmp_path)
        findings = ant._check_scheduler_heartbeat()
        assert any(f.severity == "WARNING" for f in findings)

    def test_empty_scheduler_log_emits_warning(self, tmp_path):
        """Scenario 4: leeg logbestand → WARNING."""
        p = tmp_path / "colony" / "scheduler.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        ant = make_ant(tmp_path)
        findings = ant._check_scheduler_heartbeat()
        assert any(f.severity == "WARNING" for f in findings)

    def test_no_logs_root_returns_empty(self, tmp_path):
        """logs_root=None → lege lijst, geen crash."""
        ant = AuditAnt(
            ant_id="x", mission=make_mission(), scheduler=MagicMock(),
            biome_registry=BiomeRegistry(), logs_root=None,
        )
        assert ant._check_scheduler_heartbeat() == []


# ---------------------------------------------------------------------------
# 5–8  Audit trail integriteit
# ---------------------------------------------------------------------------

class TestAuditTrailIntegrity:
    def test_gap_in_jsonl_emits_anomaly(self, tmp_path):
        """Scenario 5: sequence gap → ANOMALY."""
        p = tmp_path / "scouts" / "ant-x.jsonl"
        write_jsonl_with_sequences(p, [0, 1, 3])  # gap bij 1→3
        ant = make_ant(tmp_path)

        findings = ant._check_audit_trail_integrity()

        assert any(f.severity == "ANOMALY" and f.check_name == "sequence_gap" for f in findings)

    def test_contiguous_sequences_no_anomaly(self, tmp_path):
        """Scenario 6: aaneengesloten sequences → geen ANOMALY."""
        p = tmp_path / "scouts" / "ant-x.jsonl"
        write_jsonl_with_sequences(p, [0, 1, 2, 3, 4])
        ant = make_ant(tmp_path)

        findings = ant._check_audit_trail_integrity()

        assert not any(f.severity == "ANOMALY" for f in findings)

    def test_single_sequence_entry_no_gap(self, tmp_path):
        """Scenario 7: één sequence-regel → geen gap mogelijk."""
        p = tmp_path / "scouts" / "ant-x.jsonl"
        write_jsonl_with_sequences(p, [0])
        ant = make_ant(tmp_path)

        findings = ant._check_audit_trail_integrity()

        assert not any(f.check_name == "sequence_gap" for f in findings)

    def test_own_audit_log_skipped(self, tmp_path):
        """Scenario 8: eigen audit-logbestand wordt niet gescand."""
        ant = make_ant(tmp_path)

        # Schrijf een gat in het EIGEN audit-logbestand
        own_log = tmp_path / "audit" / f"{ant.ant_id}.jsonl"
        write_jsonl_with_sequences(own_log, [0, 5])  # groot gat

        findings = ant._check_audit_trail_integrity()

        assert not any(f.check_name == "sequence_gap" for f in findings)

    def test_gap_detail_contains_filename(self, tmp_path):
        """Gap-detail vermeldt het bestandspad."""
        p = tmp_path / "research" / "ant-r.jsonl"
        write_jsonl_with_sequences(p, [0, 2])
        ant = make_ant(tmp_path)

        findings = ant._check_audit_trail_integrity()

        assert any("ant-r.jsonl" in f.detail for f in findings)

    def test_no_logs_root_returns_empty(self, tmp_path):
        ant = AuditAnt(
            ant_id="x", mission=make_mission(), scheduler=MagicMock(),
            biome_registry=BiomeRegistry(), logs_root=None,
        )
        assert ant._check_audit_trail_integrity() == []


# ---------------------------------------------------------------------------
# 9–10  Heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeat:
    def test_send_heartbeat_calls_scheduler(self, tmp_path):
        """Scenario 17: heartbeat gerapporteerd aan scheduler."""
        scheduler = MagicMock()
        ant = make_ant(tmp_path, scheduler=scheduler)
        ant._status = AntStatus.RUNNING

        ant._send_heartbeat()

        scheduler.record_heartbeat.assert_called_once()

    def test_heartbeat_contains_correct_ids(self, tmp_path):
        scheduler = MagicMock()
        mission = make_mission()
        ant = make_ant(tmp_path, mission=mission, scheduler=scheduler)
        ant._status = AntStatus.RUNNING

        ant._send_heartbeat()

        hb = scheduler.record_heartbeat.call_args[0][0]
        assert hb.ant_id == ant.ant_id
        assert hb.mission_id == mission.mission_id

    def test_heartbeat_exception_does_not_propagate(self, tmp_path):
        """Scenario 18: heartbeat-fout propageert niet."""
        scheduler = MagicMock()
        scheduler.record_heartbeat.side_effect = RuntimeError("scheduler down")
        ant = make_ant(tmp_path, scheduler=scheduler)

        ant._send_heartbeat()  # mag niet raisen

    def test_run_sends_heartbeat_after_interval(self, tmp_path):
        scheduler = MagicMock()
        mission = make_mission(ttl=300, heartbeat_interval=10)
        ant = make_ant(tmp_path, mission=mission, scheduler=scheduler)

        base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with patch("ant_colony.ants.audit_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.audit_ant.time") as mock_time:
            mock_dt.now.return_value = base
            # Eerste aanroep started_at, daarna escaleer naar TTL
            calls = [base, base + timedelta(seconds=11), base + timedelta(seconds=11),
                     base + timedelta(seconds=301)]
            mock_dt.now.side_effect = calls
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            mock_time.sleep = MagicMock()
            ant.run()

        assert scheduler.record_heartbeat.call_count >= 1


# ---------------------------------------------------------------------------
# 19–20  TTL expiry
# ---------------------------------------------------------------------------

class TestTTLExpiry:
    def test_run_returns_completed_when_ttl_expires(self, tmp_path):
        """Scenario 19: TTL verlopen → COMPLETED."""
        mission = make_mission(ttl=10, heartbeat_interval=5)
        ant = make_ant(tmp_path, mission=mission)

        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch("ant_colony.ants.audit_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.audit_ant.time") as mock_time:
            mock_dt.now.side_effect = [
                base,
                base + timedelta(seconds=1),
                base + timedelta(seconds=11),
            ]
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            mock_time.sleep = MagicMock()
            status = ant.run()

        assert status == AntStatus.COMPLETED

    def test_run_sends_final_heartbeat_on_exit(self, tmp_path):
        """Scenario 20: finally-blok stuurt altijd een heartbeat."""
        scheduler = MagicMock()
        mission = make_mission(ttl=10, heartbeat_interval=5)
        ant = make_ant(tmp_path, mission=mission, scheduler=scheduler)

        base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch("ant_colony.ants.audit_ant.datetime") as mock_dt, \
             patch("ant_colony.ants.audit_ant.time") as mock_time:
            mock_dt.now.side_effect = [base, base + timedelta(seconds=11)]
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            mock_time.sleep = MagicMock()
            ant.run()

        assert scheduler.record_heartbeat.called

    def test_status_idle_before_run(self, tmp_path):
        ant = make_ant(tmp_path)
        assert ant._status == AntStatus.IDLE


# ---------------------------------------------------------------------------
# 21–23  Audit log
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_finding_written_to_audit_subdir(self, tmp_path):
        """Scenario 21: bevinding geschreven naar ANT_LOGS/audit/."""
        ant = make_ant(tmp_path)
        finding = AuditFinding(
            severity="WARNING",
            component="test",
            check_name="test_check",
            detail="test detail",
        )
        ant._emit(finding)

        assert log_path(tmp_path, ant).exists()

    def test_log_record_is_valid_json(self, tmp_path):
        ant = make_ant(tmp_path)
        ant._emit(AuditFinding("INFO", "test", "test_check", "ok"))

        records = read_jsonl(log_path(tmp_path, ant))
        assert len(records) == 1

    def test_log_record_has_required_fields(self, tmp_path):
        """Scenario 22: logrecord bevat event_id, timestamp, source, payload."""
        ant = make_ant(tmp_path)
        ant._emit(AuditFinding("ANOMALY", "scheduler", "scheduler_heartbeat", "stale"))

        record = read_jsonl(log_path(tmp_path, ant))[0]
        for field in ("event_id", "timestamp", "source", "mission_id",
                      "sequence", "payload"):
            assert field in record, f"Veld '{field}' ontbreekt"

    def test_payload_contains_finding_fields(self, tmp_path):
        ant = make_ant(tmp_path)
        ant._emit(AuditFinding("WARNING", "positions", "position_age", "BTC 10 days old"))

        payload = read_jsonl(log_path(tmp_path, ant))[0]["payload"]
        assert payload["severity"] == "WARNING"
        assert payload["component"] == "positions"
        assert payload["check_name"] == "position_age"
        assert "BTC 10 days old" in payload["detail"]

    def test_source_field_is_ant_id(self, tmp_path):
        ant = make_ant(tmp_path)
        ant._emit(AuditFinding("INFO", "test", "test_check", "ok"))

        record = read_jsonl(log_path(tmp_path, ant))[0]
        assert record["source"] == ant.ant_id

    def test_sequence_increments(self, tmp_path):
        ant = make_ant(tmp_path)
        ant._emit(AuditFinding("INFO", "a", "c1", "d"))
        ant._emit(AuditFinding("INFO", "b", "c2", "d"))

        records = read_jsonl(log_path(tmp_path, ant))
        assert records[0]["sequence"] == 0
        assert records[1]["sequence"] == 1


# ---------------------------------------------------------------------------
# 26–29  Sequence-gap deduplicatie en 24u-bestandsfilter
# ---------------------------------------------------------------------------

import os
import time as _time_module


def _write_jsonl_with_gap(path: Path, sequences: list[int]) -> None:
    """Schrijf JSONL met opgegeven sequence-nummers (gesimuleerde gap)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for seq in sequences:
            fh.write(json.dumps({"sequence": seq, "event_type": "action_executed"}) + "\n")


class TestSequenceGapDeduplicatieEn24uFilter:
    def test_gap_maar_een_keer_gerapporteerd(self, tmp_path):
        """Scenario 26: dezelfde gap wordt slechts één keer als finding teruggegeven."""
        log_file = tmp_path / "paper" / "ant-paper.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6])  # gap 1→5

        ant = make_ant(tmp_path)

        # Eerste tick → gap gevonden
        findings1 = ant._check_audit_trail_integrity()
        gap_findings1 = [f for f in findings1 if f.check_name == "sequence_gap"]
        assert len(gap_findings1) == 1

        # Tweede tick → gap al in _reported_gaps → niet opnieuw gerapporteerd
        findings2 = ant._check_audit_trail_integrity()
        gap_findings2 = [f for f in findings2 if f.check_name == "sequence_gap"]
        assert len(gap_findings2) == 0

    def test_meerdere_gaps_allemaal_eenmalig(self, tmp_path):
        """Scenario 27: twee gaps in één bestand worden elk maar één keer gerapporteerd."""
        log_file = tmp_path / "scout" / "ant-scout.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6, 10, 11])  # gap 1→5 en 6→10

        ant = make_ant(tmp_path)

        findings1 = ant._check_audit_trail_integrity()
        gap_findings1 = [f for f in findings1 if f.check_name == "sequence_gap"]
        assert len(gap_findings1) == 2

        findings2 = ant._check_audit_trail_integrity()
        gap_findings2 = [f for f in findings2 if f.check_name == "sequence_gap"]
        assert len(gap_findings2) == 0

    def test_oud_bestand_overgeslagen(self, tmp_path):
        """Scenario 28: JSONL-bestand ouder dan 24u wordt niet gescand op gaps."""
        log_file = tmp_path / "paper" / "ant-old.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6])  # gap aanwezig

        # Zet mtime naar 25 uur geleden
        old_mtime = _time_module.time() - 25 * 3600
        os.utime(log_file, (old_mtime, old_mtime))

        ant = make_ant(tmp_path)
        findings = ant._check_audit_trail_integrity()
        gap_findings = [f for f in findings if f.check_name == "sequence_gap"]
        assert len(gap_findings) == 0

    def test_recent_bestand_wel_gescand(self, tmp_path):
        """Scenario 29: JSONL-bestand jonger dan 24u wordt wél gescand op gaps."""
        log_file = tmp_path / "paper" / "ant-recent.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6])  # gap aanwezig

        # Bestand is recent (standaard mtime = nu)
        ant = make_ant(tmp_path)
        findings = ant._check_audit_trail_integrity()
        gap_findings = [f for f in findings if f.check_name == "sequence_gap"]
        assert len(gap_findings) == 1


# ---------------------------------------------------------------------------
# 30–34  Gap-state persistentie over sessies
# ---------------------------------------------------------------------------

class TestGapStatePersistentie:
    def test_gap_persisteert_naar_disk(self, tmp_path):
        """Scenario 30: Na eerste melding wordt gap geschreven naar reported_gaps.json."""
        log_file = tmp_path / "paper" / "ant-paper.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6])

        ant = make_ant(tmp_path)
        ant._check_audit_trail_integrity()

        state_path = tmp_path / "audit_state" / "reported_gaps.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_gap_niet_opnieuw_na_herstart(self, tmp_path):
        """Scenario 31: Na herstart van AuditAnt worden bestaande gaps niet opnieuw gemeld."""
        log_file = tmp_path / "paper" / "ant-paper.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6])

        # Eerste sessie: gap wordt gevonden en gepersisteerd
        ant1 = make_ant(tmp_path)
        findings1 = ant1._check_audit_trail_integrity()
        assert len([f for f in findings1 if f.check_name == "sequence_gap"]) == 1

        # Tweede sessie (nieuwe instantie = herstart): gap staat al in state
        ant2 = make_ant(tmp_path)
        findings2 = ant2._check_audit_trail_integrity()
        assert len([f for f in findings2 if f.check_name == "sequence_gap"]) == 0

    def test_nieuwe_gap_wel_gemeld_na_herstart(self, tmp_path):
        """Scenario 32: Nieuwe gap in tweede sessie wordt wél gemeld."""
        log_file = tmp_path / "paper" / "ant-paper.jsonl"
        _write_jsonl_with_gap(log_file, [0, 1, 5, 6])

        ant1 = make_ant(tmp_path)
        ant1._check_audit_trail_integrity()

        # Voeg tweede bestand met nieuwe gap toe
        log_file2 = tmp_path / "scout" / "ant-scout.jsonl"
        _write_jsonl_with_gap(log_file2, [0, 1, 10, 11])

        ant2 = make_ant(tmp_path)
        findings2 = ant2._check_audit_trail_integrity()
        gap_findings = [f for f in findings2 if f.check_name == "sequence_gap"]
        assert len(gap_findings) == 1  # alleen de nieuwe gap

    def test_geen_state_file_bij_logs_root_none(self):
        """Scenario 33: logs_root=None → geen crash, geen state file."""
        mission = make_mission()
        ant = AuditAnt(
            ant_id="ant-audit-no-root",
            mission=mission,
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=None,
        )
        assert ant._gaps_state_path is None
        findings = ant._check_audit_trail_integrity()
        assert findings == []

    def test_state_file_geladen_bij_init(self, tmp_path):
        """Scenario 34: Bij init wordt bestaande state geladen, niet overschreven."""
        state_path = tmp_path / "audit_state" / "reported_gaps.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        pre_existing_key = "some/file.jsonl:3:7"
        state_path.write_text(json.dumps([pre_existing_key]))

        ant = make_ant(tmp_path)
        assert pre_existing_key in ant._reported_gaps

    def test_no_crash_with_logs_root_none(self, tmp_path):
        """Scenario 23: logs_root=None → geen crash, geen bestand."""
        ant = AuditAnt(
            ant_id="ant-nolog",
            mission=make_mission(),
            scheduler=MagicMock(),
            biome_registry=BiomeRegistry(),
            logs_root=None,
        )
        ant._emit(AuditFinding("WARNING", "test", "test_check", "detail"))

    def test_full_tick_writes_findings(self, tmp_path):
        """_tick() voert alle checks uit en schrijft bevindingen naar disk."""
        # Maak een stale scheduler log aan → ANOMALY verwacht
        stale_ts = _NOW - timedelta(seconds=200)
        write_scheduler_log(tmp_path, stale_ts)
        ant = make_ant(tmp_path, scheduler_tick_interval=5)

        with patch("ant_colony.ants.audit_ant.datetime") as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            ant._tick()

        records = read_jsonl(log_path(tmp_path, ant))
        assert len(records) >= 1
        anomalies = findings_with(records, check_name="scheduler_heartbeat", severity="ANOMALY")
        assert len(anomalies) >= 1
