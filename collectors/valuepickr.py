"""Collect raw public posts from ValuePickr (forum.valuepickr.com, a Discourse forum).

Only fetches and stores raw posts; deciding which stock a post is about and scoring it
belong in processing/social.py. Per run:

1. robots.txt is fetched and every later URL is checked against it.
2. Each configured topic: /t/<id>.json gives the topic's full post-id stream. Posts
   stored locally but no longer in the stream were deleted or hidden upstream and are
   deleted here. Posts newer than the last one fetched are downloaded (the first run only
   takes the newest `backfill_posts`), 20 per /t/<id>/posts.json request.
3. /latest.json: topics whose title names a watchlist stock (its news search terms, as
   for the Google News query) are fetched the same way, as "discovered" topics.
4. Deletion sync: the `recheck_posts` least recently checked posts are re-fetched;
   missing, deleted, withdrawn or hidden ones are deleted, edited ones get the new text.

Privacy (see CLAUDE.md): no usernames, display names, avatars or profile links are stored.
The author is kept only as an HMAC of Discourse's numeric user id, keyed with
SOCIAL_HASH_KEY from .env, so distinct authors can be counted. Quotes of other posts,
@mentions, code and images are stripped from the text.

Politeness: an honest User-Agent, one request every `min_interval_s` seconds, retries
with backoff for network errors and 5xx, and HTTP 429 handled by waiting Retry-After once
(if short enough) and otherwise stopping the run. Nothing is requested until
`confirmed: true` is set in config/social_sources.yaml.

Run with:  uv run python -m collectors.valuepickr
"""

import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode
from urllib.robotparser import RobotFileParser

import httpx
import lxml.html
from dotenv import load_dotenv

from config.loader import Stock, load_watchlist
from config.social_sources import ValuePickrConfig, load_social_sources
from storage.db import PROJECT_ROOT, init_db
from storage.social import (
    delete_posts,
    insert_new_posts,
    mark_checked,
    posts_to_recheck,
    read_topic,
    stored_posts,
    topic_post_ids,
    upsert_topic,
)
from utils import setup_logging
from utils.http import DomainRateLimiter, RetryableHTTPError
from utils.retry import retry

logger = logging.getLogger(__name__)

PLATFORM = "valuepickr"
HASH_KEY_ENV = "SOCIAL_HASH_KEY"
POSTS_PER_REQUEST = 20  # Discourse's post_ids[] batch size
REGULAR_POST = 1  # Discourse post_type; others are moderator notes and "small actions"
GONE_STATUSES = (403, 404, 410)  # topic deleted or made private
WITHDRAWN_RE = re.compile(r"^\(post (?:deleted|withdrawn) by author", re.IGNORECASE)
# Other people's words and identities: quotes, onebox previews, @mentions, code, images.
STRIP_XPATH = (
    "//aside | //blockquote | //pre | //code | //img | //svg"
    " | //a[contains(concat(' ', @class, ' '), ' mention ')]"
    " | //a[contains(concat(' ', @class, ' '), ' mention-group ')]"
    " | //div[contains(@class, 'lightbox-wrapper')] | //div[contains(@class, 'poll')]"
)
BLOCK_TAGS = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "tr", "br", "hr")
SPACE_RE = re.compile(r"[ \t ]+")


class NotConfiguredError(RuntimeError):
    """A required setting (e.g. SOCIAL_HASH_KEY) is missing."""


class RateLimitedError(RuntimeError):
    """HTTP 429. `retry_after` is the server's Retry-After in seconds, if given."""

    def __init__(self, url: str, retry_after: float | None) -> None:
        super().__init__(f"HTTP 429 from {url} (Retry-After: {retry_after})")
        self.retry_after = retry_after


class RunAbortedError(RuntimeError):
    """The forum kept rate-limiting us; no more requests this run."""


class RobotsDisallowedError(RuntimeError):
    """robots.txt disallows a URL we were about to request."""


@dataclass
class TopicResult:
    """Outcome of collecting one topic."""

    topic_id: int
    role: str
    new: int = 0
    deleted: int = 0
    error: str | None = None


@dataclass
class RunResult:
    """Outcome of a whole run."""

    topics: list[TopicResult] = field(default_factory=list)
    rechecked: int = 0
    recheck_deleted: int = 0
    recheck_edited: int = 0
    aborted: str | None = None
    skipped: str | None = None

    @property
    def failures(self) -> list[str]:
        """Descriptions of everything that failed."""
        failed = [f"topic {t.topic_id}: {t.error}" for t in self.topics if t.error]
        return failed + ([f"aborted: {self.aborted}"] if self.aborted else [])


# --- parsing -----------------------------------------------------------------------


