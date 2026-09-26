"""Tests for SEC 6-K results: exhibit parsing, XBRL cross-checks and the collector.

The fixtures in tests/fixtures/sec/ are trimmed from real 6-K documents (HDFC Bank FY27Q1
EX-99, HDFC Bank FY22Q1 6-K body in ₹ lac, Infosys FY27Q1 EX-99.3): labels, cell layout
and figures are copied verbatim, other rows removed. No test touches the network.
"""

import datetime as dt
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest
import yaml
from sqlalchemy import Engine, select

import collectors.result_files as rf
import collectors.sec_results as sec
import processing.results as res
from config.loader import load_watchlist
from config.results_overrides import ResultOverrideError, load_result_overrides
from storage import db
from tests.test_results import xbrl

STOCKS = load_watchlist()
FIXTURES = Path(__file__).parent / "fixtures" / "sec"
HDFC_FY27Q1 = (FIXTURES / "hdfcbank_fy27q1_ex99.htm").read_bytes()
HDFC_FY22Q1 = (FIXTURES / "hdfcbank_fy22q1_6k.htm").read_bytes()
INFY_FY27Q1 = (FIXTURES / "infy_fy27q1_ex99_3.htm").read_bytes()
COVER = (
    b"<DOCUMENT>\n<TYPE>6-K\n<TEXT><HTML><BODY><P>FORM 6-K</P><P>Infosys Limited</P></BODY></HTML>"
)


def by_basis(parsed: list[res.ParsedResult]) -> dict[str, res.ParsedResult]:
    return {p.basis: p for p in parsed}


# --- exhibit parsing -----------------------------------------------------------------


def test_hdfc_bank_exhibit_headline_rows() -> None:
    parsed = by_basis(res.parse_sec_exhibit(HDFC_FY27Q1, STOCKS))
    sa, co = parsed["standalone"], parsed["consolidated"]
    assert sa.kind == "sec" and sa.company == "HDFCBANK"
    assert sa.period_end == dt.date(2026, 6, 30) and sa.filed_on == dt.date(2026, 7, 18)
    assert sa.values["interest_earned"] == (79362.78, "INR crore")
    assert sa.values["total_income"][0] == 92184.38  # not the segment table's made-up figure
    assert sa.values["net_profit"][0] == 19059.72
    assert sa.values["eps"] == (12.38, "INR/share")
    assert sa.values["gross_npa_pct"] == (1.17, "%")  # "1.17" and "%" are separate cells
    assert sa.values["nii"][0] == pytest.approx(79362.78 - 45828.83)
    assert sa.precision["net_profit"] == pytest.approx(0.01) and "nii" not in sa.precision
    assert co.values["net_profit"][0] == 19244.71  # after minority interest, not 20382.69
    assert "gross_npa" not in co.values


def test_lakh_figures_in_the_6k_body_are_converted_with_their_precision() -> None:
    parsed = by_basis(res.parse_sec_exhibit(HDFC_FY22Q1, STOCKS))
    sa = parsed["standalone"]
    assert sa.period_end == dt.date(2021, 6, 30) and sa.filed_on == dt.date(2021, 7, 17)
    assert sa.values["interest_earned"][0] == pytest.approx(30482.97)  # 3048297 lac
    assert sa.precision["interest_earned"] == pytest.approx(0.01)  # 1 lac = 0.01 crore
    assert sa.precision["eps"] == pytest.approx(0.1)  # printed "14.0"
    assert parsed["consolidated"].values["net_profit"][0] == pytest.approx(7922.09)


