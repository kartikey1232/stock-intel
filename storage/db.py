"""Database schema, engine setup, idempotent upserts and reads.

The database location comes from DB_PATH in .env (default: data/stock_intel.db). Relative
paths are resolved against the project root. Upserts use the dialect's native
INSERT ... ON CONFLICT so the same code works on SQLite now and PostgreSQL later.
"""

import datetime as dt
import logging
import os
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Engine,
    Float,
    Integer,
    String,
    Table,
    Text,
    TypeDecorator,
    bindparam,
    create_engine,
    delete,
    event,
    exists,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateColumn, CreateIndex

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "stock_intel.db"
UPSERT_CHUNK_SIZE = 500  # keeps each statement well under SQLite's bound-parameter limit


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Timezone-aware UTC datetime that round-trips correctly on SQLite and PostgreSQL."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"Naive datetime not allowed; use UTC-aware values: {value!r}")
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: dt.datetime | None, dialect: Any) -> dt.datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


class Base(DeclarativeBase):
    pass


class Price(Base):
    """Raw daily OHLCV bar for one symbol."""

    __tablename__ = "prices"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[int | None] = mapped_column(BigInteger)
    fetched_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class Indicator(Base):
    """Technical indicators computed from prices for one symbol and date."""

    __tablename__ = "indicators"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    rsi_14: Mapped[float | None] = mapped_column(Float)
    macd: Mapped[float | None] = mapped_column(Float)
    macd_signal: Mapped[float | None] = mapped_column(Float)
    macd_hist: Mapped[float | None] = mapped_column(Float)
    sma_20: Mapped[float | None] = mapped_column(Float)
    sma_50: Mapped[float | None] = mapped_column(Float)
    sma_200: Mapped[float | None] = mapped_column(Float)
    ema_20: Mapped[float | None] = mapped_column(Float)
    bb_upper: Mapped[float | None] = mapped_column(Float)
    bb_middle: Mapped[float | None] = mapped_column(Float)
    bb_lower: Mapped[float | None] = mapped_column(Float)
    atr_14: Mapped[float | None] = mapped_column(Float)
    volume_sma_20: Mapped[float | None] = mapped_column(Float)


class CorporateActionRow(Base):
    """A corporate action (split, bonus, demerger...) used to build adjusted prices.

    Seeded from config/corporate_actions.yaml, which is the source of truth.
    """

    __tablename__ = "corporate_actions"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ex_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False)
    price_factor: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class Article(Base):
    """A raw news article as first seen in a feed. Never overwritten once stored.

    `id` is a hash of the normalised URL. `published_at` comes from the feed and may be
    wrong or missing; `first_seen_at` is when we first fetched it, and is the only
    timestamp backtests may rely on.
    """

    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    fetched_via: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    text: Mapped[str | None] = mapped_column(Text)
    text_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    text_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    text_error: Mapped[str | None] = mapped_column(Text)
    story_id: Mapped[str | None] = mapped_column(String(32), index=True)
    # When entity linking last ran on this article; reset whenever its text changes.
    linked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)


class ArticleMention(Base):
    """A watchlist stock an article is about, with how strongly (processing/entities.py).

    One row per (article, symbol). `location` is the strongest place it was found
    (title > summary > body); `mention_count` counts matches across all locations.
    """

    __tablename__ = "article_mentions"

    article_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
    matched_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str] = mapped_column(String(16), nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)


class ArticleSentiment(Base):
    """Sentiment of an article towards one stock, per model (processing/sentiment.py).

    Keyed by model_name so another model can be added alongside without overwriting.
    model_version is the exact model revision (commit hash) used.
    """

    __tablename__ = "article_sentiment"

    article_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    p_positive: Mapped[float] = mapped_column(Float, nullable=False)
    p_negative: Mapped[float] = mapped_column(Float, nullable=False)
    p_neutral: Mapped[float] = mapped_column(Float, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)  # p_positive - p_negative
    computed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class NewsDaily(Base):
    """Per-stock news sentiment for one IST trading session, counted by story."""

    __tablename__ = "news_daily"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    model_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    story_count: Mapped[int] = mapped_column(Integer, nullable=False)
    article_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mean_score: Mapped[float] = mapped_column(Float, nullable=False)
    weighted_score: Mapped[float] = mapped_column(Float, nullable=False)
    strong_negative_stories: Mapped[int] = mapped_column(Integer, nullable=False)
    # When the last article in this session was first seen: a backtest may only use
    # this row from that moment on.
    latest_first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class Filing(Base):
    """A raw exchange filing (announcement). Written by a filings collector or importer.

    `category` is the exchange's own label; `filing_type`/`filing_tags` are ours, set by
    processing/filing_categories.py. Attachments live on disk under data/filings/.
    """

    __tablename__ = "filings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    exchange_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    filed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    category: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    attachment_url: Mapped[str | None] = mapped_column(Text)
    attachment_path: Mapped[str | None] = mapped_column(Text)
    attachment_sha256: Mapped[str | None] = mapped_column(String(64))
    duplicate_of: Mapped[str | None] = mapped_column(String(64), index=True)
    filing_type: Mapped[str | None] = mapped_column(String(32), index=True)
    filing_tags: Mapped[str | None] = mapped_column(String(255))


