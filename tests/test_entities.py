import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import Engine

import processing.entities as entities
from config.loader import ConditionalAlias, Stock, WatchlistError, load_watchlist
from storage import db

AFTER = dt.date(2026, 9, 20)
DEMERGER = dt.date(2025, 10, 14)

HDFCBANK = Stock(
    "HDFCBANK", "HDFCBANK.NS", "HDFC Bank Ltd", "Banks", ("HDFC Bank",),
    ambiguous_aliases=("HDFC",), exclude_patterns=("HDFC Life", "HDFC AMC"),
)  # fmt: skip
RELIANCE = Stock("RELIANCE", "RELIANCE.NS", "Reliance Industries Ltd", "Energy", ("RIL",))
INFY = Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys",))
TMPV = Stock(
    "TMPV", "TMPV.NS", "Tata Motors Passenger Vehicles Ltd", "Auto",
    ("Tata Motors Passenger Vehicles", "JLR"),
    conditional_aliases=(
        ConditionalAlias(("Tata Motors",), ("passenger vehicle", "EV", "Nexon"), DEMERGER),
    ),
)  # fmt: skip
MATCHERS = [entities.StockMatcher(s) for s in (HDFCBANK, RELIANCE, INFY, TMPV)]


def linked(title: str, summary: str | None = None, body: str | None = None, date=AFTER):
    mentions = entities.link_article(title, summary, body, date, MATCHERS)
    return {m["symbol"]: m for m in mentions}


# --- the labelled benchmark ------------------------------------------------------------


def test_labelled_headlines_precision_and_recall() -> None:
    result = entities.evaluate(load_watchlist())
    report = "\n".join(
        [f"precision={result.precision:.3f} recall={result.recall:.3f}"]
        + [f"  FP {s}: {t}" for t, s in result.false_positives]
        + [f"  FN {s}: {t}" for t, s in result.false_negatives]
    )
    print(report)
    assert result.precision >= 0.9, report
    assert result.recall >= 0.9, report


# --- matching rules ----------------------------------------------------------------


def test_uppercase_aliases_are_case_sensitive_full_names_are_not() -> None:
    assert "RELIANCE" in linked("RIL shares rise")
    assert "RELIANCE" not in linked("Ril shares rise")
    assert "INFY" in linked("INFOSYS shares rise")
    assert "INFY" in linked("infosys shares rise")


def test_matching_is_whole_word() -> None:
    assert "RELIANCE" not in linked("RILEY Group shares rise")
    assert "INFY" not in linked("Infosysx launches product")
    assert "RELIANCE" in linked("RIL's retail arm expands")


def test_exclusion_removes_only_the_excluded_phrase() -> None:
    assert linked("HDFC Life shares slump 6%") == {}
    assert "HDFCBANK" in linked("HDFC Life and HDFC Bank shares diverge")


def test_ambiguous_alias_needs_finance_context_in_same_sentence() -> None:
    assert "HDFCBANK" in linked("HDFC shares gain 2%")
    assert linked("HDFC launches new health cover") == {}
    body = "HDFC opened a new branch in Pune. Separately, Nifty shares rose."
    assert linked("Branch news", body=body) == {}


def test_sentence_split_ignores_abbreviations() -> None:
    text = "HDFC Ltd. shares rose 2% today. Others fell."
    assert entities.sentence_at(text, 0) == "HDFC Ltd. shares rose 2% today."


def test_conditional_alias_before_and_after_change_date() -> None:
    title = "Tata Motors sales rise 12% in September"
    assert "TMPV" in linked(title, date=dt.date(2025, 9, 30))  # combined company then
    assert linked(title, date=DEMERGER) == {}  # from the ex-date: needs PV context
    assert "TMPV" in linked("Tata Motors EV sales rise 12%", date=DEMERGER)