def test_infosys_exhibit_owner_profit_standalone_and_no_ifrs() -> None:
    parsed = by_basis(res.parse_sec_exhibit(INFY_FY27Q1, STOCKS))
    co, sa = parsed["consolidated"], parsed["standalone"]
    assert set(parsed) == {"consolidated", "standalone"}  # IFRS US$ section skipped
    assert co.values["net_profit"][0] == 7769  # owners of the company, not 7,775
    assert co.values["eps"][0] == 19.19  # ₹ EPS, not the IFRS $0.20
    assert co.precision["revenue"] == 1.0  # whole crore
    assert co.filed_on == dt.date(2026, 7, 23)  # "taken on record by the Board ... held on"
    assert sa.period_end == dt.date(2026, 6, 30)  # undated heading takes the previous period
    assert sa.values == {"revenue": (39957.0, "INR crore"), "net_profit": (7249.0, "INR crore")}


def test_exhibit_without_indian_standards_is_rejected() -> None:
    content = INFY_FY27Q1.replace(
        b"in compliance with the Indian Accounting Standards (Ind-AS)", b""
    )
    with pytest.raises(res.ResultParseError, match="Ind AS / Indian GAAP"):
        res.parse_sec_exhibit(content, STOCKS)
    assert res.has_results_table(content)  # so the collector fails loudly on it


def test_has_results_table_only_for_results_documents() -> None:
    assert res.has_results_table(HDFC_FY27Q1) and res.has_results_table(INFY_FY27Q1)
    assert not res.has_results_table(COVER)


def test_sec_documents_are_dispatched_to_the_sec_parser(tmp_path: Path) -> None:
    path = tmp_path / "x.htm"
    path.write_bytes(INFY_FY27Q1)
    assert {p.kind for p in res.parse_file(path, STOCKS)} == {"sec"}


# --- cross-check against XBRL --------------------------------------------------------


def infy_consolidated() -> res.ParsedResult:
    return by_basis(res.parse_sec_exhibit(INFY_FY27Q1, STOCKS))["consolidated"]


def test_xbrl_rounded_to_printed_precision_matches() -> None:
    p = infy_consolidated()
    xb = {"revenue": 48210.51, "total_income": 49194.5, "net_profit": 7769.2, "eps": 19.194}
    assert res.sec_mismatches("INFY", p, xb) == []  # 48210.51 -> 48,211; ties may go either way


def test_any_other_difference_names_symbol_quarter_basis_and_metric() -> None:
    p = infy_consolidated()
    problems = res.sec_mismatches("INFY", p, {"revenue": 48211.6, "eps": 19.2, "x:Other": 1.0})
    assert len(problems) == 2
    assert problems[0].startswith("INFY FY27Q1 consolidated eps: 6-K prints 19.19")
    assert problems[1].startswith("INFY FY27Q1 consolidated revenue: 6-K prints 48,211")


def test_lakh_precision_is_strict() -> None:
    sa = by_basis(res.parse_sec_exhibit(HDFC_FY22Q1, STOCKS))["standalone"]
    assert res.sec_mismatches("HDFCBANK", sa, {"interest_earned": 30482.974}) == []
    assert res.sec_mismatches("HDFCBANK", sa, {"interest_earned": 30482.99}) != []


def test_ranking_is_xbrl_then_sec_then_pdf() -> None:
    def one(kind: str, value: float) -> res.ParsedResult:
        return res.ParsedResult(kind, dt.date(2026, 6, 30), "standalone",
                                values={"revenue": (value, "INR crore")})  # fmt: skip

    now = dt.datetime(2026, 9, 26, tzinfo=dt.UTC)
    rows = res.build_rows([("INFY", "p", one("pdf", 1)), ("INFY", "s", one("sec", 2))], now)
    assert [(r["source"], r["trust"], r["value"]) for r in rows] == [("sec", "high", 2)]
    rows = res.build_rows(
        [("INFY", "s", one("sec", 2)), ("INFY", "x", one("xbrl", 3)), ("INFY", "p", one("pdf", 1))],
        now,
    )
    assert [(r["source"], r["value"]) for r in rows] == [("xbrl", 3)]