class PendingAction(Base):
    """A corporate action announced in a filing, compared with corporate_actions.yaml.

    Rebuilt by processing/filing_categories.py; the YAML itself is never edited
    automatically.
    """

    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # filing id + action type
    filing_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ratio: Mapped[str | None] = mapped_column(String(32))
    price_factor: Mapped[float | None] = mapped_column(Float)
    record_date: Mapped[dt.date | None] = mapped_column(Date)
    ex_date: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    filed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)


class Result(Base):
    """One reported line item for one quarter, in long format (processing/results.py).

    Long because companies report different items (banks: interest earned, NPAs...).
    Monetary values are in ₹ crore; EPS in ₹ per share; ratios in percent.
    """

    __tablename__ = "results"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_end: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    basis: Mapped[str] = mapped_column(String(16), primary_key=True)  # standalone | consolidated
    metric: Mapped[str] = mapped_column(String(128), primary_key=True)
    fiscal_quarter: Mapped[str] = mapped_column(String(8), nullable=False)  # e.g. FY27Q1
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)  # INR crore | INR/share | %
    source: Mapped[str] = mapped_column(String(8), nullable=False)  # xbrl | pdf
    trust: Mapped[str] = mapped_column(String(8), nullable=False)  # high (xbrl) | low (pdf)
    filing_id: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    flag: Mapped[str | None] = mapped_column(Text)  # validation warning, if any
    flag_reviewed: Mapped[str | None] = mapped_column(Text)  # acknowledgement reason, if any


TEXT_STATUSES = ("pending", "ok", "paywalled", "failed", "skipped")

PRICES: Table = Price.__table__  # type: ignore[assignment]
INDICATORS: Table = Indicator.__table__  # type: ignore[assignment]
CORPORATE_ACTIONS: Table = CorporateActionRow.__table__  # type: ignore[assignment]
ACTION_COLUMNS = list(CORPORATE_ACTIONS.columns.keys())
ARTICLES: Table = Article.__table__  # type: ignore[assignment]
MENTIONS: Table = ArticleMention.__table__  # type: ignore[assignment]
SENTIMENT: Table = ArticleSentiment.__table__  # type: ignore[assignment]
NEWS_DAILY: Table = NewsDaily.__table__  # type: ignore[assignment]
FILINGS: Table = Filing.__table__  # type: ignore[assignment]
PENDING_ACTIONS: Table = PendingAction.__table__  # type: ignore[assignment]
RESULTS: Table = Result.__table__  # type: ignore[assignment]
KEY_COLUMNS = ("symbol", "date")


def project_path(path: str | Path) -> Path:
    """Resolve a stored path: relative paths are relative to the project root.

    File paths are stored project-relative so the project folder can be moved.
    """
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def to_project_relative(path: Path) -> str:
    """`path` relative to the project root if it's inside it, else as an absolute path."""
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def resolve_db_url() -> str:
    """Return the SQLAlchemy URL for the database configured by DB_PATH in .env."""
    load_dotenv()
    path = Path(os.getenv("DB_PATH") or DEFAULT_DB_PATH)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def create_db_engine(url: str) -> Engine:
    """Create an engine; SQLite gets WAL mode so the dashboard can read while jobs write."""
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the shared application engine (created on first use)."""
    return create_db_engine(resolve_db_url())


def init_db(engine: Engine | None = None) -> None:
    """Create missing tables and columns. Safe to call repeatedly."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    logger.info("Database initialised at %s", engine.url.render_as_string(hide_password=True))


