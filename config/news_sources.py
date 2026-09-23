"""Load and validate news source configuration from config/news_sources.yaml."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_NEWS_SOURCES_PATH = Path(__file__).resolve().parent / "news_sources.yaml"


class NewsSourcesError(ValueError):
    """Raised when the news sources file is missing, malformed, or invalid."""


@dataclass(frozen=True)
class Feed:
    """A general publisher RSS feed."""

    name: str
    publisher: str
    url: str


@dataclass(frozen=True)
class NewsConfig:
    """Everything collectors/news.py needs to know about its sources."""

    user_agent: str
    timeout_s: float
    rate_limits: dict[str, float]
    google_news_url: str
    google_news_params: dict[str, str]
    google_news_window: str
    feeds: list[Feed] = field(default_factory=list)
    text_max_attempts: int = 3
    text_min_chars: int = 300
    text_boilerplate: dict[str, list[str]] = field(default_factory=dict)
    story_similarity_threshold: float = 90.0
    story_window_hours: float = 48.0
    story_min_tokens: int = 6
    sentiment_model: str = "ProsusAI/finbert"
    sentiment_revision: str | None = None
    sentiment_batch_size: int = 16
    sentiment_max_sentences: int = 6
    sentiment_strong_negative: float = -0.5

    def min_interval(self, domain: str) -> float:
        """Minimum seconds between requests to `domain`."""
        return self.rate_limits.get(domain, self.rate_limits.get("default", 1.0))


def load_news_sources(path: Path = DEFAULT_NEWS_SOURCES_PATH) -> NewsConfig:
    """Load and validate the news sources file.

    Raises:
        NewsSourcesError: if the file is missing or invalid, or feed names are duplicated.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise NewsSourcesError(f"News sources file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise NewsSourcesError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise NewsSourcesError(f"{path}: expected a mapping at the top level")

    google = _require(raw, "google_news", dict, path)
    feeds = [_parse_feed(entry, i, path) for i, entry in enumerate(raw.get("feeds") or [])]
    names = [f.name for f in feeds]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise NewsSourcesError(f"{path}: duplicate feed name(s): {', '.join(duplicates)}")
    if any(n.startswith("google:") for n in names):
        raise NewsSourcesError(f"{path}: feed names may not start with 'google:' (reserved)")

    text = raw.get("article_text") or {}
    stories = raw.get("stories") or {}
    sentiment = raw.get("sentiment") or {}
    strong_negative = float(sentiment.get("strong_negative", -0.5))
    if not -1 <= strong_negative <= 0:
        raise NewsSourcesError(f"{path}: sentiment.strong_negative must be in [-1, 0]")
    threshold = float(stories.get("similarity_threshold", 90))
    if not 0 < threshold <= 100:
        raise NewsSourcesError(f"{path}: stories.similarity_threshold must be in (0, 100]")
    max_attempts = int(text.get("max_attempts", 3))
    if max_attempts < 1:
        raise NewsSourcesError(f"{path}: article_text.max_attempts must be at least 1")

    return NewsConfig(
        user_agent=_require(raw, "user_agent", str, path),
        timeout_s=float(raw.get("timeout_s", 20)),
        rate_limits={str(k): float(v) for k, v in (raw.get("rate_limits") or {}).items()},
        google_news_url=_require(google, "url", str, path),
        google_news_params={str(k): str(v) for k, v in (google.get("params") or {}).items()},
        google_news_window=str(google.get("window", "7d")),
        feeds=feeds,
        text_max_attempts=max_attempts,
        text_min_chars=int(text.get("min_chars", 300)),
        text_boilerplate=_parse_boilerplate(text.get("boilerplate") or {}, path),
        story_similarity_threshold=threshold,
        story_window_hours=float(stories.get("window_hours", 48)),
        story_min_tokens=int(stories.get("min_tokens", 6)),
        sentiment_model=str(sentiment.get("model", "ProsusAI/finbert")),
        sentiment_revision=sentiment.get("revision"),
        sentiment_batch_size=int(sentiment.get("batch_size", 16)),
        sentiment_max_sentences=int(sentiment.get("max_sentences", 6)),
        sentiment_strong_negative=strong_negative,
    )


def _parse_boilerplate(raw: Any, path: Path) -> dict[str, list[str]]:
    """Validate {domain: [regex, ...]}; '*' applies to every domain."""
    if not isinstance(raw, dict):
        raise NewsSourcesError(f"{path}: article_text.boilerplate must be a mapping")
    result: dict[str, list[str]] = {}
    for domain, patterns in raw.items():
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise NewsSourcesError(f"{path}: boilerplate[{domain}] must be a list of regexes")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise NewsSourcesError(f"{path}: bad boilerplate regex {pattern!r}: {exc}") from exc
        result[str(domain).lower()] = patterns
    return result


def _require(mapping: dict[str, Any], key: str, kind: type, path: Path) -> Any:
    """Return mapping[key], raising NewsSourcesError if it's missing or the wrong type."""
    value = mapping.get(key)
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise NewsSourcesError(f"{path}: '{key}' must be a non-empty {kind.__name__}")
    return value


def _parse_feed(entry: Any, index: int, path: Path) -> Feed:
    """Validate one feed entry."""
    if not isinstance(entry, dict):
        raise NewsSourcesError(f"{path}: feeds[{index}] must be a mapping")
    unknown = sorted(set(entry) - {"name", "publisher", "url"})
    if unknown:
        raise NewsSourcesError(f"{path}: feeds[{index}] unknown field(s): {', '.join(unknown)}")
    values = {k: _require(entry, k, str, path).strip() for k in ("name", "publisher", "url")}
    if not values["url"].startswith("https://"):
        raise NewsSourcesError(f"{path}: feeds[{index}] url must start with https://")
    return Feed(**values)
