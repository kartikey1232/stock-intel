"""Group syndicated copies of the same story under a shared story_id.

Two articles are the same story when their event times (published_at, else first_seen_at)
are within `window_hours` and their normalised titles score at least `threshold` with
rapidfuzz's token-set ratio. Token-set ratio scores 100 when one title's words are a
subset of the other's, which is right for "X shares jump" vs "X shares jump after Q2 beat"
but too loose for short titles ("TCS Outlook for the Week" vs "INFY Outlook for the Week"
scores 91), so titles with fewer than `min_tokens` words use the stricter token-sort
ratio. Titles whose numbers, weekdays or named watchlist companies conflict never match,
which keeps templated headlines ("<Company> Share Price Prediction for Tomorrow: 18 Sep")
apart. Matches are transitive (union-find).

Duplicates are never deleted. story_id is the id of the group's earliest article, or the
story_id a group member already had, so ids stay stable across incremental runs.

Run with:  uv run python -m processing.stories [--full]
"""

import argparse
import datetime as dt
import logging
import re
import sys
import unicodedata

import pandas as pd
from rapidfuzz import fuzz

from config.loader import Stock, load_watchlist
from config.news_sources import NewsConfig, load_news_sources
from storage.db import (
    earliest_ungrouped_time,
    init_db,
    read_articles_for_grouping,
    set_story_ids,
)
from utils import setup_logging

logger = logging.getLogger(__name__)

NON_WORD_RE = re.compile(r"[^\w%]+")
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def normalise_title(title: str) -> str:
    """Lowercase, ASCII-fold, and reduce to space-separated words (keeping '%')."""
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return NON_WORD_RE.sub(" ", folded.lower()).strip()


def company_patterns(stocks: list[Stock]) -> dict[str, re.Pattern[str]]:
    """Whole-word regex per watchlist symbol, built from its name and aliases."""
    patterns = {}
    for stock in stocks:
        names = {normalise_title(n) for n in (*stock.search_terms, *stock.aliases)}
        alternation = "|".join(sorted((re.escape(n) for n in names if n), key=len, reverse=True))
        patterns[stock.symbol] = re.compile(rf"\b(?:{alternation})\b")
    return patterns


def distinguishing_tokens(
    title: str, companies: dict[str, re.Pattern[str]] | None = None
) -> frozenset[str]:
    """What tells otherwise-identical templated headlines apart: numbers, weekdays, and
    which watchlist companies are named (as '@SYMBOL')."""
    tokens = {t for t in title.split() if t in WEEKDAYS or any(c.isdigit() for c in t)}
    tokens |= {f"@{symbol}" for symbol, rx in (companies or {}).items() if rx.search(title)}
    return frozenset(tokens)


def titles_match(
    a: str,
    b: str,
    threshold: float,
    min_tokens: int,
    keys: tuple[frozenset[str], frozenset[str]] | None = None,
) -> bool:
    """True if two normalised titles are near-identical.

    Vetoed when their distinguishing tokens conflict (neither set contains the other), so
    "X falls Friday" != "X falls Wednesday", "Infosys prediction for 21 Sep" != "HDFC Bank
    prediction for 21 Sep", while "12000 crore NCDs" still matches "12000 crore NCDs at
    7 47%". `keys` are precomputed distinguishing_tokens(a), distinguishing_tokens(b).
    """
    da, db = keys or (distinguishing_tokens(a), distinguishing_tokens(b))
    if not (da <= db or db <= da):
        return False
    shortest = min(len(a.split()), len(b.split()))
    scorer = fuzz.token_set_ratio if shortest >= min_tokens else fuzz.token_sort_ratio
    return scorer(a, b, score_cutoff=threshold) >= threshold


def group_stories(
    articles: pd.DataFrame,
    threshold: float,
    window_hours: float,
    min_tokens: int,
    companies: dict[str, re.Pattern[str]] | None = None,
) -> dict[str, str]:
    """Return article id -> story id for every article in `articles`.

    `articles` needs id, title, published_at, first_seen_at and (optionally) story_id.
    `companies` (from company_patterns) stops headlines about different watchlist
    companies from being grouped.
    """
    if articles.empty:
        return {}
    df = articles.copy()
    df["event_time"] = pd.to_datetime(df["published_at"].fillna(df["first_seen_at"]), utc=True)
    df["norm"] = df["title"].map(normalise_title)
    df = df.sort_values(["event_time", "id"]).reset_index(drop=True)

    parent = list(range(len(df)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    window = pd.Timedelta(hours=window_hours)
    times, norms = df["event_time"].tolist(), df["norm"].tolist()
    keys = [distinguishing_tokens(n, companies) for n in norms]
    for i in range(len(df)):
        for j in range(i + 1, len(df)):
            if times[j] - times[i] > window:
                break
            if find(i) != find(j) and titles_match(
                norms[i], norms[j], threshold, min_tokens, keys=(keys[i], keys[j])
            ):
                parent[find(j)] = find(i)

    df["root"] = [find(i) for i in range(len(df))]
    existing = df.get("story_id", pd.Series(index=df.index, dtype=object))
    story_ids: dict[int, str] = {}
    for root, members in df.groupby("root", sort=False):  # members are in time order
        prior = existing.loc[members.index].dropna()
        story_ids[root] = prior.iloc[0] if not prior.empty else members["id"].iloc[0]
    return dict(zip(df["id"], df["root"].map(story_ids), strict=True))


def run(config: NewsConfig, stocks: list[Stock], full: bool = False) -> dict[str, str]:
    """Assign story ids. Incremental runs regroup only the recent window. Returns changes."""
    since = None
    if not full:
        earliest = earliest_ungrouped_time()
        if earliest is None:
            logger.info("All articles already have a story_id")
            return {}
        # Two windows back so new articles can join groups that started before them.
        since = earliest - dt.timedelta(hours=2 * config.story_window_hours)

    articles = read_articles_for_grouping(since)
    assigned = group_stories(
        articles,
        config.story_similarity_threshold,
        config.story_window_hours,
        config.story_min_tokens,
        company_patterns(stocks),
    )
    current = dict(zip(articles["id"], articles["story_id"], strict=True))
    changed = {aid: sid for aid, sid in assigned.items() if current.get(aid) != sid}
    set_story_ids(changed)

    sizes = pd.Series(assigned).value_counts()
    logger.info(
        "Grouped %d article(s) into %d stories (%d with 2+ copies, largest %d); %d updated",
        len(assigned),
        len(sizes),
        int((sizes > 1).sum()),
        int(sizes.max()) if len(sizes) else 0,
        len(changed),
    )
    return changed


def main(argv: list[str] | None = None) -> int:
    """Entry point: assign story ids to articles."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="regroup all articles")
    args = parser.parse_args(argv)
    setup_logging()
    init_db()
    run(load_news_sources(), load_watchlist(), full=args.full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