def test_rebuild_logs_sec_conflicting_with_later_xbrl(engine: Engine, caplog) -> None:
    stock = next(s for s in STOCKS if s.symbol == "INFY")
    parsed = res.parse_sec_exhibit(INFY_FY27Q1, STOCKS)
    rf.store_result_file(INFY_FY27Q1, "htm", stock, parsed, "SEC")
    content = xbrl(nature="Consolidated", revenue=482_200_000_000, profit=77_690_000_000, eps=19.19)
    rf.store_result_file(content, "xml", stock, [res.parse_xbrl(content)], "NSE")
    res.rebuild(STOCKS)
    assert "INFY FY27Q1 consolidated revenue: 6-K prints 48,211" in caplog.text
    r = db.read_results().set_index(["basis", "metric"])
    assert r.loc[("consolidated", "revenue"), "source"] == "xbrl"
    assert r.loc[("standalone", "revenue"), "source"] == "sec"  # no standalone XBRL


def test_checklist_counts_sec_quarters_as_done(engine: Engine) -> None:
    stock = next(s for s in STOCKS if s.symbol == "INFY")
    rf.store_result_file(INFY_FY27Q1, "htm", stock, res.parse_sec_exhibit(INFY_FY27Q1, STOCKS),
                         "SEC")  # fmt: skip
    res.rebuild(STOCKS)
    table = rf.checklist(STOCKS, dt.date(2026, 9, 26)).set_index(["symbol", "quarter", "basis"])
    done = table.loc[("INFY", "FY27Q1", "standalone")]
    assert done["status"] == "ok (SEC 6-K)" and done["download_from"] == ""


# --- collector -----------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(rf, "FILINGS_DIR", tmp_path / "filings")
    monkeypatch.setattr("utils.retry.time.sleep", lambda _s: None)
    return engine


def test_window_quarter() -> None:
    assert sec.window_quarter(dt.date(2026, 7, 23)) == dt.date(2026, 6, 30)
    assert sec.window_quarter(dt.date(2026, 7, 1)) == dt.date(2026, 6, 30)
    assert sec.window_quarter(dt.date(2026, 6, 30)) is None  # 91 days after March
    assert sec.window_quarter(dt.date(2026, 5, 20)) == dt.date(2026, 3, 31)


def test_six_ks_filters_form_and_date() -> None:
    subs = {"filings": {"recent": {
        "form": ["6-K", "20-F", "6-K/A", "6-K"],
        "accessionNumber": ["a-3", "a-2", "a-1", "a-0"],
        "filingDate": ["2026-07-23", "2026-06-01", "2026-05-01", "2020-01-01"],
    }}}  # fmt: skip
    found = sec.six_ks(subs, dt.date(2021, 9, 26))
    assert [f.accession for f in found] == ["a-1", "a-3"]


def test_user_agent_requires_contact_email(monkeypatch) -> None:
    monkeypatch.setattr(sec, "load_dotenv", lambda: None)
    monkeypatch.delenv("SEC_CONTACT_EMAIL", raising=False)
    with pytest.raises(sec.SecConfigError):
        sec.user_agent()
    monkeypatch.setenv("SEC_CONTACT_EMAIL", "me@example.com")
    assert sec.user_agent() == "stock-intel me@example.com"


CIK = "0001067491"
FOLDER = "https://www.sec.gov/Archives/edgar/data/1067491/"


def submissions(*filings: tuple[str, str]) -> dict:
    return {"filings": {"recent": {
        "form": ["6-K"] * len(filings),
        "accessionNumber": [a for a, _ in filings],
        "filingDate": [d for _, d in filings],
    }}}  # fmt: skip