def clean_cooked(cooked: str | None) -> str | None:
    """Plain text of a post's rendered HTML, without quotes, mentions, code or images."""
    if not cooked or not cooked.strip():
        return None
    root = lxml.html.fragment_fromstring(cooked, create_parent="div")
    for element in root.xpath(STRIP_XPATH):
        element.drop_tree()
    for element in root.iter(*BLOCK_TAGS):
        element.tail = "\n" + (element.tail or "")
    lines = (SPACE_RE.sub(" ", line).strip() for line in root.text_content().splitlines())
    text = "\n".join(line for line in lines if line)
    return text or None


def is_removed(post: dict[str, Any]) -> bool:
    """True if the post is deleted, withdrawn, hidden or otherwise not a live post."""
    if post.get("user_deleted") or post.get("hidden") or post.get("deleted_at"):
        return True
    text = clean_cooked(post.get("cooked")) or ""
    return bool(WITHDRAWN_RE.match(text))


def author_hash(key: bytes, user_id: Any) -> str | None:
    """Keyed one-way hash of a Discourse user id (None if there's no id)."""
    if user_id is None:
        return None
    return hmac.new(key, f"{PLATFORM}:{user_id}".encode(), hashlib.sha256).hexdigest()[:32]


def like_count(post: dict[str, Any]) -> int | None:
    """Likes on a post (Discourse action type 2), if the payload says."""
    if isinstance(post.get("like_count"), int):
        return post["like_count"]
    for action in post.get("actions_summary") or []:
        if action.get("id") == 2 and isinstance(action.get("count"), int):
            return action["count"]
    return None


def parse_time(value: str) -> dt.datetime:
    """Discourse ISO timestamp ("2026-09-20T10:11:12.345Z") as aware UTC."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)


def post_key(platform_post_id: str | int) -> str:
    """Our social_posts id for a ValuePickr post id."""
    return f"{PLATFORM}:{platform_post_id}"


def parse_post(
    post: dict[str, Any],
    topic_id: int,
    slug: str,
    base_url: str,
    key: bytes,
    now: dt.datetime,
) -> dict[str, Any] | None:
    """A social_posts row for one Discourse post, or None if it isn't a live regular post."""
    if post.get("post_type", REGULAR_POST) != REGULAR_POST or is_removed(post):
        return None
    text = clean_cooked(post.get("cooked"))
    if text is None or not post.get("created_at"):
        return None
    number = post.get("post_number")
    return {
        "id": post_key(post["id"]),
        "platform": PLATFORM,
        "topic_id": str(topic_id),
        "platform_post_id": str(post["id"]),
        "post_number": number,
        "url": f"{base_url}/t/{slug}/{topic_id}/{number}",
        "text": text,
        "author_hmac": author_hash(key, post.get("user_id")),
        "likes": like_count(post),
        "created_at": parse_time(post["created_at"]),
        "first_seen_at": now,
        "checked_at": now,
        "linked_at": None,
    }


def new_post_ids(stream: list[int], last_post_id: int | None, backfill: int) -> list[int]:
    """Post ids to fetch: those after `last_post_id`, or the newest `backfill` if None."""
    if last_post_id is None:
        return stream[-backfill:] if backfill > 0 else []
    return [pid for pid in stream if pid > last_post_id]


def title_names_stock(title: str, stocks: list[Stock]) -> bool:
    """True if a topic title contains a stock's news search term (whole words, any case).

    This only chooses which topics to fetch, like the Google News query; deciding what a
    post is about is left to processing/social.py.
    """
    for stock in stocks:
        for term in stock.search_terms:
            if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.IGNORECASE):
                return True
    return False


