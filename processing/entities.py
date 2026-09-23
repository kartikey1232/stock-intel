"""Entity linking: which watchlist stocks is each article actually about?

For every article not yet linked, each stock's names are matched in the title, summary
and body text:

- Whole-word matching. All-uppercase aliases (RIL, TCS, HDFC) are case-sensitive; other
  strong names ignore case; ambiguous names must be Capitalised or ALL CAPS ("reliance"
  the word never matches). Longer names win over names they contain ("Tata Motors Passenger
  Vehicles" is one match, not also "Tata Motors").
- The stock's exclude_patterns are blanked out first ("HDFC Life" can't match "HDFC").
- ambiguous_aliases count only with finance context in the same sentence.
- conditional_aliases count only with their own context in the same sentence, from
  their `from` date on (the article date is published_at, else first_seen_at).
- The stock's all-caps NSE symbol is always a strong alias.
- Confidence: a title match is strongest, then summary, then body. A single passing
  mention in a long body is weak. Ambiguous or context-dependent matches score a bit
  lower. In market-wrap headlines (Sensex/Nifty/top gainers...) a stock's title
  confidence is halved unless it's the subject: the sole watchlist stock named before
  the index term, or directly followed by a price-move verb ("HDFC Bank jumps 2.5%")
  without being the tail of a list ("HDFC Bank, Infosys fall").

Mentions with confidence >= LINK_THRESHOLD count as "the article is about this stock".
All mentions are stored so downstream steps can choose their own cut-off.

Run with:  uv run python -m processing.entities [--full] [--evaluate]
"""

import argparse
import datetime as dt
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from config.loader import Stock, load_watchlist
from storage.db import articles_to_link, init_db, read_mentions, replace_mentions
from utils import setup_logging

logger = logging.getLogger(__name__)

LINK_THRESHOLD = 0.5
LONG_BODY_CHARS = 1500
LOCATIONS = ("title", "summary", "body")
FINANCE_CONTEXT = (
    "share", "stock", "result", "profit", "loss", "target", "earnings", "revenue",
    "dividend", "quarter", "quarterly", "price", "m-cap", "market cap", "valuation",
    "brokerage", "rating", "guidance", "buyback", "stake", "investor", "margin", "loan",
    "deposit", "net interest income", "listing", "Sensex", "Nifty",
    "NSE", "BSE", "IPO", "NII", "NPA", "RBI", "CEO", "Q1", "Q2", "Q3", "Q4",
)  # fmt: skip
MARKET_WRAP_TERMS = (
    "Sensex", "Nifty", "stock market today", "closing bell", "opening bell",
    "market wrap", "top gainers", "top losers", "market highlights",
)  # fmt: skip
ABBREVIATIONS = {"ltd", "rs", "co", "inc", "corp", "mr", "mrs", "ms", "dr", "no", "vs", "st"}
SENTENCE_BREAK_RE = re.compile(r"\n+|(?<=[.!?])\s+(?=[A-Z0-9\"'(‘“])")


# --- regex building ----------------------------------------------------------------