class FakeSec:
    """A MockTransport handler serving EDGAR JSON and documents; records every request."""

    def __init__(self, subs: dict, filings: dict[str, dict[str, bytes]]) -> None:
        self.subs, self.filings, self.requests = subs, filings, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, request.headers.get("User-Agent")))
        if url.endswith("/robots.txt"):
            if request.url.host == "data.sec.gov":
                return httpx.Response(404)
            return httpx.Response(200, text="User-agent: *\nAllow: /Archives/edgar/data\n")
        if url == sec.SUBMISSIONS_URL.format(cik=CIK):
            return httpx.Response(200, json=self.subs)
        for accession, docs in self.filings.items():
            base = FOLDER + accession.replace("-", "") + "/"
            if url == base + "index.json":
                items = [{"name": n} for n in [*docs, f"{accession}-index.htm"]]
                return httpx.Response(200, text=json.dumps({"directory": {"item": items}}))
            for name, content in docs.items():
                if url == base + name:
                    return httpx.Response(200, content=content)
        return httpx.Response(404)

    def archive_requests(self) -> list[str]:
        return [u for u, _ in self.requests if "/Archives/" in u]


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(sec, "SEC_SOURCES", {"INFY": CIK})
    sleeps: list[float] = []
    real_limiter = sec.DomainRateLimiter
    monkeypatch.setattr(sec, "DomainRateLimiter",
                        lambda interval: real_limiter(interval, sleep=sleeps.append))  # fmt: skip

    def make(handler: FakeSec) -> httpx.Client:
        headers = {"User-Agent": "stock-intel me@example.com"}
        return httpx.Client(transport=httpx.MockTransport(handler), headers=headers)

    make.sleeps = sleeps  # type: ignore[attr-defined]
    return make


def store_infy_xbrl(revenue: float = 482_106_000_000) -> None:
    stock = next(s for s in STOCKS if s.symbol == "INFY")
    for content in (
        xbrl(nature="Consolidated", revenue=revenue, profit=77_690_000_000, eps=19.19),
        xbrl(nature="Standalone", revenue=399_570_000_000, profit=72_490_000_000, eps=17.46),
    ):
        rf.store_result_file(content, "xml", stock, [res.parse_xbrl(content)], "NSE")


def test_collector_stores_validated_exhibit_and_skips_known_filings(engine, fake_client) -> None:
    store_infy_xbrl()
    handler = FakeSec(
        submissions(("0001-26-01", "2026-07-13"), ("0001-26-02", "2026-07-23"),
                    ("0001-26-03", "2026-07-28")),
        {"0001-26-01": {"index.htm": COVER},
         "0001-26-02": {"index.htm": COVER, "exv99w02.htm": COVER, "exv99w03.htm": INFY_FY27Q1}},
    )  # fmt: skip
    client = fake_client(handler)

    assert sec.collect_all(STOCKS, client=client) == {}
    files = db.read_result_files()
    stored = files[files["exchange"] == "SEC"]
    assert (
        len(stored) == 1
        and stored.iloc[0]["subject"] == "FY27Q1 consolidated+standalone results (sec)"
    )
    checked = db.checked_sec_filings("INFY")
    assert checked == {  # -03 skipped: both bases of FY27Q1 are covered
        "0001-26-01": (None, set()),
        "0001-26-02": (dt.date(2026, 6, 30), {"consolidated", "standalone"}),
    }
    assert not any("0001260003" in u for u in handler.archive_requests())  # quarter covered
    assert {ua for _, ua in handler.requests} == {"stock-intel me@example.com"}
    assert fake_client.sleeps and min(fake_client.sleeps) > 0  # rate limited per host

    handler.requests.clear()
    assert sec.collect_all(STOCKS, client=client) == {}
    assert handler.archive_requests() == []  # nothing fetched twice