def _add_missing_columns(engine: Engine) -> None:
    """Add model columns (and their indexes) that an older database is missing.

    create_all() only creates whole tables; this covers purely additive schema changes.
    Anything more (renames, type changes) needs a real migration tool such as Alembic.
    """
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = CreateColumn(column).compile(dialect=engine.dialect)
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")
                logger.info("Added column %s.%s", table.name, column.name)
                for index in table.indexes:
                    if column in index.columns.values():
                        conn.execute(CreateIndex(index, if_not_exists=True))


def upsert_prices(df: pd.DataFrame, engine: Engine | None = None) -> int:
    """Insert or update price rows keyed on (symbol, date). Returns rows written.

    `df` needs `symbol` and `date` columns plus any of the OHLCV columns. `fetched_at`
    defaults to the current UTC time when absent.
    """
    df = df.copy()
    if "fetched_at" not in df.columns:
        df["fetched_at"] = dt.datetime.now(dt.UTC)
    return _upsert(PRICES, df, engine or get_engine())


def upsert_indicators(df: pd.DataFrame, engine: Engine | None = None) -> int:
    """Insert or update indicator rows keyed on (symbol, date). Returns rows written."""
    return _upsert(INDICATORS, df, engine or get_engine())


def read_prices(
    symbol: str,
    start: dt.date | str | None = None,
    end: dt.date | str | None = None,
    engine: Engine | None = None,
) -> pd.DataFrame:
    """Return prices for `symbol` between `start` and `end` (inclusive), sorted by date."""
    return _read(PRICES, symbol, start, end, engine or get_engine())


def read_indicators(
    symbol: str,
    start: dt.date | str | None = None,
    end: dt.date | str | None = None,
    engine: Engine | None = None,
) -> pd.DataFrame:
    """Return indicators for `symbol` between `start` and `end` (inclusive), sorted by date."""
    return _read(INDICATORS, symbol, start, end, engine or get_engine())


def latest_price_date(symbol: str, engine: Engine | None = None) -> dt.date | None:
    """Return the most recent stored price date for `symbol`, or None if it has none."""
    stmt = select(func.max(PRICES.c.date)).where(PRICES.c.symbol == symbol)
    with (engine or get_engine()).connect() as conn:
        return conn.execute(stmt).scalar_one()


def latest_indicator_date(symbol: str, engine: Engine | None = None) -> dt.date | None:
    """Return the most recent stored indicator date for `symbol`, or None if it has none."""
    stmt = select(func.max(INDICATORS.c.date)).where(INDICATORS.c.symbol == symbol)
    with (engine or get_engine()).connect() as conn:
        return conn.execute(stmt).scalar_one()


def sync_corporate_actions(actions: pd.DataFrame, engine: Engine | None = None) -> set[str]:
    """Make the corporate_actions table match `actions` exactly (the YAML is the source).

    Rows no longer present are deleted. Returns the symbols whose actions were added,
    removed or changed, so callers can fully recompute anything derived from them.
    """
    engine = engine or get_engine()
    new = actions.reindex(columns=ACTION_COLUMNS)
    new["ex_date"] = pd.to_datetime(new["ex_date"]).dt.date
    new["price_factor"] = new["price_factor"].astype(float)
    new = new.astype(object)
    new = new.where(new.notna(), None)
    new_rows = {tuple(r) for r in new.itertuples(index=False)}

    with engine.begin() as conn:
        old_rows = {tuple(r) for r in conn.execute(select(CORPORATE_ACTIONS))}
        if old_rows == new_rows:
            return set()
        conn.execute(delete(CORPORATE_ACTIONS))
        if new_rows:
            conn.execute(
                CORPORATE_ACTIONS.insert(),
                [dict(zip(ACTION_COLUMNS, r, strict=True)) for r in new_rows],
            )

    changed = {row[0] for row in old_rows ^ new_rows}
    logger.info("Synced %d corporate action(s); changed symbols: %s", len(new_rows), changed)
    return changed


def read_corporate_actions(symbol: str | None = None, engine: Engine | None = None) -> pd.DataFrame:
    """Return corporate actions (for one symbol, or all), sorted by symbol and ex_date."""
    stmt = select(CORPORATE_ACTIONS).order_by(
        CORPORATE_ACTIONS.c.symbol, CORPORATE_ACTIONS.c.ex_date
    )
    if symbol is not None:
        stmt = stmt.where(CORPORATE_ACTIONS.c.symbol == symbol)
    with (engine or get_engine()).connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    df = pd.DataFrame(rows, columns=ACTION_COLUMNS)
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


