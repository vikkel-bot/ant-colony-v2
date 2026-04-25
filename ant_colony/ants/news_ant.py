"""
ant_colony/ants/news_ant.py

NewsAnt — monitort nieuwsfeeds via NewsAPI en schrijft sentiment-snapshots
naar ANT_LOGS/news/.

Verantwoordelijkheden:
  1. Poll NewsAPI elke 30 minuten (max 80 calls/dag, hard stop bij 100).
  2. Berekent sentiment-scores voor drie categorieën:
       - market_sentiment   (breed marktnieuws + macro)
       - crypto_sentiment   (BTC, ETH, crypto regulation)
       - equities_sentiment (earnings, indices — alleen ma-vr)
  3. Schrijft per poll één JSON-snapshot naar ANT_LOGS/news/.
  4. Slaat equities queries over in het weekend.

Regels:
  - Geen live orders, geen paper posities (pure observatie).
  - Gooit nooit een exception naar buiten.
  - Geen API-aanroepen als NEWS_API_KEY ontbreekt.
  - Dagelijkse call-teller reset op middernacht UTC.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, date, timezone
from pathlib import Path

import httpx

from ant_colony.colony.scheduler.colony_scheduler import ColonyScheduler
from ant_colony.schemas.mission import Mission

# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------

_POLL_INTERVAL_SECONDS  = 30 * 60          # 30 minuten tussen polls
_DAILY_CALL_LIMIT       = 100
_DAILY_CALL_WARNING     = 80               # log warning bij dit aantal

_NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"
_NEWSAPI_TIMEOUT  = 10.0                   # seconden

_MARKET_QUERIES   = [
    "stock market", "crypto market", "Federal Reserve",
    "inflation", "recession", "tariffs", "trade war",
]
_CRYPTO_QUERIES   = ["Bitcoin", "Ethereum", "crypto regulation"]
_EQUITIES_QUERIES = ["earnings", "S&P 500", "NYSE", "NASDAQ"]

_POSITIVE_WORDS = frozenset([
    "surge", "rally", "gain", "bullish", "growth",
    "beat", "strong", "rise", "up",
])
_NEGATIVE_WORDS = frozenset([
    "crash", "fall", "drop", "bearish", "recession",
    "warn", "weak", "down", "loss", "fear",
])

_BULLISH_THRESHOLD = 0.1
_BEARISH_THRESHOLD = -0.1

_TOP_HEADLINES_COUNT = 3


def _classify(score: float) -> str:
    if score > _BULLISH_THRESHOLD:
        return "bullish"
    if score < _BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _sentiment_score(articles: list[dict]) -> float:
    """Bereken gemiddelde sentiment-score over een lijst artikelen."""
    total = 0.0
    count = 0
    for art in articles:
        title = art.get("title") or ""
        desc  = art.get("description") or ""
        text  = (title + " " + desc).lower()
        words = re.findall(r"[a-z]+", text)
        if not words:
            continue
        pos   = sum(1 for w in words if w in _POSITIVE_WORDS)
        neg   = sum(1 for w in words if w in _NEGATIVE_WORDS)
        total += (pos - neg) / len(words)
        count += 1
    return total / count if count > 0 else 0.0


class NewsAnt:
    """
    Nieuwssentiment-monitor die periodiek NewsAPI raadpleegt en snapshots
    wegschrijft naar ANT_LOGS/news/.

    Args:
        ant_id:    Unieke identifier.
        mission:   Toegewezen Mission (capital_limit=0, observatie-only).
        scheduler: ColonyScheduler voor heartbeats.
        logs_root: Pad naar ANT_LOGS root directory.
        api_key:   NewsAPI key (NEWS_API_KEY uit .env).
    """

    def __init__(
        self,
        ant_id: str,
        mission: Mission,
        scheduler: ColonyScheduler,
        logs_root: Path | None,
        api_key: str,
    ) -> None:
        self.ant_id    = ant_id
        self.mission   = mission
        self.scheduler = scheduler
        self.logs_root = logs_root
        self._api_key  = api_key

        self._daily_calls: int  = 0
        self._reset_date: date  = datetime.now(timezone.utc).date()

        self._log = logging.getLogger(f"ant.news.{ant_id[:8]}")
        self._out_dir = (Path(logs_root) / "news") if logs_root else None

    # ------------------------------------------------------------------
    # Publieke interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Hoofdlus: poll elke 30 minuten. Blokkeerd indefinitely."""
        if self._out_dir is not None:
            self._out_dir.mkdir(parents=True, exist_ok=True)

        self._log.info(
            "NewsAnt gestart | ant_id=%s  interval=%ds",
            self.ant_id, _POLL_INTERVAL_SECONDS,
        )
        while True:
            try:
                self._tick()
            except Exception:
                self._log.exception("NewsAnt tick onverwachte fout — gaat door")
            time.sleep(_POLL_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Interne methoden
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        self._maybe_reset_daily_counter(now.date())

        if self._daily_calls >= _DAILY_CALL_LIMIT:
            self._log.warning(
                "Dagelijks API limiet bereikt (%d calls) — poll overgeslagen",
                _DAILY_CALL_LIMIT,
            )
            return

        is_weekend = now.weekday() >= 5

        market_articles   = self._fetch_queries(_MARKET_QUERIES)
        crypto_articles   = self._fetch_queries(_CRYPTO_QUERIES)
        equities_articles = [] if is_weekend else self._fetch_queries(_EQUITIES_QUERIES)

        all_articles = market_articles + crypto_articles + equities_articles

        market_score   = _sentiment_score(market_articles)
        crypto_score   = _sentiment_score(crypto_articles)
        equities_score = _sentiment_score(equities_articles) if not is_weekend else 0.0

        bullish_count = 0
        bearish_count = 0
        neutral_count = 0
        for art in all_articles:
            score = _sentiment_score([art])
            if score > _BULLISH_THRESHOLD:
                bullish_count += 1
            elif score < _BEARISH_THRESHOLD:
                bearish_count += 1
            else:
                neutral_count += 1

        top_headlines = [
            art.get("title", "")
            for art in all_articles[:_TOP_HEADLINES_COUNT]
            if art.get("title")
        ]

        snapshot: dict = {
            "timestamp":           now.strftime("%Y-%m-%dT%H:%M:%S"),
            "market_sentiment":    _classify(market_score),
            "sentiment_score":     round(market_score, 4),
            "crypto_sentiment":    _classify(crypto_score),
            "crypto_score":        round(crypto_score, 4),
            "equities_sentiment":  _classify(equities_score),
            "equities_score":      round(equities_score, 4),
            "top_headlines":       top_headlines,
            "article_count":       len(all_articles),
            "bullish_count":       bullish_count,
            "bearish_count":       bearish_count,
            "neutral_count":       neutral_count,
        }
        if is_weekend:
            snapshot["weekend"] = True

        self._write_snapshot(now, snapshot)
        self._log.info(
            "NewsAnt snapshot geschreven | articles=%d  market=%s  crypto=%s  equities=%s",
            len(all_articles),
            snapshot["market_sentiment"],
            snapshot["crypto_sentiment"],
            snapshot["equities_sentiment"],
        )

    def _fetch_queries(self, queries: list[str]) -> list[dict]:
        """Haal artikelen op voor een lijst queries. Stop bij call-limiet."""
        articles: list[dict] = []
        for q in queries:
            if self._daily_calls >= _DAILY_CALL_LIMIT:
                break

            if self._daily_calls >= _DAILY_CALL_WARNING:
                self._log.warning(
                    "Dagelijks API limiet bijna bereikt (%d/%d calls)",
                    self._daily_calls, _DAILY_CALL_LIMIT,
                )

            batch = self._fetch_one(q)
            articles.extend(batch)

        return articles

    def _fetch_one(self, query: str) -> list[dict]:
        """Eén API-aanroep voor één query. Retourneert lege lijst bij fout."""
        try:
            resp = httpx.get(
                _NEWSAPI_BASE_URL,
                params={
                    "q":        query,
                    "language": "en",
                    "pageSize": 10,
                    "sortBy":   "publishedAt",
                },
                headers={"X-Api-Key": self._api_key},
                timeout=_NEWSAPI_TIMEOUT,
            )
            self._daily_calls += 1
            if resp.status_code != 200:
                self._log.warning(
                    "NewsAPI fout voor query '%s': HTTP %d", query, resp.status_code
                )
                return []
            data = resp.json()
            return data.get("articles", [])
        except Exception:
            self._log.exception("NewsAPI aanroep mislukt voor query '%s'", query)
            return []

    def _write_snapshot(self, now: datetime, snapshot: dict) -> None:
        if self._out_dir is None:
            return
        self._out_dir.mkdir(parents=True, exist_ok=True)
        filename = now.strftime("%Y%m%d-%H%M%S") + ".json"
        path = self._out_dir / filename
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    def _maybe_reset_daily_counter(self, today: date) -> None:
        if today != self._reset_date:
            self._log.info(
                "Dagelijks call-teller gereset (%d calls gisteren)", self._daily_calls
            )
            self._daily_calls = 0
            self._reset_date  = today
