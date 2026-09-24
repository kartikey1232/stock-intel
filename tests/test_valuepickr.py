"""Tests for the ValuePickr collector against a fake Discourse forum (no network)."""

import datetime as dt
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import Engine, select

import collectors.valuepickr as vp
from config.loader import Stock
from config.social_sources import Topic, ValuePickrConfig
from storage import db
from storage.social import replace_social_mentions

BASE = "https://forum.example.com"
KEY = b"0123456789abcdef-test-key"
INFY = Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys", "Infy"))
ROBOTS = "User-agent: *\nDisallow: /search\nDisallow: /t/*/*.rss\n"


def config(**overrides) -> ValuePickrConfig:
    values = {
        "base_url": BASE,
        "user_agent": "stock-intel-test (personal non-commercial research)",
        "min_interval_s": 5,
        "max_retry_after_s": 60,
        "backfill_posts": 25,
        "discovered_backfill_posts": 3,
        "recheck_posts": 100,
        "latest_max_topics": 5,
        "confirmed": True,
        "topics": [Topic(10, "Infosys thread", "INFY")],
    }
    return ValuePickrConfig(**{**values, **overrides})


def post(pid: int, number: int, text: str | None = None, **extra) -> dict:
    return {
        "id": pid,
        "post_number": number,
        "post_type": 1,
        "created_at": f"2026-09-{1 + number % 20:02d}T05:00:00.000Z",
        "cooked": f"<p>{text or f'Post {number} about Infosys margins.'}</p>",
        "user_id": 1000 + number % 3,
        "username": f"secret_user_{number}",
        "name": f"Real Name {number}",
        "avatar_template": "/user_avatar/x.png",
        "actions_summary": [{"id": 2, "count": number % 4}],
        **extra,
    }


class FakeForum:
    """In-memory Discourse: topics -> ordered posts; records every request."""

    def __init__(self) -> None:
        self.topics: dict[int, dict] = {}
        self.latest: list[dict] = []
        self.robots = ROBOTS
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []  # queued overrides, e.g. 429s
        self.gone: set[int] = set()

    def add_topic(self, tid: int, title: str, count: int) -> None:
        posts = [post(1000 * tid + n, n) for n in range(1, count + 1)]
        self.topics[tid] = {"title": title, "slug": title.lower().replace(" ", "-"), "posts": posts}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        if path == "/latest.json":
            return httpx.Response(200, json={"topic_list": {"topics": self.latest}})
        parts = path.strip("/").split("/")
        tid = int(parts[1].removesuffix(".json"))
        if tid in self.gone or tid not in self.topics:
            return httpx.Response(404, json={"errors": ["not found"]})
        topic = self.topics[tid]
        live = [p for p in topic["posts"] if not p.get("_gone")]
        if path.endswith("/posts.json"):
            wanted = {int(i) for i in parse_qs(urlsplit(str(request.url)).query)["post_ids[]"]}
            chosen = [p for p in live if p["id"] in wanted]
            return httpx.Response(200, json={"post_stream": {"posts": chosen}})
        return httpx.Response(
            200,
            json={
                "id": tid,
                "title": topic["title"],
                "slug": topic["slug"],
                "highest_post_number": max(p["post_number"] for p in live),
                "post_stream": {"posts": live[:20], "stream": [p["id"] for p in live]},
            },
        )

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


@pytest.fixture
def forum() -> FakeForum:
    f = FakeForum()
    f.add_topic(10, "Infosys thread", 30)
    return f


def run(forum: FakeForum, cfg: ValuePickrConfig | None = None, sleeps: list | None = None):
    client = httpx.Client(
        transport=httpx.MockTransport(forum.handler),
        headers={"User-Agent": (cfg or config()).user_agent},
    )
    sleep = (sleeps if sleeps is not None else []).append
    return vp.collect_all(cfg or config(), [INFY], client=client, sleep=sleep, key=KEY)


def stored(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(db.SOCIAL_POSTS).order_by(db.SOCIAL_POSTS.c.post_number))
        return [dict(r) for r in rows.mappings()]


# --- parsing -----------------------------------------------------------------------