def test_longest_name_wins() -> None:
    m = linked("Tata Motors Passenger Vehicles Q2 profit rises")["TMPV"]
    assert m["matched_alias"] == "Tata Motors Passenger Vehicles"
    assert m["mention_count"] == 1


def test_context_terms_starting_with_capital_ignore_lowercase() -> None:
    assert linked("Tata Motors takes a nexon approach to trucks") == {}
    assert "TMPV" in linked("Tata Motors NEXON bookings open")


def test_article_can_link_several_stocks() -> None:
    assert set(linked("Infosys and RIL shares rally")) == {"INFY", "RELIANCE"}


# --- confidence --------------------------------------------------------------------


LONG_FILLER = "The broader market was mixed as investors weighed global cues. " * 40


def test_confidence_ranks_title_over_summary_over_body() -> None:
    title = linked("Infosys wins deal")["INFY"]["confidence"]
    summary = linked("IT major wins deal", summary="Infosys signed it.")["INFY"]["confidence"]
    body = linked("IT major wins deal", body="Infosys signed it. Infosys said.")["INFY"]
    assert title > summary > body["confidence"] >= entities.LINK_THRESHOLD
    assert body["location"] == "body" and body["mention_count"] == 2


def test_single_passing_mention_in_long_body_is_below_threshold() -> None:
    m = linked("Markets end flat", body=LONG_FILLER + "Infosys was also traded.")["INFY"]
    assert m["confidence"] < entities.LINK_THRESHOLD


def test_repeated_body_mentions_raise_confidence() -> None:
    body = LONG_FILLER + "Infosys rose. Infosys guided higher. Infosys hired."
    assert linked("Markets", body=body)["INFY"]["confidence"] >= 0.7


def test_ambiguous_match_scores_below_plain_alias() -> None:
    assert (
        linked("HDFC shares gain")["HDFCBANK"]["confidence"]
        < (linked("HDFC Bank shares gain")["HDFCBANK"]["confidence"])
    )


def test_market_wrap_listing_is_not_about_each_stock() -> None:
    wrap = linked("Sensex, Nifty end higher; HDFC Bank, Infosys, RIL top gainers")
    assert set(wrap) == {"HDFCBANK", "INFY", "RELIANCE"}  # still stored...
    assert all(m["confidence"] < entities.LINK_THRESHOLD for m in wrap.values())  # ...but weak
    subject = linked("HDFC Bank shares drag Nifty lower after Q2 miss")
    assert subject["HDFCBANK"]["confidence"] >= entities.LINK_THRESHOLD


# --- persistence -------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def add(aid: str, title: str) -> None:
    db.insert_new_articles(
        [
            {
                "id": aid,
                "url": f"https://x.com/{aid}",
                "title": title,
                "first_seen_at": dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
                "fetched_via": "test",
            }
        ]
    )


def test_run_links_only_new_or_changed_articles(engine: Engine) -> None:
    stocks = [HDFCBANK, RELIANCE, INFY, TMPV]
    add("a", "Infosys and RIL shares rally")
    add("b", "Tata Steel profit triples")
    assert entities.run(stocks) == 2
    mentions = db.read_mentions()
    assert sorted(zip(mentions["article_id"], mentions["symbol"], strict=True)) == [
        ("a", "INFY"),
        ("a", "RELIANCE"),
    ]
    assert entities.run(stocks) == 0  # nothing new

    db.update_article_text("b", "ok", 1, text="Tata Steel said HDFC Bank led the loan. " * 3)
    assert entities.run(stocks) == 1  # text changed -> relinked
    assert set(db.read_mentions()["symbol"]) == {"INFY", "RELIANCE", "HDFCBANK"}

    assert entities.run(stocks, full=True) == 2
    assert len(db.read_mentions()) == 3  # replacing, not duplicating


# --- loader ------------------------------------------------------------------------


def write_watchlist(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "stocks:\n  - symbol: X\n    yf: X.NS\n    name: X Ltd\n    sector: S\n"
        f"    aliases: [X Corp]\n{extra}",
        encoding="utf-8",
    )
    return path