def insert_new_articles(articles: list[dict[str, Any]], engine: Engine | None = None) -> int:
    """Insert articles whose id isn't stored yet; existing rows are left untouched.

    Keeping the first stored row preserves first_seen_at, fetched_via and any extracted
    text. Duplicate ids within `articles` keep the first occurrence. Returns rows inserted.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for article in articles:
        by_id.setdefault(article["id"], article)
    unique = list(by_id.values())
    if not unique:
        return 0
    engine = engine or get_engine()
    inserted = 0
    with engine.begin() as conn:
        for chunk in _chunks(unique, UPSERT_CHUNK_SIZE):
            ids = [a["id"] for a in chunk]
            existing = set(
                conn.execute(select(ARTICLES.c.id).where(ARTICLES.c.id.in_(ids))).scalars()
            )
            new = [{"text_status": "pending", **a} for a in chunk if a["id"] not in existing]
            if new:
                conn.execute(ARTICLES.insert(), new)
                inserted += len(new)
    return inserted


def pending_text_articles(
    max_attempts: int, limit: int | None = None, engine: Engine | None = None
) -> list[dict[str, Any]]:
    """Articles still awaiting text extraction with attempts left, oldest first."""
    stmt = (
        select(ARTICLES.c.id, ARTICLES.c.url, ARTICLES.c.source, ARTICLES.c.text_attempts)
        .where(ARTICLES.c.text_status == "pending", ARTICLES.c.text_attempts < max_attempts)
        .order_by(ARTICLES.c.first_seen_at, ARTICLES.c.id)
        .limit(limit)
    )
    with (engine or get_engine()).connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def update_article_text(
    article_id: str,
    status: str,
    attempts: int,
    text: str | None = None,
    error: str | None = None,
    engine: Engine | None = None,
) -> None:
    """Record the outcome of a text-extraction attempt for one article."""
    if status not in TEXT_STATUSES:
        raise ValueError(f"invalid text_status {status!r}; expected one of {TEXT_STATUSES}")
    stmt = (
        update(ARTICLES)
        .where(ARTICLES.c.id == article_id)
        .values(
            text_status=status, text_attempts=attempts, text=text, text_error=error, linked_at=None
        )
    )
    with (engine or get_engine()).begin() as conn:
        conn.execute(stmt)


def read_extracted_texts(engine: Engine | None = None) -> list[dict[str, Any]]:
    """id, url, text and text_attempts of every article with text_status = 'ok'."""
    stmt = select(ARTICLES.c.id, ARTICLES.c.url, ARTICLES.c.text, ARTICLES.c.text_attempts).where(
        ARTICLES.c.text_status == "ok"
    )
    with (engine or get_engine()).connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def set_article_texts(texts: dict[str, str], engine: Engine | None = None) -> None:
    """Overwrite articles.text for each article id -> text pair."""
    if not texts:
        return
    rows = [{"aid": aid, "txt": text} for aid, text in texts.items()]
    stmt = (
        update(ARTICLES)
        .where(ARTICLES.c.id == bindparam("aid"))
        .values(text=bindparam("txt"), linked_at=None)
    )
    with (engine or get_engine()).begin() as conn:
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(stmt, chunk)


def read_articles_for_grouping(
    since: dt.datetime | None = None, engine: Engine | None = None
) -> pd.DataFrame:
    """id, title, source, published_at, first_seen_at, story_id; optionally only recent ones.

    `since` filters on COALESCE(published_at, first_seen_at).
    """
    event_time = func.coalesce(ARTICLES.c.published_at, ARTICLES.c.first_seen_at)
    columns = ["id", "title", "source", "published_at", "first_seen_at", "story_id"]
    stmt = select(*(ARTICLES.c[c] for c in columns))
    if since is not None:
        stmt = stmt.where(event_time >= since)
    with (engine or get_engine()).connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return pd.DataFrame(rows, columns=columns)


def earliest_ungrouped_time(engine: Engine | None = None) -> dt.datetime | None:
    """COALESCE(published_at, first_seen_at) of the oldest article without a story_id."""
    stmt = select(func.min(func.coalesce(ARTICLES.c.published_at, ARTICLES.c.first_seen_at))).where(
        ARTICLES.c.story_id.is_(None)
    )
    with (engine or get_engine()).connect() as conn:
        value = conn.execute(stmt).scalar_one()
    if value is None:
        return None
    value = pd.Timestamp(value).to_pydatetime()  # func.min loses the UTC type on SQLite
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def set_story_ids(story_ids: dict[str, str], engine: Engine | None = None) -> None:
    """Set articles.story_id for each article id -> story id pair."""
    if not story_ids:
        return
    with (engine or get_engine()).begin() as conn:
        for chunk in _chunks(
            [{"aid": a, "sid": s} for a, s in story_ids.items()], UPSERT_CHUNK_SIZE
        ):
            conn.execute(
                update(ARTICLES)
                .where(ARTICLES.c.id == bindparam("aid"))
                .values(story_id=bindparam("sid")),
                chunk,
            )


def articles_to_link(full: bool = False, engine: Engine | None = None) -> pd.DataFrame:
    """Articles needing entity linking (never linked, or text changed since); all if full."""
    columns = ["id", "title", "summary", "text", "published_at", "first_seen_at"]
    stmt = select(*(ARTICLES.c[c] for c in columns))
    if not full:
        stmt = stmt.where(ARTICLES.c.linked_at.is_(None))
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def replace_mentions(
    article_ids: list[str], mentions: list[dict[str, Any]], engine: Engine | None = None
) -> None:
    """Replace all mentions (and drop sentiment) for `article_ids`; mark them linked."""
    now = dt.datetime.now(dt.UTC)
    with (engine or get_engine()).begin() as conn:
        for chunk in _chunks([{"aid": a} for a in article_ids], UPSERT_CHUNK_SIZE):
            ids = [c["aid"] for c in chunk]
            conn.execute(delete(MENTIONS).where(MENTIONS.c.article_id.in_(ids)))
            # Sentiment is scored per mention, so it's stale once mentions are rebuilt.
            conn.execute(delete(SENTIMENT).where(SENTIMENT.c.article_id.in_(ids)))
            conn.execute(update(ARTICLES).where(ARTICLES.c.id.in_(ids)).values(linked_at=now))
        for chunk in _chunks(mentions, UPSERT_CHUNK_SIZE):
            conn.execute(MENTIONS.insert(), chunk)


def read_mentions(engine: Engine | None = None) -> pd.DataFrame:
    """All mentions joined with the article's title, source and story_id."""
    stmt = select(
        MENTIONS,
        ARTICLES.c.title,
        ARTICLES.c.source,
        ARTICLES.c.story_id,
        ARTICLES.c.published_at,
    ).join(ARTICLES, ARTICLES.c.id == MENTIONS.c.article_id)
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all())