def test_cooked_html_loses_quotes_mentions_code_and_images() -> None:
    cooked = (
        '<aside class="quote" data-username="alice"><div class="title">alice:</div>'
        "<blockquote><p>Quoted Reliance text</p></blockquote></aside>"
        '<p>Agree with <a class="mention" href="/u/bob">@bob</a> on Infosys.<br>Margins held.</p>'
        "<pre><code>x = 1</code></pre><p><img src='a.png' alt='chart'>Done &amp; dusted</p>"
        '<aside class="onebox"><article>Some linked page about TCS</article></aside>'
    )
    text = vp.clean_cooked(cooked)
    assert text == "Agree with on Infosys.\nMargins held.\nDone & dusted"
    for leaked in ("alice", "bob", "Reliance", "TCS", "x = 1"):
        assert leaked not in text


def test_parse_post_stores_no_identity_only_a_keyed_hash() -> None:
    now = dt.datetime(2026, 9, 24, tzinfo=dt.UTC)
    row = vp.parse_post(post(7, 3), 10, "infy", BASE, KEY, now)
    assert row is not None
    assert "secret_user" not in json.dumps(row, default=str)
    assert "Real Name" not in json.dumps(row, default=str)
    assert row["url"] == f"{BASE}/t/infy/10/3"
    assert row["likes"] == 3
    assert row["created_at"] == dt.datetime(2026, 9, 4, 5, tzinfo=dt.UTC)
    same_author = vp.parse_post(post(8, 6), 10, "infy", BASE, KEY, now)  # user_id 1000 again
    assert row["author_hmac"] == same_author["author_hmac"]
    other_key = vp.parse_post(post(7, 3), 10, "infy", BASE, b"another-key-000000", now)
    assert other_key["author_hmac"] != row["author_hmac"]


@pytest.mark.parametrize(
    "extra",
    [
        {"post_type": 3},  # "small action", e.g. topic closed
        {"user_deleted": True},
        {"hidden": True},
        {"deleted_at": "2026-09-20T00:00:00Z"},
        {"cooked": "<p>(post deleted by author)</p>"},
        {"cooked": "<p>(post withdrawn by author, will be automatically deleted in 24 hours)</p>"},
    ],
)
def test_removed_and_non_regular_posts_are_not_stored(extra: dict) -> None:
    now = dt.datetime(2026, 9, 24, tzinfo=dt.UTC)
    assert vp.parse_post({**post(7, 3), **extra}, 10, "infy", BASE, KEY, now) is None


def test_new_post_ids() -> None:
    stream = [5, 6, 9, 12]
    assert vp.new_post_ids(stream, None, 2) == [9, 12]
    assert vp.new_post_ids(stream, 6, 2) == [9, 12]
    assert vp.new_post_ids(stream, 12, 2) == []


# --- collection --------------------------------------------------------------------


def test_nothing_is_requested_until_topics_are_confirmed(engine: Engine, forum) -> None:
    result = run(forum, config(confirmed=False))
    assert forum.requests == [] and result.skipped and result.failures == []


def test_missing_hash_key_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(vp.HASH_KEY_ENV, raising=False)
    monkeypatch.setattr(vp, "PROJECT_ROOT", tmp_path)  # no .env there
    with pytest.raises(vp.NotConfiguredError):
        vp.hash_key()


def test_first_run_backfills_newest_posts_politely(engine: Engine, forum) -> None:
    sleeps: list[float] = []
    result = run(forum, sleeps=sleeps)
    assert result.failures == []
    rows = stored(engine)
    assert [r["post_number"] for r in rows] == list(range(6, 31))  # newest 25 of 30
    # robots, discovery, topic (posts 1-20 inline), one posts.json batch for 21-30, recheck.
    assert forum.paths()[:4] == ["/robots.txt", "/latest.json", "/t/10.json", "/t/10/posts.json"]
    assert all(r.headers["User-Agent"].startswith("stock-intel-test") for r in forum.requests)
    assert len(sleeps) == len(forum.requests) - 1  # a pause before every request but the first
    assert all(s == pytest.approx(5, abs=0.5) for s in sleeps)


def test_incremental_run_fetches_only_newer_posts_and_syncs_deletions(engine, forum) -> None:
    run(forum)
    mention = {"post_id": "valuepickr:10010", "symbol": "INFY", "method": "thread",
               "matched_alias": None, "confidence": 0.9}  # fmt: skip
    replace_social_mentions(["valuepickr:10010"], [mention])
    forum.topics[10]["posts"] += [post(10031, 31), post(10032, 32)]
    forum.topics[10]["posts"][9]["_gone"] = True  # post 10 deleted upstream
    forum.requests.clear()

    result = run(forum, config(recheck_posts=0))
    assert result.topics[0].new == 2 and result.topics[0].deleted == 1
    batches = [parse_qs(urlsplit(str(r.url)).query) for r in forum.requests
               if r.url.path.endswith("/posts.json")]  # fmt: skip
    assert batches == [{"post_ids[]": ["10031", "10032"]}]
    numbers = [r["post_number"] for r in stored(engine)]
    assert 10 not in numbers and numbers[-2:] == [31, 32]
    with engine.connect() as conn:  # derived rows went with the post
        assert conn.execute(select(db.SOCIAL_MENTIONS)).all() == []