def term_pattern(term: str, *, alias: bool = False, plural: bool = False) -> str:
    """Whole-word regex for `term`.

    Case rules: all-uppercase terms (RIL, NSE, PV) are case-sensitive. Other aliases are
    case-insensitive. Other context terms that start with a capital (Nexon, Punch,
    Sensex) match Capitalised or ALL CAPS but not lowercase, so "punch" the verb doesn't
    count; lowercase context terms ignore case.
    """
    body = r"\s+".join(re.escape(word) for word in term.split())
    suffix = "(?:s|es)?" if plural else ""
    letters = [c for c in term if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        core = f"(?:{body}{suffix})"
    elif alias or term[0].islower():
        core = f"(?i:{body}{suffix})"
    else:
        upper = r"\s+".join(re.escape(word.upper()) for word in term.split())
        core = f"(?:{body}{suffix}|{upper}{suffix.upper()})"
    return rf"(?<!\w){core}(?!\w)"


def any_term_regex(terms: tuple[str, ...], plural: bool = True) -> re.Pattern[str]:
    """One regex matching any of the context `terms` (optionally pluralised)."""
    return re.compile("|".join(term_pattern(t, plural=plural) for t in terms))


FINANCE_RE = any_term_regex(FINANCE_CONTEXT)
MARKET_WRAP_RE = any_term_regex(MARKET_WRAP_TERMS, plural=False)
# A stock followed by a price-move verb is the subject of its own clause, even inside a
# market wrap ("Nifty slips; HDFC Bank jumps 2.5%"). "shares"/"stock" may sit in between.
PRICE_MOVE_RE = re.compile(
    r"\s+(?:shares?\s+|stocks?\s+)?"
    r"(?:jump(?:s|ed)?|fall(?:s|ing)?|fell|rise(?:s|n)?|rose|rising|gain(?:s|ed)?|"
    r"slip(?:s|ped)?|surge(?:s|d)?|tank(?:s|ed)?|rall(?:y|ies|ied)|drop(?:s|ped)?|"
    r"climb(?:s|ed)?)\b",
    re.IGNORECASE,
)
# ...unless it's the last item of a list the verb belongs to: "HDFC Bank, Infosys fall".
LIST_BEFORE_RE = re.compile(r"(?:,|&|\band)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Name:
    """One matchable name for a stock and the rule that governs it."""

    text: str
    kind: str  # alias | ambiguous | conditional
    context: re.Pattern[str] | None = None
    from_date: dt.date | None = None


class StockMatcher:
    """Precompiled matching rules for one stock."""

    def __init__(self, stock: Stock) -> None:
        self.symbol = stock.symbol
        names = [Name(a, "alias") for a in stock.aliases]
        if stock.symbol not in stock.aliases:
            # The all-caps NSE symbol (RELIANCE, INFY) is always a strong alias. It's
            # case-sensitive, and exclude_patterns (case-insensitive) still mask e.g.
            # "RELIANCE POWER" before matching.
            names.append(Name(stock.symbol, "alias"))
        names += [Name(a, "ambiguous", FINANCE_RE) for a in stock.ambiguous_aliases]
        for cond in stock.conditional_aliases:
            context = any_term_regex(cond.context)
            names += [Name(n, "conditional", context, cond.from_date) for n in cond.names]
        # Longest first so a name never also matches as the shorter name it contains;
        # among equal lengths, strong aliases first ("RELIANCE" before ambiguous "Reliance").
        priority = {"alias": 0, "conditional": 1, "ambiguous": 2}
        self.names = sorted(names, key=lambda n: (-len(n.text), priority[n.kind]))
        # Strong names ignore case. Ambiguous names are often common words too ("reliance"),
        # so like context terms they must be Capitalised or ALL CAPS.
        self.regex = re.compile(
            "|".join(f"({term_pattern(n.text, alias=n.kind != 'ambiguous')})" for n in self.names)
        )
        excludes = sorted(stock.exclude_patterns, key=len, reverse=True)  # longest wins
        self.exclude = (
            re.compile("|".join(f"(?i:{term_pattern(p)})" for p in excludes)) if excludes else None
        )

    def matches(self, text: str, event_date: dt.date) -> list[tuple[Name, int, int]]:
        """(name, start, end) for every valid mention of this stock in `text`."""
        if not text:
            return []
        if self.exclude:
            text = self.exclude.sub(lambda m: " " * len(m.group(0)), text)
        found = []
        for m in self.regex.finditer(text):
            name = self.names[m.lastindex - 1]
            if self._context_ok(name, text, m.start(), event_date):
                found.append((name, m.start(), m.end()))
        return found

    @staticmethod
    def _context_ok(name: Name, text: str, pos: int, event_date: dt.date) -> bool:
        if name.kind == "alias":
            return True
        if name.kind == "conditional" and name.from_date and event_date < name.from_date:
            return True  # before the change, it was an ordinary alias
        return bool(name.context and name.context.search(sentence_at(text, pos)))


def sentence_at(text: str, pos: int) -> str:
    """The sentence of `text` containing position `pos` (a headline is one sentence)."""
    start = 0
    for m in SENTENCE_BREAK_RE.finditer(text):
        if "\n" not in m.group(0):  # a space after . ! ?: skip abbreviations like "Ltd."
            before = text[: m.start()].rstrip(".!?").split()
            if before and before[-1].lower() in ABBREVIATIONS:
                continue
        if m.start() >= pos:
            return text[start : m.start()]
        start = m.end()
    return text[start:]


# --- linking -----------------------------------------------------------------------


def link_article(
    title: str,
    summary: str | None,
    body: str | None,
    event_date: dt.date,
    matchers: list[StockMatcher],
) -> list[dict]:
    """Mentions for one article: symbol, matched_alias, location, mention_count, confidence."""
    fields = {"title": title or "", "summary": summary or "", "body": body or ""}
    found = {
        m.symbol: {loc: m.matches(text, event_date) for loc, text in fields.items()}
        for m in matchers
    }
    found = {sym: locs for sym, locs in found.items() if any(locs.values())}

    wrap = MARKET_WRAP_RE.search(fields["title"])
    in_title = [sym for sym, locs in found.items() if locs["title"]]

    mentions = []
    for symbol, locs in found.items():
        location = next(loc for loc in LOCATIONS if locs[loc])
        hits = locs[location]
        strong = any(name.kind == "alias" or _pre_change(name, event_date) for name, *_ in hits)
        confidence = _confidence(location, strong, sum(map(len, locs.values())), fields["body"])
        if location == "title" and wrap:
            leads = in_title == [symbol] and hits[0][1] < wrap.start()
            if not (leads or any(_moves_itself(fields["title"], s, e) for _, s, e in hits)):
                confidence *= 0.5
        mentions.append(
            {
                "symbol": symbol,
                "matched_alias": hits[0][0].text,
                "location": location,
                "mention_count": sum(map(len, locs.values())),
                "confidence": round(confidence, 3),
            }
        )
    return mentions


def _moves_itself(title: str, start: int, end: int) -> bool:
    """True if the name at [start, end) is directly followed by a price-move verb and
    isn't the tail of a list ("HDFC Bank, Infosys fall")."""
    return bool(PRICE_MOVE_RE.match(title, end)) and not LIST_BEFORE_RE.search(title[:start])


def _pre_change(name: Name, event_date: dt.date) -> bool:
    return name.kind == "conditional" and bool(name.from_date) and event_date < name.from_date


def _confidence(location: str, strong: bool, count: int, body: str) -> float:
    """Base confidence by where the stock was found and how often."""
    if location == "title":
        return 0.95 if strong else 0.8
    if location == "summary":
        return 0.75 if strong else 0.65
    if count >= 3:
        score = 0.7
    elif count == 2 or len(body) < LONG_BODY_CHARS:
        score = 0.55
    else:
        score = 0.3  # a single passing mention in a long article
    return score if strong else score - 0.1


def event_date_of(published_at: object, first_seen_at: object) -> dt.date:
    """Article date for date-dependent rules: published_at, else first_seen_at."""
    value = (
        published_at if published_at is not None and not pd.isna(published_at) else first_seen_at
    )
    return pd.Timestamp(value).date()


def run(stocks: list[Stock], full: bool = False) -> int:
    """Link articles needing it and store their mentions. Returns articles processed."""
    articles = articles_to_link(full=full)
    articles = articles.astype(object).where(articles.notna(), None)  # NULL text -> None
    matchers = [StockMatcher(s) for s in stocks]
    rows = []
    for a in articles.itertuples(index=False):
        date = event_date_of(a.published_at, a.first_seen_at)
        for mention in link_article(a.title, a.summary, a.text, date, matchers):
            rows.append({"article_id": a.id, **mention})
    replace_mentions(articles["id"].tolist(), rows)
    linked = sum(1 for r in rows if r["confidence"] >= LINK_THRESHOLD)
    logger.info(
        "Linked %d article(s): %d mention(s), %d at confidence >= %.2f",
        len(articles),
        len(rows),
        linked,
        LINK_THRESHOLD,
    )
    return len(articles)


# --- evaluation --------------------------------------------------------------------

DEFAULT_LABELS_PATH = Path(__file__).resolve().parents[1] / "tests/fixtures/labelled_headlines.yaml"


@dataclass
class Evaluation:
    """Precision/recall of linking against labelled headlines."""

    precision: float
    recall: float
    false_positives: list[tuple[str, str]]
    false_negatives: list[tuple[str, str]]


def evaluate(stocks: list[Stock], path: Path = DEFAULT_LABELS_PATH) -> Evaluation:
    """Score linking (confidence >= LINK_THRESHOLD) against a labelled headline file."""
    with path.open(encoding="utf-8") as fh:
        cases = yaml.safe_load(fh)["headlines"]
    matchers = [StockMatcher(s) for s in stocks]
    tp, fps, fns = 0, [], []
    for case in cases:
        date = case.get("date", dt.date(2026, 9, 20))
        mentions = link_article(case["title"], case.get("summary"), None, date, matchers)
        predicted = {m["symbol"] for m in mentions if m["confidence"] >= LINK_THRESHOLD}
        expected = set(case.get("about") or [])
        tp += len(predicted & expected)
        fps += [(case["title"], s) for s in sorted(predicted - expected)]
        fns += [(case["title"], s) for s in sorted(expected - predicted)]
    precision = tp / (tp + len(fps)) if tp + len(fps) else 1.0
    recall = tp / (tp + len(fns)) if tp + len(fns) else 1.0
    return Evaluation(precision, recall, fps, fns)


def main(argv: list[str] | None = None) -> int:
    """Entry point: link articles to stocks, or --evaluate against labelled headlines."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="re-link all articles")
    parser.add_argument("--evaluate", action="store_true", help="score labelled headlines")
    args = parser.parse_args(argv)
    setup_logging()
    stocks = load_watchlist()
    if args.evaluate:
        result = evaluate(stocks)
        logger.info("precision %.3f, recall %.3f", result.precision, result.recall)
        for title, symbol in result.false_positives:
            logger.info("false positive %s: %s", symbol, title)
        for title, symbol in result.false_negatives:
            logger.info("false negative %s: %s", symbol, title)
        return 0
    init_db()
    run(stocks, full=args.full)
    counts = read_mentions()
    if not counts.empty:
        linked = counts[counts["confidence"] >= LINK_THRESHOLD]
        logger.info("Articles about each stock: %s", linked["symbol"].value_counts().to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