def mentions_to_score(
    model_name: str, min_confidence: float, engine: Engine | None = None
) -> pd.DataFrame:
    """Linked mentions (confidence >= min_confidence) not yet scored by `model_name`."""
    already_scored = select(SENTIMENT.c.article_id).where(
        SENTIMENT.c.model_name == model_name,
        SENTIMENT.c.article_id == MENTIONS.c.article_id,
        SENTIMENT.c.symbol == MENTIONS.c.symbol,
    )
    stmt = (
        select(
            MENTIONS.c.article_id,
            MENTIONS.c.symbol,
            ARTICLES.c.title,
            ARTICLES.c.summary,
            ARTICLES.c.text,
            ARTICLES.c.published_at,
            ARTICLES.c.first_seen_at,
        )
        .join(ARTICLES, ARTICLES.c.id == MENTIONS.c.article_id)
        .where(MENTIONS.c.confidence >= min_confidence)
        .where(~exists(already_scored))
        .order_by(MENTIONS.c.article_id, MENTIONS.c.symbol)
    )
    with (engine or get_engine()).connect() as conn:
        df = pd.DataFrame(conn.execute(stmt).mappings().all())
    columns = ["article_id", "symbol", "title", "summary", "text", "published_at", "first_seen_at"]
    return df.reindex(columns=columns)


def insert_sentiment(rows: list[dict[str, Any]], engine: Engine | None = None) -> None:
    """Insert article_sentiment rows."""
    if not rows:
        return
    with (engine or get_engine()).begin() as conn:
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(SENTIMENT.insert(), chunk)


