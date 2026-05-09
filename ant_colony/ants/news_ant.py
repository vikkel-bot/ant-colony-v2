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
import xml.etree.ElementTree as ET
from html import unescape
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
_RSS_TIMEOUT      = 10.0

_RSS_FALLBACK_FEEDS = [
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]

_MARKET_QUERIES   = [
    "stock market", "crypto market", "Federal Reserve",
    "inflation", "recession", "tariffs", "trade war",
]
_CRYPTO_QUERIES   = ["Bitcoin", "Ethereum", "crypto regulation"]
_EQUITIES_QUERIES = ["earnings", "S&P 500", "NYSE", "NASDAQ"]

_RSS_TAG_KEYWORDS = {
    "crypto": (
        "bitcoin", "crypto", "ethereum", "blockchain",
    ),
    "market": (
        "fed", "federal reserve", "inflation", "rate", "rates",
        "stock", "market", "recession", "tariff",
    ),
    "technology": (
        "tech", "technology", "ai", "chip", "semiconductor",
    ),
    "commodity": (
        "oil", "gas", "energy", "brent", "natural gas",
        "copper", "silver", "gold",
    ),
}

_POSITIVE_WORDS = frozenset([
    "surge", "rally", "gain", "bullish", "growth",
    "beat", "strong", "rise", "up", "profit", "record",
    "outperform", "boom", "recovery", "optimism", "soar",
])
_NEGATIVE_WORDS = frozenset([
    "crash", "fall", "drop", "bearish", "recession",
    "warn", "weak", "down", "loss", "fear", "plunge", "slump",
    "concern", "risk", "decline", "tumble", "selloff",
])

_BULLISH_THRESHOLD = 0.02
_BEARISH_THRESHOLD = -0.02

_TOP_HEADLINES_COUNT = 3

_sentiment_log = logging.getLogger("ant.news.sentiment")


def _classify(score: float) -> str:
    if score > _BULLISH_THRESHOLD:
        return "bullish"
    if score < _BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _sentiment_score(articles: list[dict]) -> float:
    """Bereken gemiddelde sentiment-score over een lijst artikelen."""
    total       = 0.0
    count       = 0
    pos_total   = 0
    neg_total   = 0
    words_total = 0
    for art in articles:
        title = art.get("title") or ""
        desc  = art.get("description") or ""
        text  = (title + " " + desc).lower()
        words = re.findall(r"[a-z]+", text)
        if not words:
            continue
        pos   = sum(1 for w in words if w in _POSITIVE_WORDS)
        neg   = sum(1 for w in words if w in _NEGATIVE_WORDS)
        total       += (pos - neg) / len(words)
        pos_total   += pos
        neg_total   += neg
        words_total += len(words)
        count       += 1
    score = total / count if count > 0 else 0.0
    _sentiment_log.debug(
        "sentiment_score | artikelen=%d pos_hits=%d neg_hits=%d totaal_woorden=%d score=%.4f",
        count, pos_total, neg_total, words_total, score,
    )
    return score


def _rss_tags_for_title(title: str) -> list[str]:
    text = (title or "").lower()
    tags = [
        tag
        for tag, keywords in _RSS_TAG_KEYWORDS.items()
        if any(keyword in text for keyword in keywords)
    ]
    return tags or ["market"]