# --- HTTP --------------------------------------------------------------------------


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Retry-After in seconds (numeric form only), or None."""
    value = response.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


@retry(attempts=3, base_delay=5.0, exceptions=(httpx.TransportError, RetryableHTTPError))
def _get(client: httpx.Client, url: str, limiter: DomainRateLimiter) -> httpx.Response:
    """One polite GET. 429 raises RateLimitedError at once (never retried blindly)."""
    limiter.wait(url)
    response = client.get(url)
    if response.status_code == 429:
        raise RateLimitedError(url, retry_after_seconds(response))
    if response.status_code >= 500:
        raise RetryableHTTPError(response)
    response.raise_for_status()
    return response


class Forum:
    """HTTP access to one Discourse forum: rate limit, robots.txt and 429 handling."""

    def __init__(
        self,
        config: ValuePickrConfig,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.sleep = sleep
        self.limiter = DomainRateLimiter(lambda _domain: config.min_interval_s, sleep=sleep)
        self.robots: RobotFileParser | None = None

    def load_robots(self) -> None:
        """Fetch and parse robots.txt; every later request is checked against it."""
        text = self._request(f"{self.config.base_url}/robots.txt").text
        self.robots = RobotFileParser()
        self.robots.parse(text.splitlines())

    def get_json(self, path: str) -> Any:
        """GET base_url + path as JSON, after checking robots.txt."""
        url = f"{self.config.base_url}{path}"
        if self.robots is None or not self.robots.can_fetch(self.config.user_agent, url):
            raise RobotsDisallowedError(f"robots.txt disallows (or wasn't loaded for) {url}")
        return self._request(url).json()

    def _request(self, url: str) -> httpx.Response:
        """GET with 429 handling: wait Retry-After once if short enough, else abort."""
        try:
            return _get(self.client, url, self.limiter)
        except RateLimitedError as exc:
            wait = exc.retry_after if exc.retry_after is not None else 60.0
            if wait > self.config.max_retry_after_s:
                raise RunAbortedError(f"{exc}; longer than max_retry_after_s") from exc
            logger.warning("Rate limited by %s; waiting %.0fs as asked", url, wait)
            self.sleep(wait)
            try:
                return _get(self.client, url, self.limiter)
            except RateLimitedError as again:
                raise RunAbortedError(f"still rate limited after waiting: {again}") from again


def posts_path(topic_id: int | str, post_ids: list[int] | list[str]) -> str:
    """/t/<id>/posts.json path for a batch of post ids."""
    return f"/t/{topic_id}/posts.json?" + urlencode([("post_ids[]", p) for p in post_ids])


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    """`items` in consecutive slices of at most `size`."""
    return [items[i : i + size] for i in range(0, len(items), size)]


# --- collection --------------------------------------------------------------------


def forget_topic(topic_id: int | str) -> int:
    """Delete every stored post of a topic that's gone upstream. Returns posts deleted."""
    ids = topic_post_ids(PLATFORM, str(topic_id))
    return delete_posts(ids)


def collect_topic(
    forum: Forum, topic_id: int, role: str, backfill: int, key: bytes, now: dt.datetime
) -> TopicResult:
    """Fetch new posts of one topic and delete stored posts no longer in its stream."""
    result = TopicResult(topic_id, role)
    try:
        data = forum.get_json(f"/t/{topic_id}.json")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in GONE_STATUSES:
            raise
        result.deleted = forget_topic(topic_id)
        logger.warning("Topic %s returned HTTP %s: deleted its %d stored post(s)",
                       topic_id, exc.response.status_code, result.deleted)  # fmt: skip
        return result

    stream = [int(pid) for pid in data["post_stream"]["stream"]]
    known = read_topic(PLATFORM, str(topic_id))
    stored = stored_posts(PLATFORM, str(topic_id))
    live = {str(pid) for pid in stream}
    gone = [post_key(pid) for pid in stored if pid not in live]
    result.deleted = delete_posts(gone) if gone else 0

    wanted = new_post_ids(stream, known["last_post_id"] if known else None, backfill)
    inline = {int(p["id"]): p for p in data["post_stream"].get("posts") or []}
    posts = [inline[pid] for pid in wanted if pid in inline]
    for batch in chunked([pid for pid in wanted if pid not in inline], POSTS_PER_REQUEST):
        posts += forum.get_json(posts_path(topic_id, batch))["post_stream"]["posts"]

    slug = data.get("slug") or "-"
    rows = [
        row
        for post in posts
        if (row := parse_post(post, topic_id, slug, forum.config.base_url, key, now))
    ]
    result.new = insert_new_posts(rows)
    last_id = max(wanted, default=known["last_post_id"] if known else None)
    numbers = [p["post_number"] for p in posts if p.get("post_number")]
    upsert_topic(
        {
            "platform": PLATFORM,
            "topic_id": str(topic_id),
            "title": data.get("title") or "",
            "slug": slug,
            "role": role,
            "fetched_at": now,
            "last_post_id": last_id,
            "last_post_number": max(numbers, default=known["last_post_number"] if known else None),
        }
    )
    logger.info("Topic %s (%s): %d new post(s), %d deleted upstream", topic_id, role,
                result.new, result.deleted)  # fmt: skip
    return result


def discover_topics(forum: Forum, stocks: list[Stock]) -> list[int]:
    """Topic ids from /latest.json worth fetching: unconfigured, regular, naming a stock,
    and with posts newer than the last one we fetched."""
    config = forum.config
    topics = forum.get_json("/latest.json")["topic_list"]["topics"]
    picked = []
    for topic in topics:
        if config.topic(topic["id"]) or topic.get("pinned"):
            continue
        if topic.get("archetype", "regular") != "regular":
            continue
        if not title_names_stock(topic.get("title") or "", stocks):
            continue
        known = read_topic(PLATFORM, str(topic["id"]))
        highest = topic.get("highest_post_number")
        if known and known["last_post_number"] and highest and highest <= known["last_post_number"]:
            continue
        picked.append(topic["id"])
    return picked[: config.latest_max_topics]