def read_scored_mentions(model_name: str, engine: Engine | None = None) -> pd.DataFrame:
    """Scored mentions for aggregation: sentiment + mention confidence + article fields."""
    stmt = (
        select(
            SENTIMENT.c.article_id,
            SENTIMENT.c.symbol,
            SENTIMENT.c.label,
            SENTIMENT.c.score,
            MENTIONS.c.confidence,
            ARTICLES.c.title,
            ARTICLES.c.source,
            ARTICLES.c.story_id,
            ARTICLES.c.published_at,
            ARTICLES.c.first_seen_at,
        )
        .join(
            MENTIONS,
            (MENTIONS.c.article_id == SENTIMENT.c.article_id)
            & (MENTIONS.c.symbol == SENTIMENT.c.symbol),
        )
        .join(ARTICLES, ARTICLES.c.id == SENTIMENT.c.article_id)
        .where(SENTIMENT.c.model_name == model_name)
    )
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all())


def trading_dates(engine: Engine | None = None) -> list[dt.date]:
    """Every date with at least one stored price bar, i.e. known NSE trading sessions."""
    stmt = select(PRICES.c.date).distinct().order_by(PRICES.c.date)
    with (engine or get_engine()).connect() as conn:
        return list(conn.execute(stmt).scalars())


def replace_news_daily(
    model_name: str, rows: list[dict[str, Any]], engine: Engine | None = None
) -> None:
    """Replace all news_daily rows for `model_name` with `rows`."""
    with (engine or get_engine()).begin() as conn:
        conn.execute(delete(NEWS_DAILY).where(NEWS_DAILY.c.model_name == model_name))
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(NEWS_DAILY.insert(), chunk)


def read_news_daily(model_name: str, engine: Engine | None = None) -> pd.DataFrame:
    """news_daily rows for `model_name`, sorted by symbol and session."""
    stmt = (
        select(NEWS_DAILY)
        .where(NEWS_DAILY.c.model_name == model_name)
        .order_by(NEWS_DAILY.c.symbol, NEWS_DAILY.c.session_date)
    )
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all())


def read_stock_news(
    symbol: str, model_name: str, min_confidence: float, engine: Engine | None = None
) -> pd.DataFrame:
    """Articles linked to `symbol` (confidence >= min_confidence) with their sentiment.

    Sentiment columns are NaN for articles not yet scored by `model_name`.
    """
    sentiment = (
        select(SENTIMENT)
        .where(SENTIMENT.c.model_name == model_name, SENTIMENT.c.symbol == symbol)
        .subquery()
    )
    stmt = (
        select(
            ARTICLES.c.id.label("article_id"),
            ARTICLES.c.title,
            ARTICLES.c.url,
            ARTICLES.c.source,
            ARTICLES.c.story_id,
            ARTICLES.c.published_at,
            ARTICLES.c.first_seen_at,
            MENTIONS.c.confidence,
            sentiment.c.label,
            sentiment.c.score,
        )
        .join(MENTIONS, MENTIONS.c.article_id == ARTICLES.c.id)
        .outerjoin(sentiment, sentiment.c.article_id == ARTICLES.c.id)
        .where(MENTIONS.c.symbol == symbol, MENTIONS.c.confidence >= min_confidence)
    )
    columns = [
        "article_id", "title", "url", "source", "story_id", "published_at",
        "first_seen_at", "confidence", "label", "score",
    ]  # fmt: skip
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def read_story_sources(story_ids: list[str], engine: Engine | None = None) -> pd.DataFrame:
    """(story_id, source) for every article in the given stories, linked or not."""
    frames = []
    with (engine or get_engine()).connect() as conn:
        for chunk in _chunks([{"sid": s} for s in story_ids], UPSERT_CHUNK_SIZE):
            stmt = select(ARTICLES.c.story_id, ARTICLES.c.source).where(
                ARTICLES.c.story_id.in_([c["sid"] for c in chunk])
            )
            frames.append(pd.DataFrame(conn.execute(stmt).mappings().all()))
    if not frames:
        return pd.DataFrame(columns=["story_id", "source"])
    return pd.concat(frames, ignore_index=True).reindex(columns=["story_id", "source"])