def test_topic_gone_upstream_deletes_its_posts(engine: Engine, forum) -> None:
    run(forum)
    forum.gone.add(10)
    result = run(forum, config(recheck_posts=0))
    assert result.failures == [] and result.topics[0].deleted == 25
    assert stored(engine) == []


def test_429_waits_retry_after_once_then_continues(engine: Engine, forum) -> None:
    forum.responses = [httpx.Response(200, text=ROBOTS),
                       httpx.Response(429, headers={"Retry-After": "7"})]  # fmt: skip
    sleeps: list[float] = []
    result = run(forum, sleeps=sleeps)
    assert result.failures == []
    assert 7 in sleeps
    assert len(stored(engine)) == 25


def test_repeated_429_stops_the_run(engine: Engine, forum) -> None:
    forum.responses = [httpx.Response(200, text=ROBOTS)] + [
        httpx.Response(429, headers={"Retry-After": "3"}) for _ in range(2)
    ]
    result = run(forum)
    assert result.aborted and "still rate limited" in result.aborted
    assert len(forum.requests) == 3  # no further requests after the second 429


def test_long_retry_after_stops_without_waiting(engine: Engine, forum) -> None:
    forum.responses = [httpx.Response(200, text=ROBOTS),
                       httpx.Response(429, headers={"Retry-After": "3600"})]  # fmt: skip
    sleeps: list[float] = []
    result = run(forum, sleeps=sleeps)
    assert result.aborted and 3600 not in sleeps
    assert len(forum.requests) == 2


def test_robots_disallow_is_honoured(engine: Engine, forum) -> None:
    forum.robots = "User-agent: *\nDisallow: /t/\n"
    result = run(forum)
    assert "RobotsDisallowedError" in result.topics[0].error
    assert forum.paths() == ["/robots.txt", "/latest.json"]


def test_recheck_deletes_removed_posts_and_updates_edited_text(engine: Engine, forum) -> None:
    run(forum, config(backfill_posts=3))  # posts 28-30
    posts = forum.topics[10]["posts"]
    posts[27]["cooked"] = "<p>Edited: Infosys guidance cut.</p>"  # post 28
    posts[28]["user_deleted"] = True  # post 29
    posts[29]["_gone"] = True  # post 30: gone entirely
    with engine.begin() as conn:
        conn.execute(db.SOCIAL_POSTS.update().values(linked_at=dt.datetime.now(dt.UTC)))

    result = run(forum, config(backfill_posts=3))
    rows = stored(engine)
    assert [r["post_number"] for r in rows] == [28]
    assert rows[0]["text"] == "Edited: Infosys guidance cut."
    assert rows[0]["linked_at"] is None  # re-linked next time
    assert result.recheck_deleted == 1 and result.recheck_edited == 1  # 30 left the stream


def test_latest_topics_naming_a_stock_are_discovered(engine: Engine, forum) -> None:
    forum.add_topic(20, "Infosys Q2 results discussion", 5)
    forum.add_topic(21, "Random chat", 5)
    forum.add_topic(22, "Infosys pinned rules", 5)
    forum.latest = [
        {"id": 20, "title": "Infosys Q2 results discussion", "highest_post_number": 5},
        {"id": 21, "title": "Random chat", "highest_post_number": 5},
        {"id": 22, "title": "Infosys pinned rules", "highest_post_number": 5, "pinned": True},
        {"id": 10, "title": "Infosys thread", "highest_post_number": 30},  # configured
    ]
    result = run(forum, config(recheck_posts=0))
    roles = {t.topic_id: t.role for t in result.topics}
    assert roles == {10: "dedicated", 20: "discovered"}
    assert sum(1 for r in stored(engine) if r["topic_id"] == "20") == 3  # discovered backfill

    forum.requests.clear()
    run(forum, config(recheck_posts=0))  # no new posts in topic 20: not fetched again
    assert "/t/20.json" not in forum.paths()
