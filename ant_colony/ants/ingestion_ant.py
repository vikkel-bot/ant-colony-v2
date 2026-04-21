"""
ant_colony/ants/ingestion_ant.py

IngestionAnt — doorzoekt publieke bronnen naar trading strategieën en
normaliseert ze naar het interne StrategyCandidate schema.

Verantwoordelijkheden:
  1. GitHub public search API doorzoeken op vaste zoektermen.
     Maximaal 10 resultaten per zoekterm per tick.
  2. Reddit r/algotrading top posts van de week ophalen.
  3. Dev.to artikelen met tag "trading" ophalen.
  4. Per bron kwaliteitsfilter op keywords.
  5. Geaccepteerde kandidaten normaliseren naar StrategyCandidate
     (status=INGESTED, provenance ingevuld, nog niet gebacktest).
  6. Kandidaten loggen naar ANT_LOGS/ingestion/{ant_id}.jsonl.
  7. URL-hashes bijhouden om duplicaten te voorkomen — ook over ticks.
  8. Rate limiting per bron: GitHub 10s, Reddit 30s, Dev.to 30s.
  9. Heartbeat rapporteren aan scheduler na elke tick.
  10. Zichzelf netjes beëindigen bij TTL expiry.

Regels:
  - Plaatst geen orders, beheert geen kapitaal (P1)
  - Gooit nooit een exception naar buiten (fail-closed P2)
  - Alle state leeft in het object — geen globals (P7)
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ant_colony.ants._heartbeat import HeartbeatThread
from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.ant import AntStatus
from ant_colony.schemas.audit_event import AuditEvent, AuditEventType
from ant_colony.schemas.heartbeat import Heartbeat, HeartbeatStatus
from ant_colony.schemas.mission import Mission
from ant_colony.schemas.strategy_candidate import (
    CandidateStatus,
    ProvenanceEntry,
    StrategyCandidate,
)

_GITHUB_API_BASE     = "https://api.github.com"
_REDDIT_TOP_URL      = "https://www.reddit.com/r/algotrading/top.json?t=week&limit=10"
_DEVTO_ARTICLES_URL  = "https://dev.to/api/articles?tag=trading&per_page=10"

_SEARCH_TERMS        = [
    "trading strategy",
    "crypto bot",
    "mean reversion",
    "momentum strategy",
    "breakout strategy crypto",
    "trend following bot",
    "scalping strategy python",
    "arbitrage crypto bot",
]
_RESULTS_PER_TERM    = 10
_MIN_STARS           = 10

# Per-bron rate limits (seconden tussen calls)
_RATE_LIMITS: dict[str, float] = {
    "github": 10.0,
    "reddit": 30.0,
    "devto":  30.0,
}
_RATE_LIMIT_BACKOFF  = 60.0   # wacht 60s bij 403/429 rate limit response
_README_MAX_BYTES    = 65_536  # 64 KB — genoeg voor keyword-scan

# Keywords voor Reddit/Dev.to kwaliteitsfilter
_REDDIT_KEYWORDS: frozenset[str] = frozenset({"strategy", "backtest", "bot", "edge"})

_ENTRY_KEYWORDS: frozenset[str] = frozenset({
    "entry", "buy signal", "long", "crossover", "breakout",
    "momentum", "mean reversion", "rsi", "macd", "ema", "sma",
    "bollinger", "indicator", "oversold", "overbought", "signal",
})
_EXIT_KEYWORDS: frozenset[str] = frozenset({
    "exit", "sell", "stop loss", "stop_loss", "take profit", "take_profit",
    "trailing stop", "trailing_stop", "close position",
})

_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class IngestionAnt:
    """
    Doorzoekt GitHub naar publieke trading-strategie repos en normaliseert
    ze naar StrategyCandidate (status=INGESTED).

    Args:
        ant_id:       Unieke identifier (UUID-string).
        mission:      Toegewezen Mission.
        scheduler:    ColonyScheduler voor heartbeat-registratie.
        logs_root:    Pad naar ANT_LOGS. None = geen disk-logging.
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None = None,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root

        self._seen_urls: set[str] = set()
        self._last_call: dict[str, float] = {"github": 0.0, "reddit": 0.0, "devto": 0.0}
        self._log_seq: int = 0
        self._status: AntStatus = AntStatus.IDLE
        self._last_action: str = "init"

        self._log = logging.getLogger(f"ant.ingestion.{ant_id[:8]}")

        if self.logs_root is not None:
            log_dir = self.logs_root / "ingestion"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._log.info("Logs map: %s", log_dir)

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> AntStatus:
        """
        Blokkerende tick-loop. Retourneert AntStatus bij afsluiting.

        Elke tick:
          1. TTL controleren
          2. GitHub doorzoeken en kandidaten inladen
          3. Heartbeat sturen
          4. Wachten tot volgende tick
        """
        self._status = AntStatus.RUNNING
        self._log.info(
            "IngestionAnt gestart | mission=%s ttl=%ds",
            self.mission.mission_id,
            self.mission.ttl,
        )

        started_at = datetime.now(tz=timezone.utc)

        _hb = HeartbeatThread(self, self.mission.heartbeat_interval)
        _hb.start()

        try:
            while self._status == AntStatus.RUNNING:
                now     = datetime.now(tz=timezone.utc)
                elapsed = (now - started_at).total_seconds()

                if elapsed >= self.mission.ttl:
                    self._log.info(
                        "TTL verlopen (%.1fs / %ds) — afsluiten", elapsed, self.mission.ttl
                    )
                    self._status = AntStatus.COMPLETED
                    break

                self._tick()

                time.sleep(1.0)

        except KeyboardInterrupt:
            self._log.info("IngestionAnt onderbroken door operator")
            self._status = AntStatus.ABORTED
        except Exception:
            self._log.exception("Onverwachte fout in tick-loop")
            self._status = AntStatus.ABORTED
        finally:
            _hb.stop()
            self._send_heartbeat()
            self._log.info("IngestionAnt gestopt | status=%s", self._status.value)

        return self._status

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Één ingestion-cyclus: alle bronnen doorlopen."""
        for term in _SEARCH_TERMS:
            repos = self._search_github(term)
            for repo in repos:
                self._process_repo(repo)

        for post in self._fetch_reddit():
            self._process_reddit_post(post)

        for article in self._fetch_devto():
            self._process_devto_article(article)

        self._last_action = "tick"

    def _process_repo(self, repo: dict) -> None:
        """Verwerk één repo: filter, README lezen, kandidaat bouwen en loggen."""
        stars = repo.get("stargazers_count", 0)
        if stars < _MIN_STARS:
            self._log.debug(
                "Repo %s overgeslagen — te weinig stars (%d < %d)",
                repo.get("full_name"), stars, _MIN_STARS,
            )
            return

        url = repo.get("html_url", "")
        if not url:
            return
        if url in self._seen_urls:
            self._log.debug("Duplicaat overgeslagen: %s", url)
            return
        self._seen_urls.add(url)

        readme = self._fetch_readme(repo)
        if readme is None:
            self._log.debug("Geen README voor %s — overgeslagen", repo.get("full_name"))
            return

        extracted = self._extract_strategy(readme, repo)
        if extracted is None:
            self._log.debug(
                "Geen herkenbare strategie in %s — overgeslagen", repo.get("full_name")
            )
            return

        candidate = self._build_candidate(repo, extracted)
        if candidate is None:
            return

        self._write_candidate(candidate)
        self._last_action = f"ingested:{repo.get('full_name', url)}"
        self._log.info(
            "KANDIDAAT INGESTED | %s  stars=%d  entry=%s  exit=%s",
            repo.get("full_name"), stars,
            extracted["entry_keywords"][:3],
            extracted["exit_keywords"][:2],
        )

    # ------------------------------------------------------------------
    # GitHub API
    # ------------------------------------------------------------------

    def _search_github(self, term: str) -> list[dict]:
        """Zoek repos op GitHub. Retourneert lijst van repo-dicts (leeg bij fout)."""
        self._rate_limit("github")
        try:
            resp = httpx.get(
                f"{_GITHUB_API_BASE}/search/repositories",
                params={
                    "q":        term,
                    "sort":     "stars",
                    "order":    "desc",
                    "per_page": _RESULTS_PER_TERM,
                },
                headers=_GITHUB_HEADERS,
                timeout=15.0,
            )
            if resp.status_code == 403:
                self._log.warning(
                    "GitHub rate limit (403) voor term '%s' — backoff %.0fs", term, _RATE_LIMIT_BACKOFF
                )
                time.sleep(_RATE_LIMIT_BACKOFF)
                return []
            if resp.status_code != 200:
                self._log.warning(
                    "GitHub search HTTP %d voor term '%s'", resp.status_code, term
                )
                return []
            return resp.json().get("items", [])
        except Exception:
            self._log.exception("GitHub search mislukt voor term: %s", term)
            return []

    def _fetch_readme(self, repo: dict) -> str | None:
        """Haal README op en decodeer van base64. Retourneert tekst of None."""
        full_name = repo.get("full_name", "")
        if not full_name:
            return None
        self._rate_limit("github")
        try:
            resp = httpx.get(
                f"{_GITHUB_API_BASE}/repos/{full_name}/readme",
                headers=_GITHUB_HEADERS,
                timeout=15.0,
            )
            if resp.status_code != 200:
                return None
            raw_content = resp.json().get("content", "")
            if not raw_content:
                return None
            decoded = base64.b64decode(
                raw_content.replace("\n", "").encode("ascii")
            ).decode("utf-8", errors="replace")
            return decoded[:_README_MAX_BYTES]
        except Exception:
            self._log.exception("README ophalen mislukt voor %s", full_name)
            return None

    # ------------------------------------------------------------------
    # Strategie-extractie
    # ------------------------------------------------------------------

    def _extract_strategy(self, readme: str, repo: dict) -> dict | None:
        """
        Zoek naar entry- en exit-indicatoren in de README.

        Retourneert een dict met gevonden keywords, of None als de README
        geen herkenbare strategie-logica bevat.
        """
        text = readme.lower()
        found_entry = [kw for kw in _ENTRY_KEYWORDS if kw in text]
        found_exit  = [kw for kw in _EXIT_KEYWORDS  if kw in text]

        if not found_entry or not found_exit:
            return None

        return {
            "entry_keywords": found_entry,
            "exit_keywords":  found_exit,
            "description":    (repo.get("description") or "").strip(),
        }

    # ------------------------------------------------------------------
    # Kandidaat bouwen
    # ------------------------------------------------------------------

    def _build_candidate(self, repo: dict, extracted: dict) -> StrategyCandidate | None:
        """Bouw een StrategyCandidate van repo-metadata + extracted strategie."""
        url       = repo.get("html_url", "")
        full_name = repo.get("full_name", url)
        desc      = extracted.get("description") or f"GitHub: {full_name}"

        try:
            return StrategyCandidate(
                candidate_id=str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
                name=full_name,
                source="github",
                source_url=url,
                biome=self.mission.market_scope.biome,
                market_scope={"symbols": self.mission.market_scope.symbols},
                logic_summary=desc[:500],
                parameters={
                    "stars":    repo.get("stargazers_count", 0),
                    "language": repo.get("language") or "",
                },
                entry_conditions={"keywords": extracted["entry_keywords"]},
                exit_conditions={"keywords": extracted["exit_keywords"]},
                status=CandidateStatus.INGESTED,
                provenance=[
                    ProvenanceEntry(
                        actor=self.ant_id,
                        action="ingested",
                        details={
                            "source":   "github",
                            "url":      url,
                            "stars":    repo.get("stargazers_count", 0),
                            "found_at": datetime.now(tz=timezone.utc).isoformat(),
                        },
                    )
                ],
            )
        except Exception:
            self._log.exception("Kan StrategyCandidate niet bouwen voor %s", url)
            return None

    # ------------------------------------------------------------------
    # Reddit API
    # ------------------------------------------------------------------

    def _fetch_reddit(self) -> list[dict]:
        """Haal top posts van r/algotrading op. Retourneert lijst van post-dicts."""
        self._rate_limit("reddit")
        try:
            resp = httpx.get(
                _REDDIT_TOP_URL,
                headers={"User-Agent": "ant-colony-ingestion/2.0"},
                timeout=15.0,
                follow_redirects=True,
            )
            if resp.status_code == 429:
                self._log.warning("Reddit rate limit (429) — backoff %.0fs", _RATE_LIMIT_BACKOFF)
                time.sleep(_RATE_LIMIT_BACKOFF)
                return []
            if resp.status_code != 200:
                self._log.warning("Reddit HTTP %d", resp.status_code)
                return []
            children = resp.json().get("data", {}).get("children", [])
            return [c["data"] for c in children if "data" in c]
        except Exception:
            self._log.exception("Reddit fetch mislukt")
            return []

    def _process_reddit_post(self, post: dict) -> None:
        """Filter en normaliseer één Reddit post naar StrategyCandidate."""
        title    = post.get("title", "")
        selftext = post.get("selftext", "")
        url      = post.get("url", "")
        permalink = "https://www.reddit.com" + post.get("permalink", "")

        combined = (title + " " + selftext).lower()
        if not any(kw in combined for kw in _REDDIT_KEYWORDS):
            self._log.debug("Reddit post overgeslagen — geen keywords: %s", title[:60])
            return

        source_url = url if url.startswith("http") else permalink
        if source_url in self._seen_urls:
            self._log.debug("Reddit duplicaat: %s", source_url)
            return
        self._seen_urls.add(source_url)

        extracted = self._extract_strategy(combined, {"description": title})
        if extracted is None:
            self._log.debug("Geen herkenbare strategie in Reddit post: %s", title[:60])
            return

        candidate = self._build_text_candidate(
            source="reddit",
            source_url=source_url,
            name=title[:120],
            description=title[:500],
            extracted=extracted,
            extra={"permalink": permalink, "score": post.get("score", 0)},
        )
        if candidate is None:
            return

        self._write_candidate(candidate)
        self._last_action = f"ingested:reddit:{title[:40]}"
        self._log.info(
            "KANDIDAAT INGESTED | reddit | %s  score=%d",
            title[:60], post.get("score", 0),
        )

    # ------------------------------------------------------------------
    # Dev.to API
    # ------------------------------------------------------------------

    def _fetch_devto(self) -> list[dict]:
        """Haal trading-artikelen van Dev.to op. Retourneert lijst van artikel-dicts."""
        self._rate_limit("devto")
        try:
            resp = httpx.get(
                _DEVTO_ARTICLES_URL,
                headers={"Accept": "application/json"},
                timeout=15.0,
                follow_redirects=True,
            )
            if resp.status_code == 429:
                self._log.warning("Dev.to rate limit (429) — backoff %.0fs", _RATE_LIMIT_BACKOFF)
                time.sleep(_RATE_LIMIT_BACKOFF)
                return []
            if resp.status_code != 200:
                self._log.warning("Dev.to HTTP %d", resp.status_code)
                return []
            return resp.json() if isinstance(resp.json(), list) else []
        except Exception:
            self._log.exception("Dev.to fetch mislukt")
            return []

    def _process_devto_article(self, article: dict) -> None:
        """Filter en normaliseer één Dev.to artikel naar StrategyCandidate."""
        title       = article.get("title", "")
        description = article.get("description", "")
        url         = article.get("url", "")

        if not url:
            return

        combined = (title + " " + description).lower()
        if not any(kw in combined for kw in _REDDIT_KEYWORDS):
            self._log.debug("Dev.to artikel overgeslagen — geen keywords: %s", title[:60])
            return

        if url in self._seen_urls:
            self._log.debug("Dev.to duplicaat: %s", url)
            return
        self._seen_urls.add(url)

        extracted = self._extract_strategy(combined, {"description": description})
        if extracted is None:
            self._log.debug("Geen herkenbare strategie in Dev.to artikel: %s", title[:60])
            return

        candidate = self._build_text_candidate(
            source="devto",
            source_url=url,
            name=title[:120],
            description=(description or title)[:500],
            extracted=extracted,
            extra={"reactions": article.get("positive_reactions_count", 0)},
        )
        if candidate is None:
            return

        self._write_candidate(candidate)
        self._last_action = f"ingested:devto:{title[:40]}"
        self._log.info("KANDIDAAT INGESTED | devto | %s", title[:60])

    # ------------------------------------------------------------------
    # Generieke kandidaat-builder voor tekst-bronnen
    # ------------------------------------------------------------------

    def _build_text_candidate(
        self,
        source: str,
        source_url: str,
        name: str,
        description: str,
        extracted: dict,
        extra: dict,
    ) -> StrategyCandidate | None:
        """Bouw StrategyCandidate voor niet-GitHub bronnen (Reddit, Dev.to)."""
        try:
            return StrategyCandidate(
                candidate_id=str(uuid.uuid5(uuid.NAMESPACE_URL, source_url)),
                name=name,
                source=source,
                source_url=source_url,
                biome=self.mission.market_scope.biome,
                market_scope={"symbols": self.mission.market_scope.symbols},
                logic_summary=description,
                parameters=extra,
                entry_conditions={"keywords": extracted["entry_keywords"]},
                exit_conditions={"keywords": extracted["exit_keywords"]},
                status=CandidateStatus.INGESTED,
                provenance=[
                    ProvenanceEntry(
                        actor=self.ant_id,
                        action="ingested",
                        details={
                            "source":   source,
                            "url":      source_url,
                            "found_at": datetime.now(tz=timezone.utc).isoformat(),
                            **extra,
                        },
                    )
                ],
            )
        except Exception:
            self._log.exception("Kan StrategyCandidate niet bouwen voor %s", source_url)
            return None

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _rate_limit(self, source: str) -> None:
        """Blokkeer tot de per-bron rate limit is verstreken."""
        limit   = _RATE_LIMITS.get(source, 10.0)
        elapsed = time.monotonic() - self._last_call.get(source, 0.0)
        if elapsed < limit:
            time.sleep(limit - elapsed)
        self._last_call[source] = time.monotonic()

    # ------------------------------------------------------------------
    # Log events
    # ------------------------------------------------------------------

    def _write_candidate(self, candidate: StrategyCandidate) -> None:
        """Schrijf een AuditEvent met de kandidaat naar ANT_LOGS/ingestion/{ant_id}.jsonl."""
        if self.logs_root is None:
            return

        event = AuditEvent(
            event_type=AuditEventType.ACTION_EXECUTED,
            source=self.ant_id,
            mission_id=self.mission.mission_id,
            node_id=self.mission.allowed_node,
            sequence=self._log_seq,
            payload={
                "action":        "candidate_ingested",
                "candidate_id":  candidate.candidate_id,
                "name":          candidate.name,
                "source_url":    candidate.source_url,
                "status":        candidate.status.value,
                "stars":         candidate.parameters.get("stars"),
                "entry_keywords": candidate.entry_conditions.get("keywords"),
                "exit_keywords":  candidate.exit_conditions.get("keywords"),
                "logic_summary":  candidate.logic_summary,
                "provenance":     [p.model_dump(mode="json") for p in candidate.provenance],
            },
        )
        self._log_seq += 1

        log_path = self.logs_root / "ingestion" / f"{self.ant_id}.jsonl"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError:
            self._log.exception("Kon kandidaat niet naar disk schrijven: %s", log_path)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def _send_heartbeat(self) -> None:
        """Stuur heartbeat naar de scheduler (fail-closed: negeert fouten)."""
        try:
            hb_status = (
                HeartbeatStatus.RUNNING
                if self._status == AntStatus.RUNNING
                else HeartbeatStatus.PAUSED
            )
            hb = Heartbeat(
                ant_id=self.ant_id,
                mission_id=self.mission.mission_id,
                node_id=self.mission.allowed_node,
                status=hb_status,
                budget_used=0.0,
                last_action=self._last_action,
            )
            self.scheduler.record_heartbeat(hb)
            self._log.debug("Heartbeat gestuurd | action=%s", self._last_action)
        except Exception:
            self._log.exception("Heartbeat mislukt — doorgaan")
