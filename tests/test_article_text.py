import datetime as dt
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, select

import collectors.article_text as article_text
from config.news_sources import NewsConfig
from storage import db
from utils.http import DomainRateLimiter

CONFIG = NewsConfig(
    user_agent="stock-intel-test",
    timeout_s=5,
    rate_limits={"default": 0},
    google_news_url="https://news.google.com/rss/search",
    google_news_params={},
    google_news_window="7d",
    text_max_attempts=3,
    text_min_chars=300,
)
BODY = " ".join(
    f"Paragraph {i}: Infosys reported strong quarterly numbers and raised guidance."
    for i in range(12)
)
ARTICLE_HTML = f"""<html><head><title>Infosys beats</title></head><body>
<nav>Home | Markets | Subscribe to Prime</nav>
<article><h1>Infosys beats estimates</h1>
{"".join(f"<p>{s}.</p>" for s in BODY.split(". "))}
</article><footer>Copyright</footer></body></html>"""
PAYWALL_HTML = ARTICLE_HTML.replace(
    "</head>",
    '<script type="application/ld+json">{"@type":"NewsArticle",'
    '"isAccessibleForFree": "false"}</script></head>',
)
ROBOTS_OK = "User-agent: *\nDisallow: /private/\n"


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr("utils.retry.time.sleep", lambda _s: None)
    return engine


def add_article(url: str, source: str = "Mint") -> str:
    row = {
        "id": url[-32:].rjust(32, "0"),
        "url": url,
        "source": source,
        "title": "Original title",
        "summary": "Original summary",
        "published_at": None,
        "first_seen_at": dt.datetime(2026, 9, 23, tzinfo=dt.UTC),
        "fetched_via": "test",
    }
    db.insert_new_articles([row])
    return row["id"]


def article(engine: Engine, article_id: str) -> dict:
    with engine.connect() as conn:
        stmt = select(db.ARTICLES).where(db.ARTICLES.c.id == article_id)
        return dict(conn.execute(stmt).mappings().one())


