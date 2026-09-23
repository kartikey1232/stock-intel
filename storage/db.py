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
    String,
    Table,
    Text,
    TypeDecorator,
    create_engine,
    delete,
    event,
    func,
    select,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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


PRICES: Table = Price.__table__  # type: ignore[assignment]
INDICATORS: Table = Indicator.__table__  # type: ignore[assignment]
CORPORATE_ACTIONS: Table = CorporateActionRow.__table__  # type: ignore[assignment]
ACTION_COLUMNS = list(CORPORATE_ACTIONS.columns.keys())
KEY_COLUMNS = ("symbol", "date")


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
    """Create all tables if they don't exist. Safe to call repeatedly."""
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    logger.info("Database initialised at %s", engine.url.render_as_string(hide_password=True))


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
