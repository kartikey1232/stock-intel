"""Reads and writes for the social tables (social_topics, social_posts, social_mentions,
social_sentiment, social_daily). The schema lives in storage/db.py with the others."""

import datetime as dt
from typing import Any

import pandas as pd
from sqlalchemy import Engine, delete, exists, select, update

from storage import db
from storage.db import (
    SOCIAL_DAILY,
    SOCIAL_MENTIONS,
    SOCIAL_POSTS,
    SOCIAL_SENTIMENT,
    SOCIAL_TOPICS,
    UPSERT_CHUNK_SIZE,
    _chunks,
    _dialect_insert,
)


def upsert_topic(row: dict[str, Any], engine: Engine | None = None) -> None:
    """Insert or update a social_topics row; columns missing from `row` are kept."""
    engine = engine or db.get_engine()
    stmt = _dialect_insert(engine)(SOCIAL_TOPICS).values(row)
    stmt = stmt.on_conflict_do_update(
        index_elements=["platform", "topic_id"],
        set_={c: stmt.excluded[c] for c in row if c not in ("platform", "topic_id")},
    )
    with engine.begin() as conn:
        conn.execute(stmt)


def read_topic(platform: str, topic_id: str, engine: Engine | None = None) -> dict | None:
    """The social_topics row for one topic, or None if we've never fetched it."""
    stmt = select(SOCIAL_TOPICS).where(
        SOCIAL_TOPICS.c.platform == platform, SOCIAL_TOPICS.c.topic_id == topic_id
    )
    with (engine or db.get_engine()).connect() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def stored_posts(platform: str, topic_id: str, engine: Engine | None = None) -> dict[str, int]:
    """{platform_post_id: post_number} for the posts stored for one topic."""
    stmt = select(SOCIAL_POSTS.c.platform_post_id, SOCIAL_POSTS.c.post_number).where(
        SOCIAL_POSTS.c.platform == platform, SOCIAL_POSTS.c.topic_id == topic_id
    )
    with (engine or db.get_engine()).connect() as conn:
        return {pid: number or 0 for pid, number in conn.execute(stmt)}