def test_loader_reads_new_optional_fields(tmp_path: Path) -> None:
    extra = (
        "    ambiguous_aliases: [XC]\n    exclude_patterns: [X Corp Life]\n"
        "    conditional_aliases:\n      - {names: [Xco], context: [cars], from: 2025-10-14}\n"
    )
    [stock] = load_watchlist(write_watchlist(tmp_path, extra))
    assert stock.ambiguous_aliases == ("XC",)
    assert stock.exclude_patterns == ("X Corp Life",)
    assert stock.conditional_aliases == (ConditionalAlias(("Xco",), ("cars",), DEMERGER),)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("    ambiguous_aliases: X Corp\n", "ambiguous_aliases"),
        ("    ambiguous_aliases: [X Corp]\n", "more than once"),
        ("    exclude_patterns: ['']\n", "exclude_patterns"),
        ("    conditional_aliases:\n      - {names: [Xco]}\n", "context"),
        ("    conditional_aliases:\n      - {names: [Xco], context: [a], from: soon}\n", "date"),
        (
            "    conditional_aliases:\n      - {names: [Xco], context: [a], to: 2025-01-01}\n",
            "unknown",
        ),
    ],
)
def test_loader_rejects_bad_new_fields(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(WatchlistError, match=message):
        load_watchlist(write_watchlist(tmp_path, extra))


# --- symbol aliases, Defender, price-move verbs ----------------------------------------

REAL = [entities.StockMatcher(s) for s in load_watchlist()]


def real(title: str, date=AFTER) -> dict[str, dict]:
    return {m["symbol"]: m for m in entities.link_article(title, None, None, date, REAL)}


def test_all_caps_symbol_is_a_strong_case_sensitive_alias() -> None:
    m = real("RELIANCE Outlook for the Week")["RELIANCE"]
    assert (m["matched_alias"], m["confidence"]) == ("RELIANCE", 0.95)
    assert "HDFCBANK" in real("HDFCBANK: support at 740")
    assert real("Firm places reliance on RBI loan guidance") == {}  # common word, lowercase


def test_exclusions_apply_to_all_caps_text() -> None:
    assert real("RELIANCE POWER SHARES SURGE 10%") == {}
    assert real("RELIANCE INFRA HITS UPPER CIRCUIT") == {}
    assert "RELIANCE" in real("RELIANCE POWER AND RELIANCE SHARES DIVERGE")


def test_defender_needs_land_rover_in_the_same_sentence() -> None:
    assert real("Microsoft Defender update fixes flaw") == {}
    assert real("Defender Octa review: Land Rover's toughest SUV")["TMPV"]["mention_count"] == 2
    body = "Microsoft Defender flagged the file. Land Rover was unaffected."
    hits = entities.link_article("Security update", None, body, AFTER, REAL)
    assert [h["matched_alias"] for h in hits] == ["Land Rover"]  # Defender not counted


@pytest.mark.parametrize(
    ("title", "subject"),
    [
        ("Bank Nifty rises above 56,400; HDFC Bank jumps over 2.5%", True),
        ("Sensex, Nifty flat; TCS shares fall 2% on weak deals", True),
        ("Nifty ends higher as RIL rallied 3% on tariff hike", True),
        ("SENSEX flat; Reliance, Bajaj Finance gain, HDFC Bank, Infosys fall", False),
        ("Nifty slips as HDFC Bank and Infosys drop 1% each", False),
        ("Sensex, Nifty end higher; HDFC Bank, Infosys, RIL top gainers", False),
    ],
)
def test_price_move_verb_marks_the_subject_in_market_wraps(title: str, subject: bool) -> None:
    linked_symbols = {
        s for s, m in real(title).items() if m["confidence"] >= entities.LINK_THRESHOLD
    }
    assert bool(linked_symbols) == subject