def test_collector_fails_loudly_on_xbrl_mismatch_and_stores_nothing(engine, fake_client) -> None:
    store_infy_xbrl(revenue=482_120_000_000)  # 48,212 crore vs the 6-K's 48,211
    handler = FakeSec(submissions(("0001-26-02", "2026-07-23")),
                      {"0001-26-02": {"exv99w03.htm": INFY_FY27Q1}})  # fmt: skip
    failures = sec.collect_all(STOCKS, client=fake_client(handler))
    assert "INFY FY27Q1 consolidated revenue: 6-K prints 48,211" in failures["INFY"]
    assert (db.read_result_files()["exchange"] != "SEC").all()
    assert db.checked_sec_filings("INFY") == {}  # retried (and fails again) next run


def test_collector_fails_loudly_when_no_exhibit_matches(engine, fake_client) -> None:
    unreadable = INFY_FY27Q1.replace(
        b"in compliance with the Indian Accounting Standards (Ind-AS)", b""
    )
    handler = FakeSec(submissions(("0001-26-02", "2026-07-23")),
                      {"0001-26-02": {"index.htm": COVER, "exv99w03.htm": unreadable}})  # fmt: skip
    failures = sec.collect_all(STOCKS, client=fake_client(handler))
    assert "no exhibit matched: exv99w03.htm: no Ind AS / Indian GAAP" in failures["INFY"]
    assert db.checked_sec_filings("INFY") == {}


def test_filing_date_falls_back_to_edgar_date_and_survives_rebuild(engine, fake_client) -> None:
    no_board_date = INFY_FY27Q1.replace(b"taken on record by the Board", b"noted")
    handler = FakeSec(submissions(("0001-26-02", "2026-07-24")),
                      {"0001-26-02": {"exv99w03.htm": no_board_date}})  # fmt: skip
    assert sec.collect_all(STOCKS, client=fake_client(handler)) == {}
    res.rebuild(STOCKS)
    f = db.read_result_files().iloc[0]
    assert f["filed_at"].astimezone(res.IST).date() == dt.date(2026, 7, 24)
    assert "approx" not in f["subject"]


def test_a_quarter_with_one_basis_keeps_checking_its_window(engine, fake_client) -> None:
    standalone_only = INFY_FY27Q1.replace(b"(in crore, except per equity share data)", b"")
    handler = FakeSec(
        submissions(("0001-26-02", "2026-07-23"), ("0001-26-03", "2026-07-23")),
        {"0001-26-02": {"exv99w06.htm": standalone_only},
         "0001-26-03": {"exv99w03.htm": INFY_FY27Q1}},
    )  # fmt: skip
    assert sec.collect_all(STOCKS, client=fake_client(handler)) == {}
    checked = db.checked_sec_filings("INFY")
    assert checked["0001-26-02"] == (dt.date(2026, 6, 30), {"standalone"})
    assert checked["0001-26-03"][1] == {"consolidated", "standalone"}


def test_half_year_heading_is_read() -> None:
    content = INFY_FY27Q1.replace(b"quarter ended June 30", b"quarter and half-year ended June 30")
    assert "consolidated" in by_basis(res.parse_sec_exhibit(content, STOCKS))


def test_results_table_without_a_readable_heading_still_fails_loudly(engine, fake_client) -> None:
    content = INFY_FY27Q1.replace(b"Statement of Consolidated Audited Results", b"Statement")
    content = content.replace(b"results of Infosys Limited</P>", b"of Infosys Limited</P>")
    with pytest.raises(res.ResultParseError):
        res.parse_sec_exhibit(content, STOCKS)
    assert res.has_results_table(content)
    handler = FakeSec(submissions(("0001-26-02", "2026-07-23")),
                      {"0001-26-02": {"exv99w03.htm": content}})  # fmt: skip
    assert "no exhibit matched" in sec.collect_all(STOCKS, client=fake_client(handler))["INFY"]