def read_filings(engine: Engine | None = None) -> pd.DataFrame:
    """All filings with the fields needed for categorisation."""
    columns = ["id", "exchange", "symbol", "filed_at", "category", "subject", "description"]
    stmt = select(*(FILINGS.c[c] for c in columns)).order_by(FILINGS.c.filed_at)
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def read_stock_filings(symbol: str, engine: Engine | None = None) -> pd.DataFrame:
    """One stock's filings for display, newest first."""
    columns = [
        "id", "exchange", "filed_at", "category", "subject", "filing_type",
        "attachment_url", "attachment_path",
    ]  # fmt: skip
    stmt = (
        select(*(FILINGS.c[c] for c in columns))
        .where(FILINGS.c.symbol == symbol)
        .order_by(FILINGS.c.filed_at.desc())
    )
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def set_filing_types(types: dict[str, tuple[str, str]], engine: Engine | None = None) -> None:
    """Set (filing_type, filing_tags) per filing id."""
    rows = [{"fid": fid, "ft": ft, "tags": tags} for fid, (ft, tags) in types.items()]
    stmt = (
        update(FILINGS)
        .where(FILINGS.c.id == bindparam("fid"))
        .values(filing_type=bindparam("ft"), filing_tags=bindparam("tags"))
    )
    with (engine or get_engine()).begin() as conn:
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(stmt, chunk)


def replace_pending_actions(rows: list[dict[str, Any]], engine: Engine | None = None) -> None:
    """Replace the whole pending_actions table (it is derived data)."""
    with (engine or get_engine()).begin() as conn:
        conn.execute(delete(PENDING_ACTIONS))
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(PENDING_ACTIONS.insert(), chunk)


def read_pending_actions(engine: Engine | None = None) -> pd.DataFrame:
    """All pending_actions rows, newest filing first."""
    stmt = select(PENDING_ACTIONS).order_by(PENDING_ACTIONS.c.filed_at.desc())
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all())


def insert_filing(row: dict[str, Any], engine: Engine | None = None) -> bool:
    """Insert a filings row unless its id exists. Returns True if inserted."""
    with (engine or get_engine()).begin() as conn:
        exists = conn.execute(select(FILINGS.c.id).where(FILINGS.c.id == row["id"])).first()
        if exists:
            return False
        conn.execute(FILINGS.insert(), [row])
        return True


def filing_by_sha256(sha256: str, engine: Engine | None = None) -> dict[str, Any] | None:
    """The filings row whose attachment has this hash, if any."""
    stmt = select(FILINGS).where(FILINGS.c.attachment_sha256 == sha256)
    with (engine or get_engine()).connect() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def update_filing_meta(
    meta: dict[str, tuple[dt.datetime, str]], engine: Engine | None = None
) -> None:
    """Set filed_at and subject per filing id."""
    rows = [{"fid": fid, "at": at, "subj": subj} for fid, (at, subj) in meta.items()]
    stmt = (
        update(FILINGS)
        .where(FILINGS.c.id == bindparam("fid"))
        .values(filed_at=bindparam("at"), subject=bindparam("subj"))
    )
    with (engine or get_engine()).begin() as conn:
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(stmt, chunk)


def filing_urls(symbol: str, engine: Engine | None = None) -> set[str]:
    """Attachment URLs already stored for `symbol`."""
    stmt = select(FILINGS.c.attachment_url).where(
        FILINGS.c.symbol == symbol, FILINGS.c.attachment_url.is_not(None)
    )
    with (engine or get_engine()).connect() as conn:
        return set(conn.execute(stmt).scalars())


