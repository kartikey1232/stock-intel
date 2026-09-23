import datetime as dt
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import Engine, select

import collectors.news as news
from config.loader import Stock
from config.news_sources import Feed, NewsConfig, NewsSourcesError, load_news_sources
from storage import db

PUBLISHER_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>ET Markets</title>
<item>
  <title>Infosys shares jump 4% on &amp; large deal win</title>
  <link>https://economictimes.indiatimes.com/markets/stocks/news/infy-deal/articleshow/1.cms?utm_source=rss&amp;from=mdr</link>
  <description>&lt;p&gt;Infosys &lt;b&gt;won&lt;/b&gt; a deal.&lt;/p&gt;</description>
  <pubDate>Wed, 23 Sep 2026 12:34:54 +0530</pubDate>
</item>
<item>
  <title>Market wrap with no date</title>
  <link>https://economictimes.indiatimes.com/markets/wrap/</link>
</item>
<item><title>No link, skipped</title></item>
</channel></rss>"""

GOOGLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>"Infosys" - Google News</title>
<item>
  <title>Infosys Q2 preview: what to expect - Upstox</title>
  <link>https://news.google.com/rss/articles/CBMiABC123?oc=5</link>
  <pubDate>Wed, 16 Sep 2026 13:46:21 GMT</pubDate>
  <description>&lt;a href="https://news.google.com/rss/articles/CBMiABC123?oc=5"&gt;
    Infosys Q2 preview&lt;/a&gt;&amp;nbsp;&lt;font&gt;Upstox&lt;/font&gt;</description>
  <source url="https://upstox.com">Upstox</source>
</item>
</channel></rss>"""

