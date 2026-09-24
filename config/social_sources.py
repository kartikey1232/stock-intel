"""Load and validate social source configuration from config/social_sources.yaml."""

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SOCIAL_SOURCES_PATH = Path(__file__).resolve().parent / "social_sources.yaml"
THREAD_CONFIDENCE = 0.9  # a post in a stock's dedicated topic is about that stock

_TOPIC_FIELDS = {"id", "title", "symbol", "default_until"}


class SocialSourcesError(ValueError):
    """Raised when the social sources file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class Topic:
    """A forum topic to follow. `symbol` is None for general topics."""

    id: int
    title: str
    symbol: str | None = None
    default_until: dt.date | None = None

    def default_symbol(self, created: dt.date) -> str | None:
        """The stock a post created on `created` links to without the entity linker."""
        if self.symbol and (self.default_until is None or created < self.default_until):
            return self.symbol
        return None


@dataclass(frozen=True)
class ValuePickrConfig:
    """Everything collectors/valuepickr.py needs."""

    base_url: str
    user_agent: str
    timeout_s: float = 20.0
    min_interval_s: float = 5.0
    max_retry_after_s: float = 300.0
    backfill_posts: int = 200
    discovered_backfill_posts: int = 20
    recheck_posts: int = 100
    latest_max_topics: int = 5
    confirmed: bool = False
    topics: list[Topic] = field(default_factory=list)

    def topic(self, topic_id: int) -> Topic | None:
        """The configured topic with this id, if any."""
        return next((t for t in self.topics if t.id == topic_id), None)


@dataclass(frozen=True)
class ScoringConfig:
    """Which social posts get sentiment scores (processing/social.py)."""

    min_words: int = 6
    headline_sources: tuple[str, ...] = ()


def load_social_scoring(path: Path = DEFAULT_SOCIAL_SOURCES_PATH) -> ScoringConfig:
    """Load the `scoring` section of the social sources file (defaults if absent).

    Raises:
        SocialSourcesError: if the file is missing or the section is malformed.
    """
    raw = _read(path)
    scoring = raw.get("scoring") or {}
    if not isinstance(scoring, dict):
        raise SocialSourcesError(f"{path}: 'scoring' must be a mapping")
    sources = scoring.get("headline_sources") or []
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        raise SocialSourcesError(f"{path}: scoring.headline_sources must be a list of names")
    min_words = int(scoring.get("min_words", 6))
    if min_words < 1:
        raise SocialSourcesError(f"{path}: scoring.min_words must be at least 1")
    return ScoringConfig(min_words=min_words, headline_sources=tuple(sources))


def _read(path: Path) -> dict[str, Any]:
    """The parsed YAML mapping in `path`."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SocialSourcesError(f"Social sources file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SocialSourcesError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SocialSourcesError(f"{path}: expected a mapping at the top level")
    return raw


def load_social_sources(path: Path = DEFAULT_SOCIAL_SOURCES_PATH) -> ValuePickrConfig:
    """Load and validate the ValuePickr section of the social sources file.

    Raises:
        SocialSourcesError: if the file is missing or invalid, a topic is malformed, or
            topic ids are duplicated.
    """
    vp = _read(path).get("valuepickr")
    if not isinstance(vp, dict):
        raise SocialSourcesError(f"{path}: expected a 'valuepickr' mapping")
    for key in ("base_url", "user_agent"):
        if not isinstance(vp.get(key), str) or not vp[key].strip():
            raise SocialSourcesError(f"{path}: valuepickr.{key} must be a non-empty string")
    if not vp["base_url"].startswith("https://"):
        raise SocialSourcesError(f"{path}: valuepickr.base_url must start with https://")
    topics = [_parse_topic(t, i, path) for i, t in enumerate(vp.get("topics") or [])]
    ids = [t.id for t in topics]
    if len(ids) != len(set(ids)):
        raise SocialSourcesError(f"{path}: duplicate topic id(s)")
    min_interval = float(vp.get("min_interval_s", 5))
    if min_interval < 1:
        raise SocialSourcesError(f"{path}: valuepickr.min_interval_s must be at least 1")
    return ValuePickrConfig(
        base_url=vp["base_url"].rstrip("/"),
        user_agent=vp["user_agent"].strip(),
        timeout_s=float(vp.get("timeout_s", 20)),
        min_interval_s=min_interval,
        max_retry_after_s=float(vp.get("max_retry_after_s", 300)),
        backfill_posts=int(vp.get("backfill_posts", 200)),
        discovered_backfill_posts=int(vp.get("discovered_backfill_posts", 20)),
        recheck_posts=int(vp.get("recheck_posts", 100)),
        latest_max_topics=int(vp.get("latest_max_topics", 5)),
        confirmed=vp.get("confirmed") is True,
        topics=topics,
    )


def _parse_topic(entry: Any, index: int, path: Path) -> Topic:
    """Validate one topic entry."""
    where = f"{path}: topics[{index}]"
    if not isinstance(entry, dict):
        raise SocialSourcesError(f"{where} must be a mapping")
    unknown = sorted(set(entry) - _TOPIC_FIELDS)
    if unknown:
        raise SocialSourcesError(f"{where} unknown field(s): {', '.join(unknown)}")
    if not isinstance(entry.get("id"), int) or entry["id"] <= 0:
        raise SocialSourcesError(f"{where}: id must be a positive integer")
    until = entry.get("default_until")
    if until is not None and not isinstance(until, dt.date):
        raise SocialSourcesError(f"{where}: default_until must be a date (YYYY-MM-DD)")
    if until is not None and not entry.get("symbol"):
        raise SocialSourcesError(f"{where}: default_until needs a symbol")
    return Topic(
        id=entry["id"],
        title=str(entry.get("title") or ""),
        symbol=str(entry["symbol"]) if entry.get("symbol") else None,
        default_until=until,
    )