def insert_new_posts(rows: list[dict[str, Any]], engine: Engine | None = None) -> int:
    """Insert posts whose id isn't stored yet (first write wins). Returns rows inserted."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_id.setdefault(row["id"], row)
    inserted = 0
    with (engine or db.get_engine()).begin() as conn:
        for chunk in _chunks(list(by_id.values()), UPSERT_CHUNK_SIZE):
            ids = [r["id"] for r in chunk]
            existing = set(
                conn.execute(select(SOCIAL_POSTS.c.id).where(SOCIAL_POSTS.c.id.in_(ids))).scalars()
            )
            new = [r for r in chunk if r["id"] not in existing]
            if new:
                conn.execute(SOCIAL_POSTS.insert(), new)
                inserted += len(new)
    return inserted


def delete_posts(post_ids: list[str], engine: Engine | None = None) -> int:
    """Delete posts and everything derived from them (mentions, sentiment)."""
    with (engine or db.get_engine()).begin() as conn:
        for chunk in _chunks([{"id": p} for p in post_ids], UPSERT_CHUNK_SIZE):
            ids = [c["id"] for c in chunk]
            conn.execute(delete(SOCIAL_SENTIMENT).where(SOCIAL_SENTIMENT.c.post_id.in_(ids)))
            conn.execute(delete(SOCIAL_MENTIONS).where(SOCIAL_MENTIONS.c.post_id.in_(ids)))
            conn.execute(delete(SOCIAL_POSTS).where(SOCIAL_POSTS.c.id.in_(ids)))
    return len(post_ids)


def topic_post_ids(platform: str, topic_id: str, engine: Engine | None = None) -> list[str]:
    """Stored post ids (our ids, "<platform>:<id>") for one topic."""
    stmt = select(SOCIAL_POSTS.c.id).where(
        SOCIAL_POSTS.c.platform == platform, SOCIAL_POSTS.c.topic_id == topic_id
    )
    with (engine or db.get_engine()).connect() as conn:
        return list(conn.execute(stmt).scalars())


def posts_to_recheck(platform: str, limit: int, engine: Engine | None = None) -> pd.DataFrame:
    """The `limit` posts checked least recently: id, topic_id, platform_post_id, text."""
    columns = ["id", "topic_id", "platform_post_id", "text"]
    stmt = (
        select(*(SOCIAL_POSTS.c[c] for c in columns))
        .where(SOCIAL_POSTS.c.platform == platform)
        .order_by(SOCIAL_POSTS.c.checked_at, SOCIAL_POSTS.c.id)
        .limit(limit)
    )
    with (engine or db.get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def mark_checked(
    post_ids: list[str],
    now: dt.datetime,
    texts: dict[str, str | None] | None = None,
    engine: Engine | None = None,
) -> None:
    """Set checked_at for `post_ids`; posts in `texts` get new text and are re-linked."""
    with (engine or db.get_engine()).begin() as conn:
        for chunk in _chunks([{"id": p} for p in post_ids], UPSERT_CHUNK_SIZE):
            ids = [c["id"] for c in chunk]
            conn.execute(
                update(SOCIAL_POSTS).where(SOCIAL_POSTS.c.id.in_(ids)).values(checked_at=now)
            )
        for post_id, text in (texts or {}).items():
            conn.execute(
                update(SOCIAL_POSTS)
                .where(SOCIAL_POSTS.c.id == post_id)
                .values(text=text, linked_at=None)
            )


def posts_to_link(full: bool = False, engine: Engine | None = None) -> pd.DataFrame:
    """Posts needing linking (never linked, or text changed since); all if `full`."""
    columns = ["id", "platform", "topic_id", "text", "created_at", "first_seen_at"]
    stmt = select(*(SOCIAL_POSTS.c[c] for c in columns))
    if not full:
        stmt = stmt.where(SOCIAL_POSTS.c.linked_at.is_(None))
    with (engine or db.get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def replace_social_mentions(
    post_ids: list[str], mentions: list[dict[str, Any]], engine: Engine | None = None
) -> None:
    """Replace all mentions (and drop sentiment) for `post_ids`; mark them linked."""
    now = dt.datetime.now(dt.UTC)
    with (engine or db.get_engine()).begin() as conn:
        for chunk in _chunks([{"id": p} for p in post_ids], UPSERT_CHUNK_SIZE):
            ids = [c["id"] for c in chunk]
            conn.execute(delete(SOCIAL_MENTIONS).where(SOCIAL_MENTIONS.c.post_id.in_(ids)))
            conn.execute(delete(SOCIAL_SENTIMENT).where(SOCIAL_SENTIMENT.c.post_id.in_(ids)))
            conn.execute(
                update(SOCIAL_POSTS).where(SOCIAL_POSTS.c.id.in_(ids)).values(linked_at=now)
            )
        for chunk in _chunks(mentions, UPSERT_CHUNK_SIZE):
            conn.execute(SOCIAL_MENTIONS.insert(), chunk)


def social_mentions_to_score(
    model_name: str, min_confidence: float, engine: Engine | None = None
) -> pd.DataFrame:
    """Linked opinion mentions (confidence >= min_confidence) not yet scored by `model_name`.

    Shares and short replies (post_kind != opinion) are never scored.
    """
    scored = select(SOCIAL_SENTIMENT.c.post_id).where(
        SOCIAL_SENTIMENT.c.model_name == model_name,
        SOCIAL_SENTIMENT.c.post_id == SOCIAL_MENTIONS.c.post_id,
        SOCIAL_SENTIMENT.c.symbol == SOCIAL_MENTIONS.c.symbol,
    )
    columns = ["post_id", "symbol", "method", "text", "created_at"]
    stmt = (
        select(
            SOCIAL_MENTIONS.c.post_id,
            SOCIAL_MENTIONS.c.symbol,
            SOCIAL_MENTIONS.c.method,
            SOCIAL_POSTS.c.text,
            SOCIAL_POSTS.c.created_at,
        )
        .join(SOCIAL_POSTS, SOCIAL_POSTS.c.id == SOCIAL_MENTIONS.c.post_id)
        .where(SOCIAL_MENTIONS.c.confidence >= min_confidence)
        .where(SOCIAL_MENTIONS.c.post_kind == "opinion")
        .where(~exists(scored))
        .order_by(SOCIAL_MENTIONS.c.post_id, SOCIAL_MENTIONS.c.symbol)
    )
    with (engine or db.get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def insert_social_sentiment(rows: list[dict[str, Any]], engine: Engine | None = None) -> None:
    """Insert social_sentiment rows."""
    with (engine or db.get_engine()).begin() as conn:
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(SOCIAL_SENTIMENT.insert(), chunk)


def read_linked_posts(
    model_name: str,
    min_confidence: float,
    symbol: str | None = None,
    engine: Engine | None = None,
) -> pd.DataFrame:
    """Linked (post, stock) pairs with post metadata, topic title and sentiment.

    `score` is NaN for pairs not yet scored by `model_name`. No post text is returned.
    """
    sentiment = (
        select(SOCIAL_SENTIMENT).where(SOCIAL_SENTIMENT.c.model_name == model_name).subquery()
    )
    stmt = (
        select(
            SOCIAL_MENTIONS.c.post_id,
            SOCIAL_MENTIONS.c.symbol,
            SOCIAL_MENTIONS.c.method,
            SOCIAL_MENTIONS.c.confidence,
            SOCIAL_MENTIONS.c.post_kind,
            SOCIAL_POSTS.c.platform,
            SOCIAL_POSTS.c.topic_id,
            SOCIAL_POSTS.c.post_number,
            SOCIAL_POSTS.c.url,
            SOCIAL_POSTS.c.author_hmac,
            SOCIAL_POSTS.c.likes,
            SOCIAL_POSTS.c.created_at,
            SOCIAL_POSTS.c.first_seen_at,
            SOCIAL_TOPICS.c.title.label("topic_title"),
            sentiment.c.score,
        )
        .join(SOCIAL_POSTS, SOCIAL_POSTS.c.id == SOCIAL_MENTIONS.c.post_id)
        .outerjoin(
            SOCIAL_TOPICS,
            (SOCIAL_TOPICS.c.platform == SOCIAL_POSTS.c.platform)
            & (SOCIAL_TOPICS.c.topic_id == SOCIAL_POSTS.c.topic_id),
        )
        .outerjoin(
            sentiment,
            (sentiment.c.post_id == SOCIAL_MENTIONS.c.post_id)
            & (sentiment.c.symbol == SOCIAL_MENTIONS.c.symbol),
        )
        .where(SOCIAL_MENTIONS.c.confidence >= min_confidence)
    )
    if symbol is not None:
        stmt = stmt.where(SOCIAL_MENTIONS.c.symbol == symbol)
    columns = [
        "post_id", "symbol", "method", "confidence", "post_kind", "platform", "topic_id",
        "post_number",
        "url", "author_hmac", "likes", "created_at", "first_seen_at", "topic_title", "score",
    ]  # fmt: skip
    with (engine or db.get_engine()).connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all(), columns=columns)


def replace_social_daily(
    model_name: str, rows: list[dict[str, Any]], engine: Engine | None = None
) -> None:
    """Replace all social_daily rows for `model_name` with `rows`."""
    with (engine or db.get_engine()).begin() as conn:
        conn.execute(delete(SOCIAL_DAILY).where(SOCIAL_DAILY.c.model_name == model_name))
        for chunk in _chunks(rows, UPSERT_CHUNK_SIZE):
            conn.execute(SOCIAL_DAILY.insert(), chunk)


def read_social_daily(
    model_name: str, symbol: str | None = None, engine: Engine | None = None
) -> pd.DataFrame:
    """social_daily rows for `model_name` (optionally one symbol), by symbol and session."""
    stmt = select(SOCIAL_DAILY).where(SOCIAL_DAILY.c.model_name == model_name)
    if symbol is not None:
        stmt = stmt.where(SOCIAL_DAILY.c.symbol == symbol)
    stmt = stmt.order_by(SOCIAL_DAILY.c.symbol, SOCIAL_DAILY.c.session_date)
    with (engine or db.get_engine()).connect() as conn:
        return pd.DataFrame(
            conn.execute(stmt).mappings().all(), columns=list(SOCIAL_DAILY.columns.keys())
        )