INFY = Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys", "Infy"))
TCS = Stock(
    "TCS",
    "TCS.NS",
    "Tata Consultancy Services Ltd",
    "IT",
    ("TCS",),
    ("Tata Consultancy Services", "TCS shares"),
)
CONFIG = NewsConfig(
    user_agent="stock-intel-test",
    timeout_s=5,
    rate_limits={"default": 0},
    google_news_url="https://news.google.com/rss/search",
    google_news_params={"hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
    google_news_window="7d",
    feeds=[
        Feed("et_markets", "The Economic Times", "https://economictimes.indiatimes.com/rss.cms"),
        Feed("mint_markets", "Mint", "https://www.livemint.com/rss/markets"),
    ],
)
SEEN = dt.datetime(2026, 9, 23, 8, 0, tzinfo=dt.UTC)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr("utils.retry.time.sleep", lambda _s: None)
    return engine


def mock_client(routes: dict[str, list[httpx.Response]], calls: list[str]) -> httpx.Client:
    """Client whose responses come from `routes` (host -> queue of responses)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        queue = routes[request.url.host]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.Client(transport=httpx.MockTransport(handler))


def stored_articles(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(db.ARTICLES)).mappings()]


# --- URL normalisation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://x.com/a/b/?utm_source=rss&utm_medium=feed", "https://x.com/a/b"),
        ("http://WWW.X.com/a#comments", "https://www.x.com/a"),
        ("https://x.com:443/a/", "https://x.com/a"),
        ("https://x.com/a?b=2&a=1&fbclid=zz&from=mdr", "https://x.com/a?a=1&b=2"),
        ("https://x.com/a?id=42", "https://x.com/a?id=42"),
        (
            "https://news.google.com/rss/articles/CBMiXYZ?oc=5",
            "https://news.google.com/rss/articles/CBMiXYZ",
        ),
        ("https://x.com/Case/Sensitive", "https://x.com/Case/Sensitive"),
    ],
)
def test_normalise_url(raw: str, expected: str) -> None:
    assert news.normalise_url(raw) == expected


def test_tracking_variants_share_one_id() -> None:
    variants = [
        "https://x.com/story/",
        "http://x.com/story?utm_campaign=a",
        "https://X.com/story#top",
    ]
    assert len({news.article_id(v) for v in variants}) == 1
    assert news.article_id("https://x.com/other") != news.article_id(variants[0])


# --- query building ----------------------------------------------------------------


def test_google_news_query_uses_terms_and_india_edition() -> None:
    url = news.google_news_url(TCS, CONFIG)
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert params["q"] == '"Tata Consultancy Services" OR "TCS shares" when:7d'
    assert (params["hl"], params["gl"], params["ceid"]) == ("en-IN", "IN", "IN:en")


def test_default_search_terms_use_name_without_ltd_and_first_alias() -> None:
    assert INFY.search_terms == ("Infosys",)  # name and first alias dedupe
    stock = Stock("X", "X.NS", "Foo Industries Limited", "S", ("FooCo", "Foo"))
    assert stock.search_terms == ("Foo Industries", "FooCo")


def test_build_sources_has_feeds_then_one_google_query_per_stock() -> None:
    names = [s.name for s in news.build_sources(CONFIG, [INFY, TCS])]
    assert names == ["et_markets", "mint_markets", "google:INFY", "google:TCS"]


# --- parsing -----------------------------------------------------------------------


def test_parse_publisher_feed() -> None:
    source = news.Source("et_markets", "https://e/rss", "The Economic Times")
    rows = news.parse_feed(PUBLISHER_RSS, source, SEEN)

    assert len(rows) == 2  # entry without a link is skipped
    first = rows[0]
    assert first["title"] == "Infosys shares jump 4% on & large deal win"
    assert first["summary"] == "Infosys won a deal."
    assert first["source"] == "The Economic Times"
    assert first["url"].endswith("/articleshow/1.cms")  # utm_source and from=mdr stripped
    assert first["published_at"] == dt.datetime(2026, 9, 23, 7, 4, 54, tzinfo=dt.UTC)  # IST->UTC
    assert first["first_seen_at"] == SEEN
    assert first["fetched_via"] == "et_markets"
    assert rows[1]["published_at"] is None  # missing date stays missing


def test_parse_google_feed_uses_entry_source_and_strips_title_suffix() -> None:
    source = news.Source("google:INFY", "https://news.google.com/rss/search", None, is_google=True)
    [row] = news.parse_feed(GOOGLE_RSS, source, SEEN)
    assert row["title"] == "Infosys Q2 preview: what to expect"
    assert row["source"] == "Upstox"
    assert row["summary"] is None
    assert row["url"] == "https://news.google.com/rss/articles/CBMiABC123"
    assert row["published_at"] == dt.datetime(2026, 9, 16, 13, 46, 21, tzinfo=dt.UTC)


def test_html_error_page_is_not_a_feed() -> None:
    with pytest.raises(news.FeedError):
        news.parse_feed(b"<html><body>403 Forbidden", news.Source("x", "u", "p"), SEEN)


# --- collection --------------------------------------------------------------------


def routes_ok() -> dict[str, list[httpx.Response]]:
    return {
        "economictimes.indiatimes.com": [httpx.Response(200, content=PUBLISHER_RSS)],
        "www.livemint.com": [httpx.Response(200, content=PUBLISHER_RSS)],
        "news.google.com": [httpx.Response(200, content=GOOGLE_RSS)],
    }


def test_rerun_is_idempotent_and_keeps_first_seen(engine: Engine) -> None:
    calls: list[str] = []
    first = news.collect_all(CONFIG, [INFY], client=mock_client(routes_ok(), calls))
    assert [(r.name, r.found, r.new) for r in first] == [
        ("et_markets", 2, 2),
        ("mint_markets", 2, 0),  # same articles as ET: already stored
        ("google:INFY", 1, 1),
    ]
    before = {a["id"]: a["first_seen_at"] for a in stored_articles(engine)}

    second = news.collect_all(CONFIG, [INFY], client=mock_client(routes_ok(), calls))
    assert sum(r.new for r in second) == 0
    after = stored_articles(engine)
    assert len(after) == 3
    assert {a["id"]: a["first_seen_at"] for a in after} == before
    assert {a["fetched_via"] for a in after} == {"et_markets", "google:INFY"}
    assert {a["text_status"] for a in after} == {"pending"}


def test_one_failing_feed_does_not_stop_others(engine: Engine) -> None:
    routes = routes_ok()
    routes["economictimes.indiatimes.com"] = [httpx.Response(403, text="Forbidden")]
    calls: list[str] = []
    results = news.collect_all(CONFIG, [INFY], client=mock_client(routes, calls))

    by_name = {r.name: r for r in results}
    assert "403" in by_name["et_markets"].error
    assert by_name["mint_markets"].new == 2
    assert by_name["google:INFY"].new == 1
    assert calls.count("economictimes.indiatimes.com") == 1  # 4xx is not retried


def test_server_errors_are_retried(engine: Engine) -> None:
    routes = routes_ok()
    routes["www.livemint.com"] = [
        httpx.Response(503),
        httpx.Response(429),
        httpx.Response(200, content=PUBLISHER_RSS),
    ]
    calls: list[str] = []
    results = news.collect_all(CONFIG, [], client=mock_client(routes, calls))
    assert {r.name: r.error for r in results}["mint_markets"] is None
    assert calls.count("www.livemint.com") == 3


# --- rate limiting -----------------------------------------------------------------


def test_rate_limiter_spaces_requests_per_domain() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    intervals = {"news.google.com": 3.0}
    limiter = news.DomainRateLimiter(
        lambda d: intervals.get(d, 1.0), clock=lambda: now[0], sleep=sleep
    )
    limiter.wait("https://news.google.com/rss/search?q=a")
    limiter.wait("https://www.livemint.com/rss")  # other domain: no wait
    now[0] += 1.0
    limiter.wait("https://news.google.com/rss/search?q=b")  # 1s later: waits 2s more
    assert sleeps == [pytest.approx(2.0)]


# --- config ------------------------------------------------------------------------


def test_real_news_sources_file_loads() -> None:
    config = load_news_sources()
    assert config.google_news_params == {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    assert config.feeds and all(f.url.startswith("https://") for f in config.feeds)
    assert config.min_interval("news.google.com") >= config.min_interval("unknown.example")


def test_duplicate_feed_names_rejected(tmp_path: Path) -> None:
    path = tmp_path / "news.yaml"
    path.write_text(
        "user_agent: x\ngoogle_news: {url: https://g}\nfeeds:\n"
        "  - {name: a, publisher: P, url: https://a}\n"
        "  - {name: a, publisher: P, url: https://b}\n",
        encoding="utf-8",
    )
    with pytest.raises(NewsSourcesError, match="duplicate"):
        load_news_sources(path)