def run(pages: dict[str, httpx.Response], robots: httpx.Response | None = None) -> list[str]:
    """Run one extraction pass against mocked pages; returns the paths requested."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return robots or httpx.Response(200, text=ROBOTS_OK)
        return pages[request.url.path]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    article_text.extract_pending(CONFIG, client=client)
    return requested


def html(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


# --- status handling ---------------------------------------------------------------


def test_successful_extraction_stores_text(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/news/infosys-beats")
    run({"/news/infosys-beats": html(ARTICLE_HTML)})
    row = article(engine, aid)
    assert row["text_status"] == "ok"
    assert "raised guidance" in row["text"]
    assert "Subscribe to Prime" not in row["text"]  # boilerplate removed
    assert row["text_attempts"] == 1 and row["text_error"] is None


@pytest.mark.parametrize(
    "response",
    [html(PAYWALL_HTML), html("<html>Payment required</html>", status=402)],
    ids=["isAccessibleForFree", "http-402"],
)
def test_paywalled_page_keeps_title_and_summary(engine: Engine, response) -> None:
    aid = add_article("https://www.livemint.com/premium/story")
    run({"/premium/story": response})
    row = article(engine, aid)
    assert row["text_status"] == "paywalled"
    assert row["text"] is None
    assert (row["title"], row["summary"]) == ("Original title", "Original summary")


def test_gone_page_fails_immediately(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/deleted")
    run({"/deleted": html("gone", status=404)})
    assert article(engine, aid)["text_status"] == "failed"


def test_non_html_fails(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/report.pdf")
    run(
        {
            "/report.pdf": httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        }
    )
    row = article(engine, aid)
    assert row["text_status"] == "failed" and "not HTML" in row["text_error"]


def test_too_little_text_is_retried(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/stub")
    run({"/stub": html("<html><body><p>Too short.</p></body></html>")})
    row = article(engine, aid)
    assert row["text_status"] == "pending" and row["text_attempts"] == 1
    assert "extracted only" in row["text_error"]


def test_google_news_links_are_skipped_without_a_request(engine: Engine) -> None:
    aid = add_article("https://news.google.com/rss/articles/CBMiXYZ", source="Upstox")
    assert run({}) == []
    row = article(engine, aid)
    assert row["text_status"] == "skipped" and row["text_attempts"] == 0


# --- robots.txt --------------------------------------------------------------------


def test_robots_disallow_skips_without_fetching_page(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/private/story")
    requested = run({"/private/story": html(ARTICLE_HTML)})
    assert requested == ["/robots.txt"]
    row = article(engine, aid)
    assert row["text_status"] == "skipped" and "robots.txt" in row["text_error"]


def test_robots_403_disallows_everything(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/news/a")
    requested = run({"/news/a": html(ARTICLE_HTML)}, robots=httpx.Response(403))
    assert requested == ["/robots.txt"]
    assert article(engine, aid)["text_status"] == "skipped"


def test_missing_robots_allows_everything(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/news/a")
    run({"/news/a": html(ARTICLE_HTML)}, robots=httpx.Response(404))
    assert article(engine, aid)["text_status"] == "ok"


def test_robots_crawl_delay_raises_domain_interval() -> None:
    limiter = DomainRateLimiter(lambda _d: 1.0, clock=lambda: 0.0, sleep=lambda _s: None)
    robots_txt = "User-agent: *\nCrawl-delay: 10\n"
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=robots_txt))
    )
    cache = article_text.RobotsCache(client, limiter, "stock-intel-test")
    assert cache.allowed("https://slow.example/a")
    assert limiter._overrides["slow.example"] == 10.0


# --- retry cap ---------------------------------------------------------------------


def test_blocked_page_is_retried_then_capped(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/blocked")
    pages = {"/blocked": html("denied", status=403)}

    for expected_attempts in (1, 2):
        run(pages)
        row = article(engine, aid)
        assert (row["text_status"], row["text_attempts"]) == ("pending", expected_attempts)
        assert "blocked" in row["text_error"]

    run(pages)
    row = article(engine, aid)
    assert (row["text_status"], row["text_attempts"]) == ("failed", 3)

    requested = run(pages)  # capped: no longer selected, so no request at all
    assert "/blocked" not in requested
    assert article(engine, aid)["text_attempts"] == 3


def test_server_errors_count_as_one_attempt_per_run(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/flaky")
    requested = run({"/flaky": html("oops", status=503)})
    assert requested.count("/flaky") == 3  # retried within the run by utils.http.fetch
    row = article(engine, aid)
    assert (row["text_status"], row["text_attempts"]) == ("pending", 1)


# --- ordering ----------------------------------------------------------------------


def test_interleave_by_domain_round_robins() -> None:
    urls = ["https://a.com/1", "https://a.com/2", "https://a.com/3", "https://b.com/1"]
    ordered = article_text.interleave_by_domain([{"url": u} for u in urls])
    assert [a["url"] for a in ordered] == [
        "https://a.com/1",
        "https://b.com/1",
        "https://a.com/2",
        "https://a.com/3",
    ]


# --- boilerplate -------------------------------------------------------------------

BOILERPLATE_CONFIG = NewsConfig(
    **{
        **CONFIG.__dict__,
        "text_boilerplate": {
            "*": [r"\(?Disclaimer:"],
            "www.livemint.com": ["Top Trending Stocks:", "Catch all the Business News"],
        },
    }
)


def test_boilerplate_lines_are_removed_per_domain() -> None:
    text = (
        "Infosys raised guidance.\n"
        "Top Trending Stocks: SBI Share Price, HDFC Bank Share Price\n"
        "(Disclaimer: views are the author's own)\n"
        "Margins improved."
    )
    mint = article_text.boilerplate_patterns(BOILERPLATE_CONFIG, "https://www.livemint.com/a")
    other = article_text.boilerplate_patterns(BOILERPLATE_CONFIG, "https://other.com/a")
    assert article_text.strip_boilerplate(text, mint) == (
        "Infosys raised guidance.\nMargins improved."
    )
    assert "Top Trending Stocks" in article_text.strip_boilerplate(text, other)  # not its rule
    assert "Disclaimer" not in article_text.strip_boilerplate(text, other)  # '*' applies


def test_extracted_text_has_boilerplate_removed(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/news/with-footer")
    page = ARTICLE_HTML.replace(
        "</article>", "<p>Top Trending Stocks: SBI Share Price, HDFC Bank Share Price</p></article>"
    )
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, text=ROBOTS_OK) if r.url.path == "/robots.txt" else html(page)
            )
        )
    )
    article_text.extract_pending(BOILERPLATE_CONFIG, client=client)
    row = article(engine, aid)
    assert row["text_status"] == "ok" and "Top Trending" not in row["text"]


def test_reclean_updates_stored_text(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/news/old")
    body = "Infosys raised its revenue guidance for the year. " * 10
    db.update_article_text(aid, "ok", 1, text=f"{body}\nCatch all the Business News on Live Mint.")
    assert article_text.reclean_stored(BOILERPLATE_CONFIG) == 1
    assert article(engine, aid)["text"] == body.strip()
    assert article_text.reclean_stored(BOILERPLATE_CONFIG) == 0  # idempotent


def test_reclean_sends_mostly_boilerplate_text_back_to_pending(engine: Engine) -> None:
    aid = add_article("https://www.livemint.com/news/brief")
    db.update_article_text(aid, "ok", 1, text="(Disclaimer: long legal text)\n" + "x" * 50)
    article_text.reclean_stored(BOILERPLATE_CONFIG)
    row = article(engine, aid)
    assert (row["text_status"], row["text_attempts"], row["text"]) == ("pending", 1, None)
    assert "after boilerplate removal" in row["text_error"]