def test_second_copy_of_stored_results_is_a_duplicate(engine, fake_client, monkeypatch) -> None:
    handler = FakeSec(
        submissions(("0001-26-02", "2026-07-23"), ("0001-26-03", "2026-07-28")),
        {"0001-26-02": {"exv99w03.htm": INFY_FY27Q1},
         "0001-26-03": {"exv99w06.htm": INFY_FY27Q1.replace(b"Exhibit 99.3", b"Exhibit 99.6")}},
    )  # fmt: skip
    monkeypatch.setattr(sec, "window_quarter", lambda _d: None)  # fetch the second 6-K too
    assert sec.collect_all(STOCKS, client=fake_client(handler)) == {}
    columns = (db.SEC_CHECKED.c.accession, db.SEC_CHECKED.c.outcome)
    with engine.connect() as conn:
        outcomes = dict(conn.execute(select(*columns)).all())
    assert outcomes == {"0001-26-02": "results", "0001-26-03": "duplicate"}
    assert (db.read_result_files()["exchange"] == "SEC").sum() == 1


def test_recheck_forgets_only_filings_without_results(engine, fake_client) -> None:
    handler = FakeSec(submissions(("0001-26-01", "2026-07-13"), ("0001-26-02", "2026-07-23")),
                      {"0001-26-01": {"index.htm": COVER},
                       "0001-26-02": {"exv99w03.htm": INFY_FY27Q1}})  # fmt: skip
    sec.collect_all(STOCKS, client=fake_client(handler))
    assert db.forget_sec_filings_without_results() == 1
    assert set(db.checked_sec_filings("INFY")) == {"0001-26-02"}


# --- overrides -----------------------------------------------------------------------

ACCESSION = "0001067491-26-000034"


def write_overrides(path: Path, **changes) -> Path:
    entry = {
        "symbol": "INFY", "quarter": "FY27Q1", "basis": "consolidated", "metric": "revenue",
        "value": 48211, "source": ACCESSION,
        "xbrl_check": "SegmentProfitBeforeTax = ProfitLossFromOrdinaryActivitiesBeforeTax",
        "reason": "test",
    }  # fmt: skip
    entry.update(changes)
    path.write_text(yaml.safe_dump({"overrides": [entry]}), encoding="utf-8")
    return path


def check_facts(segment: float, pbt: float) -> str:
    """XBRL facts for the xbrl_check identity, in absolute INR."""
    return "".join(
        f'<in-capmkt:{n} contextRef="OneD" unitRef="INR">{v * 1e7:.0f}</in-capmkt:{n}>'
        for n, v in (("SegmentProfitBeforeTax", segment),
                     ("ProfitLossFromOrdinaryActivitiesBeforeTax", pbt))
    )  # fmt: skip


def test_overrides_file_is_validated(tmp_path: Path) -> None:
    ok = load_result_overrides(write_overrides(tmp_path / "a.yaml"))
    ov = ok[("INFY", "FY27Q1", "consolidated", "revenue")]
    left, right = ov.check_sides()
    assert ov.value == 48211.0 and left == [(1, "SegmentProfitBeforeTax")]
    assert right == [(1, "ProfitLossFromOrdinaryActivitiesBeforeTax")]
    for bad in ({"source": "d101962dex99"}, {"value": "high"}, {"xbrl_check": "A + B"},
                {"xbrl_check": "A B = C"}, {"reason": ""}, {"quarter": "2026Q1"}):  # fmt: skip
        with pytest.raises(ResultOverrideError):
            load_result_overrides(write_overrides(tmp_path / "b.yaml", **bad))
    assert load_result_overrides(tmp_path / "missing.yaml") == {}