def recheck_posts(forum: Forum, now: dt.datetime) -> tuple[int, int, int]:
    """Re-fetch the least recently checked posts. Returns (checked, deleted, edited)."""
    due = posts_to_recheck(PLATFORM, forum.config.recheck_posts)
    checked, deleted, edited = 0, 0, 0
    for topic_id, group in due.groupby("topic_id"):
        stored_text = dict(zip(group["platform_post_id"], group["text"], strict=True))
        for batch in chunked(list(stored_text), POSTS_PER_REQUEST):
            try:
                data = forum.get_json(posts_path(topic_id, batch))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in GONE_STATUSES:
                    raise
                deleted += forget_topic(topic_id)
                break
            returned = {str(p["id"]): p for p in data["post_stream"]["posts"]}
            removed = [pid for pid in batch if pid not in returned or is_removed(returned[pid])]
            deleted += delete_posts([post_key(pid) for pid in removed]) if removed else 0
            alive = [pid for pid in batch if pid not in removed]
            texts = {
                post_key(pid): text
                for pid in alive
                if (text := clean_cooked(returned[pid].get("cooked"))) != stored_text[pid]
            }
            mark_checked([post_key(pid) for pid in alive], now, texts)
            checked += len(batch)
            edited += len(texts)
    return checked, deleted, edited


def describe(exc: Exception) -> str:
    """Short error text for results and notifications."""
    return f"{type(exc).__name__}: {exc}"


def hash_key() -> bytes:
    """SOCIAL_HASH_KEY from .env.

    Raises:
        NotConfiguredError: if it's missing or shorter than 16 characters.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    key = os.environ.get(HASH_KEY_ENV, "")
    if len(key) < 16:
        raise NotConfiguredError(
            f"{HASH_KEY_ENV} must be set in .env (16+ random characters), e.g. "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return key.encode()


def collect_all(
    config: ValuePickrConfig,
    stocks: list[Stock],
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    key: bytes | None = None,
) -> RunResult:
    """Run one collection pass (see module docstring). Never raises for per-topic errors."""
    result = RunResult()
    if not config.confirmed:
        result.skipped = "topic ids not confirmed (set confirmed: true in social_sources.yaml)"
        logger.warning("ValuePickr skipped: %s", result.skipped)
        return result
    key = key or hash_key()
    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": config.user_agent, "Accept": "application/json"},
        timeout=config.timeout_s,
        follow_redirects=True,
    )
    forum = Forum(config, client, sleep)
    now = dt.datetime.now(dt.UTC)
    try:
        forum.load_robots()
        jobs = [(t.id, "dedicated" if t.symbol else "general", config.backfill_posts)
                for t in config.topics]  # fmt: skip
        try:
            jobs += [(tid, "discovered", config.discovered_backfill_posts)
                     for tid in discover_topics(forum, stocks)]  # fmt: skip
        except RunAbortedError:
            raise
        except Exception as exc:
            logger.exception("Topic discovery from /latest.json failed")
            result.topics.append(TopicResult(0, "latest", error=describe(exc)))
        for topic_id, role, backfill in jobs:
            try:
                result.topics.append(collect_topic(forum, topic_id, role, backfill, key, now))
            except RunAbortedError:
                raise
            except Exception as exc:
                logger.exception("Topic %s failed", topic_id)
                result.topics.append(TopicResult(topic_id, role, error=describe(exc)))
        result.rechecked, result.recheck_deleted, result.recheck_edited = recheck_posts(forum, now)
    except RunAbortedError as exc:
        result.aborted = str(exc)
        logger.error("ValuePickr run stopped: %s", exc)
    except Exception as exc:  # robots.txt or recheck failure
        result.aborted = describe(exc)
        logger.exception("ValuePickr run failed")
    finally:
        if own_client:
            client.close()
    return result


def log_summary(result: RunResult) -> None:
    """Log per-topic counts, deletion sync and failures."""
    if result.skipped:
        return
    for t in result.topics:
        status = f"FAILED {t.error}" if t.error else f"{t.new} new, {t.deleted} deleted"
        logger.info("valuepickr topic %-8s %-10s %s", t.topic_id, t.role, status)
    logger.info("Deletion sync: %d post(s) re-checked, %d deleted, %d edited",
                result.rechecked, result.recheck_deleted, result.recheck_edited)  # fmt: skip
    for failure in result.failures:
        logger.error("FAILED %s", failure)


def main() -> int:
    """Entry point: one ValuePickr collection pass. Exit code 1 on any failure."""
    setup_logging()
    init_db()
    result = collect_all(load_social_sources(), load_watchlist())
    log_summary(result)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