def read_result_files(engine: Engine | None = None) -> pd.DataFrame:
    """Filings that carry a stored results file (XBRL or PDF) on disk."""
    columns = ["id", "exchange", "symbol", "filed_at", "subject", "attachment_path"]
    stmt = (
        select(*(FILINGS.c[c] for c in columns))
        .where(FILINGS.c.filing_type == "results", FILINGS.c.attachment_path.is_not(None))
        .order_by(FILINGS.c.filed_at)
    )
    with (engine or get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def replace_results(rows: list[dict[str, Any]], engine: Engine | None = None) -> None:
    """Replace the whole results table (it is rebuilt from stored files)."""
    with (engine or get_engine()).begin() as conn:
        conn.execute(delete(RESULTS))
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(RESULTS.insert(), chunk)


def read_results(engine: Engine | None = None) -> pd.DataFrame:
    """All results rows."""
    stmt = select(RESULTS).order_by(RESULTS.c.symbol, RESULTS.c.period_end)
    with (engine or get_engine()).connect() as conn:
        df = pd.DataFrame(conn.execute(stmt).mappings().all())
    return df if not df.empty else pd.DataFrame(columns=list(RESULTS.columns.keys()))


def count_articles(group_by: str = "fetched_via", engine: Engine | None = None) -> dict[str, int]:
    """Return stored article counts grouped by `fetched_via` or `source`."""
    column = ARTICLES.c[group_by]
    stmt = select(column, func.count()).group_by(column)
    with (engine or get_engine()).connect() as conn:
        return {key or "(unknown)": n for key, n in conn.execute(stmt)}


def text_status_counts(engine: Engine | None = None) -> pd.DataFrame:
    """Article counts by source and text_status (rows: source, columns: status)."""
    stmt = select(ARTICLES.c.source, ARTICLES.c.text_status, func.count().label("n")).group_by(
        ARTICLES.c.source, ARTICLES.c.text_status
    )
    with (engine or get_engine()).connect() as conn:
        df = pd.DataFrame(
            conn.execute(stmt).mappings().all(), columns=["source", "text_status", "n"]
        )
    return df.pivot_table(
        index="source", columns="text_status", values="n", fill_value=0, aggfunc="sum"
    )


def count_prices(engine: Engine | None = None) -> dict[str, int]:
    """Return the number of stored price rows per symbol."""
    stmt = select(PRICES.c.symbol, func.count()).group_by(PRICES.c.symbol)
    with (engine or get_engine()).connect() as conn:
        return {symbol: n for symbol, n in conn.execute(stmt)}


def _upsert(table: Table, df: pd.DataFrame, engine: Engine) -> int:
    """Upsert a DataFrame into `table` in chunks, updating non-key columns on conflict."""
    if df.empty:
        return 0
    records = _to_records(table, df)
    insert = _dialect_insert(engine)
    update_cols = [c for c in records[0] if c not in KEY_COLUMNS]

    with engine.begin() as conn:
        for chunk in _chunks(records, UPSERT_CHUNK_SIZE):
            stmt = insert(table).values(chunk)
            if update_cols:
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(KEY_COLUMNS),
                    set_={c: stmt.excluded[c] for c in update_cols},
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(KEY_COLUMNS))
            conn.execute(stmt)

    logger.debug("Upserted %d rows into %s", len(records), table.name)
    return len(records)


def _to_records(table: Table, df: pd.DataFrame) -> list[dict[str, Any]]:
    """Validate columns and convert a DataFrame to DB-ready dicts (NaN -> None)."""
    missing = [c for c in KEY_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{table.name}: DataFrame missing key column(s): {', '.join(missing)}")
    unknown = sorted(set(df.columns) - set(table.columns.keys()))
    if unknown:
        raise ValueError(f"{table.name}: unknown column(s): {', '.join(unknown)}")

    df = df.copy()
    # .dt.date on tz-aware timestamps keeps the local (exchange) calendar date.
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if df.duplicated(subset=list(KEY_COLUMNS)).any():
        raise ValueError(f"{table.name}: DataFrame contains duplicate (symbol, date) rows")
    if "fetched_at" in df.columns:
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], utc=True)

    records = df.astype(object).where(df.notna(), None).to_dict(orient="records")
    for record in records:
        if isinstance(record.get("fetched_at"), pd.Timestamp):
            record["fetched_at"] = record["fetched_at"].to_pydatetime()
        if record.get("volume") is not None:
            record["volume"] = int(record["volume"])
    return records


def _read(
    table: Table,
    symbol: str,
    start: dt.date | str | None,
    end: dt.date | str | None,
    engine: Engine,
) -> pd.DataFrame:
    """Select rows for one symbol and optional inclusive date range into a DataFrame."""
    stmt = select(table).where(table.c.symbol == symbol)
    if start is not None:
        stmt = stmt.where(table.c.date >= pd.Timestamp(start).date())
    if end is not None:
        stmt = stmt.where(table.c.date <= pd.Timestamp(end).date())
    stmt = stmt.order_by(table.c.date)

    with engine.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    df = pd.DataFrame(rows, columns=list(table.columns.keys()))
    df["date"] = pd.to_datetime(df["date"])
    return df


def _dialect_insert(engine: Engine) -> Any:
    """Return the dialect-specific insert() that supports ON CONFLICT."""
    dialects = {"sqlite": sqlite.insert, "postgresql": postgresql.insert}
    try:
        return dialects[engine.dialect.name]
    except KeyError as exc:
        raise NotImplementedError(f"Upsert not supported for {engine.dialect.name}") from exc


def _chunks(items: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Yield successive slices of `items` of length `size`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]