def _parse_rss_items(payload: bytes | str, source: str) -> list[dict]:
    """Parse RSS-items naar NewsAPI-achtige artikel-dicts."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    root = ET.fromstring(payload)
    articles: list[dict] = []
    for item in root.findall(".//item"):
        title = unescape((item.findtext("title") or "").strip())
        description = unescape((item.findtext("description") or "").strip())
        link = (item.findtext("link") or "").strip()
        if not title:
            continue
        articles.append(
            {
                "title": title,
                "description": description,
                "url": link,
                "source": {"name": source},
                "tags": _rss_tags_for_title(title),
            }
        )
    return articles


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
        self._newsapi_failed: bool = False
        self._last_newsapi_status: int | None = None

        self._log = logging.getLogger(f"ant.news.{ant_id[:8]}")
        self._out_dir = (Path(logs_root) / "news") if logs_root else None
        self._usage_path = (self._out_dir / "api_usage.json") if self._out_dir else None
        self._load_api_usage()

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
        self._newsapi_failed = False
        self._last_newsapi_status = None

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

        if self._newsapi_failed:
            rss_articles = self._fetch_rss_fallback()
            if self._last_newsapi_status == 429:
                self._log.info(
                    "NewsAPI 429 → RSS fallback actief | articles=%d",
                    len(rss_articles),
                )
            else:
                self._log.info(
                    "NewsAPI fout → RSS fallback actief | articles=%d",
                    len(rss_articles),
                )
            market_articles, crypto_articles, equities_articles = (
                self._split_rss_articles(rss_articles, is_weekend=is_weekend)
            )

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
        if self._newsapi_failed:
            snapshot["source"] = "rss_fallback"

        self._write_snapshot(now, snapshot)
        self._log.info(
            "NewsAnt snapshot geschreven | articles=%d  "
            "market=%s(%.4f)  crypto=%s(%.4f)  equities=%s(%.4f)",
            len(all_articles),
            snapshot["market_sentiment"],  snapshot["sentiment_score"],
            snapshot["crypto_sentiment"],  snapshot["crypto_score"],
            snapshot["equities_sentiment"], snapshot["equities_score"],
        )

    def _fetch_queries(self, queries: list[str]) -> list[dict]:
        """Haal artikelen op voor een lijst queries. Stop bij call-limiet."""
        articles: list[dict] = []
        for q in queries:
            if self._daily_calls >= _DAILY_CALL_LIMIT or self._newsapi_failed:
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
            self._record_api_call()
            if resp.status_code != 200:
                self._newsapi_failed = True
                self._last_newsapi_status = resp.status_code
                self._log.warning(
                    "NewsAPI fout voor query '%s': HTTP %d", query, resp.status_code
                )
                return []
            data = resp.json()
            return data.get("articles", [])
        except Exception:
            self._newsapi_failed = True
            self._log.exception("NewsAPI aanroep mislukt voor query '%s'", query)
            return []

    def _fetch_rss_fallback(self) -> list[dict]:
        articles: list[dict] = []
        for url in _RSS_FALLBACK_FEEDS:
            try:
                resp = httpx.get(url, timeout=_RSS_TIMEOUT)
                if resp.status_code != 200:
                    self._log.warning("RSS fallback fout voor %s: HTTP %d", url, resp.status_code)
                    continue
                source = "BBC"
                if "technology" in url:
                    source = "BBC Technology"
                elif "business" in url:
                    source = "BBC Business"
                elif "world" in url:
                    source = "BBC World"
                articles.extend(_parse_rss_items(resp.content, source=source))
            except Exception as exc:
                self._log.warning("RSS fallback mislukt voor %s: %s", url, exc)
        if not articles:
            self._log.warning("RSS fallback leverde geen artikelen op")
        return articles

    def _split_rss_articles(
        self,
        articles: list[dict],
        *,
        is_weekend: bool,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        market_articles: list[dict] = []
        crypto_articles: list[dict] = []
        equities_articles: list[dict] = []
        for article in articles:
            tags = set(article.get("tags") or [])
            if "crypto" in tags:
                crypto_articles.append(article)
            if "technology" in tags and not is_weekend:
                equities_articles.append(article)
            if tags.intersection({"market", "commodity"}) or not tags:
                market_articles.append(article)
        return market_articles, crypto_articles, equities_articles

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
            self._save_api_usage()

    def _record_api_call(self) -> None:
        self._daily_calls += 1
        if self._daily_calls > _DAILY_CALL_WARNING:
            self._log.warning(
                "Dagelijks API limiet bijna bereikt (%d/%d calls)",
                self._daily_calls, _DAILY_CALL_LIMIT,
            )
        self._save_api_usage()

    def _load_api_usage(self) -> None:
        if self._usage_path is None or not self._usage_path.exists():
            return
        try:
            raw = json.loads(self._usage_path.read_text(encoding="utf-8"))
            usage_date = date.fromisoformat(str(raw.get("date")))
            today = datetime.now(timezone.utc).date()
            if usage_date == today:
                self._daily_calls = int(raw.get("calls", 0))
                self._reset_date = usage_date
        except Exception as exc:
            self._log.warning("NewsAPI usage teller kon niet worden gelezen: %s", exc)

    def _save_api_usage(self) -> None:
        if self._usage_path is None:
            return
        self._usage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": self._reset_date.isoformat(),
            "calls": self._daily_calls,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._usage_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