def test_override_needs_a_contradicting_6k_and_an_inconsistent_xbrl(tmp_path: Path) -> None:
    ov = next(iter(load_result_overrides(write_overrides(tmp_path / "o.yaml")).values()))
    sec_part = infy_consolidated()  # prints revenue 48,211

    def x(revenue: float, segment: float, pbt: float) -> res.ParsedResult:
        return res.parse_xbrl(xbrl(nature="Consolidated", revenue=revenue * 1e7,
                                   extra=check_facts(segment, pbt)))  # fmt: skip

    assert res.override_problem(ov, x(48220, 100, 90), sec_part) is None
    assert "holds" in res.override_problem(ov, x(48220, 100, 100), sec_part)
    assert "already agrees" in res.override_problem(ov, x(48211.3, 100, 90), sec_part)
    assert "isn't stored" in res.override_problem(ov, x(48220, 100, 90), None)
    wrong = load_result_overrides(write_overrides(tmp_path / "w.yaml", value=48300))
    assert "prints 48,211" in res.override_problem(next(iter(wrong.values())),
                                                   x(48220, 100, 90), sec_part)  # fmt: skip
    no_facts = res.parse_xbrl(xbrl(nature="Consolidated", revenue=482_200_000_000))
    assert "has no SegmentProfitBeforeTax" in res.override_problem(ov, no_facts, sec_part)


def store_bad_infy_xbrl(segment: float, pbt: float) -> None:
    """Consolidated XBRL whose revenue (48,220) contradicts the 6-K (48,211)."""
    stock = next(s for s in STOCKS if s.symbol == "INFY")
    content = xbrl(nature="Consolidated", revenue=482_200_000_000, profit=77_690_000_000,
                   eps=19.19, extra=check_facts(segment, pbt))  # fmt: skip
    rf.store_result_file(content, "xml", stock, [res.parse_xbrl(content)], "NSE")


def test_valid_override_lets_the_6k_in_and_corrects_the_xbrl(
    engine, fake_client, monkeypatch, tmp_path
) -> None:
    overrides = load_result_overrides(write_overrides(tmp_path / "o.yaml"))
    monkeypatch.setattr(sec, "load_result_overrides", lambda: overrides)
    monkeypatch.setattr(res, "load_result_overrides", lambda: overrides)
    store_bad_infy_xbrl(segment=100, pbt=90)
    handler = FakeSec(submissions((ACCESSION, "2026-07-23")),
                      {ACCESSION: {"exv99w03.htm": INFY_FY27Q1}})  # fmt: skip
    assert sec.collect_all(STOCKS, client=fake_client(handler)) == {}

    res.rebuild(STOCKS)
    r = db.read_results().set_index(["basis", "metric"]).loc[("consolidated", "revenue")]
    assert (r["source"], r["value"], r["corrected_from"]) == ("xbrl", 48211.0, 48220.0)
    assert r["correction"] == f"6-K {ACCESSION}: test"
    line = res.report().set_index("quarter").loc["FY27Q1"]
    assert line["revenue"] == "48,211c"
    assert f"corrected revenue: 48,211.00 (XBRL 48,220.00; 6-K {ACCESSION})" in line["note"]


def test_override_is_refused_when_the_xbrl_is_consistent(
    engine, fake_client, monkeypatch, tmp_path, caplog
) -> None:
    overrides = load_result_overrides(write_overrides(tmp_path / "o.yaml"))
    monkeypatch.setattr(sec, "load_result_overrides", lambda: overrides)
    monkeypatch.setattr(res, "load_result_overrides", lambda: overrides)
    store_bad_infy_xbrl(segment=100, pbt=100)  # identity holds: no evidence of an XBRL error
    handler = FakeSec(submissions((ACCESSION, "2026-07-23")),
                      {ACCESSION: {"exv99w03.htm": INFY_FY27Q1}})  # fmt: skip
    failure = sec.collect_all(STOCKS, client=fake_client(handler))["INFY"]
    assert "override INFY FY27Q1 consolidated revenue not applied: xbrl_check holds" in failure
    assert "revenue: 6-K prints 48,211" in failure
    res.rebuild(STOCKS)
    assert "Override INFY FY27Q1 consolidated revenue not applied" in caplog.text
    r = db.read_results().set_index(["basis", "metric"]).loc[("consolidated", "revenue")]
    assert r["value"] == 48220.0 and pd.isna(r["corrected_from"])
