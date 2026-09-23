"""Tests for results extraction, the inbox importer, the checklist and the IR collector.

The XBRL fixtures are SYNTHETIC: they follow the structure of a real NSE integrated-filing
instance (in-capmkt taxonomy, INR in absolute rupees, quarter + year contexts) but the
numbers are made up. The PDF fixtures are page texts laid out like HDFC Bank's results,
including its OCR slips, again with made-up numbers.
"""

import datetime as dt
from pathlib import Path

import httpx
import pandas as pd
import pytest
from sqlalchemy import Engine

import collectors.result_files as rf
import collectors.results_ir as ir
import processing.results as res
from config.loader import load_watchlist
from storage import db

STOCKS = load_watchlist()


def xbrl(
    symbol: str = "INFY",
    end: str = "2026-06-30",
    start: str = "2026-04-01",
    nature: str = "Standalone",
    revenue: float = 399_570_000_000,
    profit: float = 72_490_000_000,
    eps: float = 17.87,
    extra: str = "",
    year_revenue: float | None = None,
) -> bytes:
    """A synthetic in-capmkt-style XBRL instance for one quarter."""
    year = ""
    if year_revenue is not None:
        year = (
            '<xbrli:context id="FourD"><xbrli:entity><xbrli:identifier scheme="x">1'
            "</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2025-04-01"
            f"</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>"
            f'<in-capmkt:RevenueFromOperations contextRef="FourD" unitRef="INR" decimals="-5">'
            f"{year_revenue:.0f}</in-capmkt:RevenueFromOperations>"
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
  xmlns:in-capmkt="http://example.com/in-capmkt" xmlns:iso4217="http://www.xbrl.org/2003/iso4217"
  xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
 <xbrli:context id="OneD"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
  </xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>
  <xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
 <xbrli:context id="OneI"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
  </xbrli:entity><xbrli:period><xbrli:instant>{end}</xbrli:instant></xbrli:period></xbrli:context>
 <xbrli:context id="Seg"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>
  <xbrli:segment><xbrldi:explicitMember dimension="a">b</xbrldi:explicitMember></xbrli:segment>
  </xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>
  <xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>
 {year}
 <xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
 <xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:INR
  </xbrli:measure></xbrli:unitNumerator><xbrli:unitDenominator><xbrli:measure>xbrli:shares
  </xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
 <in-capmkt:Symbol contextRef="OneD">{symbol}</in-capmkt:Symbol>
 <in-capmkt:NameOfTheCompany contextRef="OneD">Infosys Limited</in-capmkt:NameOfTheCompany>
 <in-capmkt:DateOfStartOfReportingPeriod contextRef="OneD">{start}</in-capmkt:DateOfStartOfReportingPeriod>
 <in-capmkt:DateOfEndOfReportingPeriod contextRef="OneD">{end}</in-capmkt:DateOfEndOfReportingPeriod>
 <in-capmkt:NatureOfReportStandaloneConsolidated contextRef="OneD">{nature}</in-capmkt:NatureOfReportStandaloneConsolidated>
 <in-capmkt:DateOfBoardMeetingWhenFinancialResultsWereApproved contextRef="OneD">2026-07-23</in-capmkt:DateOfBoardMeetingWhenFinancialResultsWereApproved>
 <in-capmkt:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-5">{revenue:.0f}</in-capmkt:RevenueFromOperations>
 <in-capmkt:RevenueFromOperations contextRef="Seg" unitRef="INR" decimals="-5">1</in-capmkt:RevenueFromOperations>
 <in-capmkt:ProfitLossForPeriod contextRef="OneD" unitRef="INR" decimals="-5">{profit:.0f}</in-capmkt:ProfitLossForPeriod>
 <in-capmkt:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations contextRef="OneD" unitRef="INRPerShare" decimals="2">{eps}</in-capmkt:BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations>
 {extra}
</xbrli:xbrl>""".encode()  # noqa: E501


BANK_PAGE = """HDFC BANK LIMITED
CIN: L65920MH1994PLC080618
UNAUDITED STANDALONE FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026
(in crore)
Particulars 30.06.2026 31.03.2026 30.06.2025 31.03.2026
Interest eamed (a)+(b)+(c)+(d) 70000.50 69000.00 65000.00 270000.00
3 Total Income (1)+(2) 80000 25 79000.00 76000.00 310000.00
4 Interest expended 40000,50 39000.00 38000.00 150000.00
14 Net Profit for the period (12)-(13) 15000.10 14800.00 14000.00 58000.00
(a) Basic EPS before & after extraordinary items (net of tax 10.10 9.90 9.40 38.00
(a) Gross NPAS 30000.00 29000.00 28000.00 29000.00
(b) Net NPAS 9000.00 8800.00 8500.00 8800.00
(c) % of Gross NPAs to Gross Advances 1.20% 1.18% 1.30% 1.18%
(d) % of Net NPAs to Net Advances 0.40% 0.39% 0.42% 0.39%"""
BANK_CONSOLIDATED = """UNAUDITED CONSOLIDATED FINANCIAL RESULTS FOR THE QUARTER ENDED JUNE 30, 2026
in crore)
1 Interest earned (a)+(b)+(c)+(d) 75000.00 74000.00 70000.00 290000.00
4 Interest expended 42000.00 41000.00 40000.00 160000.00
14 Net profit for the period before minority interest (12)-(13) 16500.00 16000.00 15000.00 60000.00
16 Net profit for the period (14)-(15) 15800.00 15500.00 14500.00 58500.00
(a) Basic EPS before & after extraordinary items 10.40 10.20 9.60 39.00"""


# --- basics ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("end", "label"),
    [((2026, 6, 30), "FY27Q1"), ((2026, 3, 31), "FY26Q4"), ((2025, 12, 31), "FY26Q3"),
     ((2025, 9, 30), "FY26Q2")],
)  # fmt: skip
def test_fiscal_quarter(end, label) -> None:
    assert res.fiscal_quarter(dt.date(*end)) == label


def test_fiscal_quarter_rejects_non_quarter_end() -> None:
    with pytest.raises(ValueError):
        res.fiscal_quarter(dt.date(2026, 5, 31))


# --- XBRL --------------------------------------------------------------------------


def test_xbrl_values_are_normalised_to_crore() -> None:
    p = res.parse_xbrl(xbrl())
    assert (p.period_end, p.basis, p.symbol_hint) == (dt.date(2026, 6, 30), "standalone", "INFY")
    assert p.filed_on == dt.date(2026, 7, 23)
    assert p.values["revenue"] == (pytest.approx(39957.0), "INR crore")
    assert p.values["net_profit"] == (pytest.approx(7249.0), "INR crore")
    assert p.values["eps"] == (17.87, "INR/share")
    assert p.values["x:RevenueFromOperations"][0] == pytest.approx(39957.0)
    assert "x:Symbol" not in p.values  # text facts aren't numeric metrics


def test_xbrl_ignores_dimensional_contexts() -> None:
    p = res.parse_xbrl(xbrl())
    assert p.values["revenue"][0] != pytest.approx(1 / 1e7)


def test_q4_xbrl_picks_the_quarter_not_the_year() -> None:
    content = xbrl(end="2026-03-31", start="2026-01-01", revenue=4e11, year_revenue=1.6e12)
    p = res.parse_xbrl(content)
    assert p.values["revenue"][0] == pytest.approx(40000.0)  # not 160000 (full year)


def test_consolidated_basis_and_owner_profit_preferred() -> None:
    extra = (
        '<in-capmkt:ProfitOrLossAttributableToOwnersOfParent contextRef="OneD" unitRef="INR">'
        "70000000000</in-capmkt:ProfitOrLossAttributableToOwnersOfParent>"
    )
    p = res.parse_xbrl(xbrl(nature="Consolidated", extra=extra))
    assert p.basis == "consolidated"
    assert p.values["net_profit"][0] == pytest.approx(7000.0)


def test_non_xbrl_xml_is_rejected() -> None:
    with pytest.raises(res.ResultParseError, match="not an XBRL instance"):
        res.parse_xbrl(b"<?xml version='1.0'?><rss></rss>")
    with pytest.raises(res.ResultParseError, match="not valid XML"):
        res.parse_xbrl(b"<<<")


# --- PDF ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "numbers"),
    [
        ("Operating expenses 18187 49 18477.53 17433.84", [18187.49, 18477.53, 17433.84]),
        ("Deposits 3170830,09 2764089.02 3105250,48", [3170830.09, 2764089.02, 3105250.48]),
        ("e) Unallocated (589.46) (591.22) (581.38 (2364.43)",
         [-589.46, -591.22, -581.38, -2364.43]),
        ("Total 1,049.19 7,67,70,39,761", [1049.19, 7677039761.0]),
        ("% of Gross NPAs 1.17% 1.15%", [1.17, 1.15]),
    ],
)  # fmt: skip
def test_ocr_number_repair(line: str, numbers: list[float]) -> None:
    assert res._numbers(line) == pytest.approx(numbers)


def test_bank_pdf_headline_rows() -> None:
    [standalone, consolidated] = res.parse_results_pdf([BANK_PAGE, BANK_CONSOLIDATED], STOCKS)
    assert (standalone.company, standalone.basis, standalone.period_end) == (
        "HDFCBANK", "standalone", dt.date(2026, 6, 30))  # fmt: skip
    v = {k: val for k, (val, _) in standalone.values.items()}
    assert v["interest_earned"] == 70000.50  # "Interest eamed" OCR slip still matched
    assert v["total_income"] == 80000.25  # "80000 25" repaired
    assert v["interest_expended"] == 40000.50  # "40000,50" repaired
    assert v["nii"] == pytest.approx(30000.0)
    assert (v["net_profit"], v["eps"], v["gross_npa_pct"], v["net_npa_pct"]) == (
        15000.10, 10.10, 1.20, 0.40)  # fmt: skip
    assert standalone.values["gross_npa_pct"][1] == "%"
    # consolidated: profit after minority interest, not before
    assert consolidated.values["net_profit"][0] == 15800.00


def test_pdf_units_convert_lakhs_to_crore() -> None:
    page = BANK_PAGE.replace("(in crore)", "(₹ in lakhs)")
    [p] = res.parse_results_pdf([page], STOCKS)
    assert p.values["net_profit"][0] == pytest.approx(150.001)  # 15000.10 lakh
    assert p.values["eps"][0] == 10.10  # per-share values aren't scaled


def test_pdf_without_unit_line_is_skipped() -> None:
    with pytest.raises(res.ResultParseError, match="no readable rows"):
        res.parse_results_pdf([BANK_PAGE.replace("(in crore)", "")], STOCKS)


def test_pdf_for_unknown_company_is_rejected() -> None:
    with pytest.raises(res.ResultParseError, match="no watchlist company"):
        res.parse_results_pdf([BANK_PAGE.replace("HDFC BANK LIMITED", "ACME LIMITED")], STOCKS)


def test_scanned_pdf_without_text_is_rejected(monkeypatch, tmp_path: Path) -> None:
    class FakePdf:
        pages = [type("P", (), {"extract_text": lambda self: None})()]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(res.pdfplumber, "open", lambda path: FakePdf())
    with pytest.raises(res.ResultParseError, match="no text layer"):
        res.pdf_pages(tmp_path / "scan.pdf")


# --- rows, changes, validation -------------------------------------------------------


def parsed(kind: str, end: tuple, basis: str = "standalone", **values) -> res.ParsedResult:
    p = res.ParsedResult(kind, dt.date(*end), basis)
    p.values = {k: (v, "INR crore") for k, v in values.items()}
    return p


NOW = dt.datetime(2026, 9, 23, tzinfo=dt.UTC)


def test_xbrl_beats_pdf_for_the_same_quarter_and_basis() -> None:
    rows = res.build_rows(
        [
            ("INFY", "f-pdf", parsed("pdf", (2026, 6, 30), revenue=1.0, net_profit=9.0)),
            ("INFY", "f-xbrl", parsed("xbrl", (2026, 6, 30), revenue=2.0)),
            ("INFY", "f-pdf2", parsed("pdf", (2026, 3, 31), revenue=3.0)),
        ],
        NOW,
    )
    by = {(r["period_end"], r["metric"]): r for r in rows}
    assert by[(dt.date(2026, 6, 30), "revenue")]["source"] == "xbrl"
    assert (dt.date(2026, 6, 30), "net_profit") not in by  # PDF dropped entirely
    assert by[(dt.date(2026, 3, 31), "revenue")]["trust"] == "low"


def quarters(metric: str, values: list[float], symbol: str = "INFY") -> list[dict]:
    ends = [(2025, 3, 31), (2025, 6, 30), (2025, 9, 30), (2025, 12, 31), (2026, 3, 31)]
    triples = [(symbol, f"f{i}", parsed("xbrl", e, **{metric: v}))
               for i, (e, v) in enumerate(zip(ends, values, strict=False))]  # fmt: skip
    return res.build_rows(triples, NOW)


def test_qoq_and_yoy_changes() -> None:
    diff = res.changes(pd.DataFrame(quarters("revenue", [100, 110, 121, 130, 150])))
    last = diff.iloc[-1]
    assert last["fiscal_quarter"] == "FY26Q4"
    assert last["qoq"] == pytest.approx(150 / 130 - 1)
    assert last["yoy"] == pytest.approx(150 / 100 - 1)
    assert pd.isna(diff.iloc[0]["qoq"])


def test_validation_flags_5x_jumps_and_sign_changes() -> None:
    rows = res.validate(quarters("revenue", [100, 105, 1050, -1100, 120]))
    flags = {r["fiscal_quarter"]: r["flag"] for r in rows}
    assert flags["FY25Q4"] is None and flags["FY26Q1"] is None
    assert "10.0x" in flags["FY26Q2"]  # a lakh/crore slip looks like this
    assert "should stay positive" in flags["FY26Q3"]
    profit = {
        r["fiscal_quarter"]: r["flag"]
        for r in res.validate(quarters("net_profit", [50, 55, -20, 30, 32]))
    }
    assert "check" in profit["FY26Q2"] and "check" in profit["FY26Q3"]
    assert profit["FY26Q4"] is None


# --- inbox importer ----------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(rf, "FILINGS_DIR", tmp_path / "filings")
    monkeypatch.setattr("utils.retry.time.sleep", lambda _s: None)
    return engine


def test_import_identifies_files_by_content_not_name(engine: Engine, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "download (3).xml").write_bytes(xbrl())
    (inbox / "whatever.bin").write_bytes(xbrl(nature="Consolidated", revenue=4.2e11))
    outcomes = rf.import_inbox(STOCKS, inbox)
    assert sorted(o.detail for o in outcomes) == [
        "INFY FY27Q1 consolidated",
        "INFY FY27Q1 standalone",
    ]
    assert all(o.status == "imported" for o in outcomes)
    stored = list((tmp_path / "filings" / "INFY").iterdir())
    assert len(stored) == 2 and all(p.suffix == ".xml" for p in stored)
    assert not any(inbox.iterdir()) or all(p.is_dir() for p in inbox.iterdir())

    res.rebuild(STOCKS)
    r = db.read_results()
    assert set(r["basis"]) == {"standalone", "consolidated"}
    assert set(r["source"]) == {"xbrl"}


def test_duplicates_and_rejects_are_moved_with_reasons(engine: Engine, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "a.xml").write_bytes(xbrl())
    rf.import_inbox(STOCKS, inbox)
    (inbox / "a-again.xml").write_bytes(xbrl())
    (inbox / "ixbrl.html").write_bytes(b"<!DOCTYPE html><html><body>iXBRL</body></html>")
    (inbox / "other.xml").write_bytes(xbrl(symbol="ACME").replace(b"Infosys Limited", b"Acme Ltd"))
    (inbox / "annual.xml").write_bytes(xbrl(end="2026-05-31", start="2026-03-01"))
    outcomes = {o.name: o for o in rf.import_inbox(STOCKS, inbox)}

    assert outcomes["a-again.xml"].status == "duplicate"
    assert (inbox / "duplicates" / "a-again.xml").exists()
    assert "download the 'XBRL' (.xml)" in outcomes["ixbrl.html"].detail
    assert "not in the watchlist" in outcomes["other.xml"].detail
    assert "not a quarter end" in outcomes["annual.xml"].detail
    reason = (inbox / "rejected" / "other.xml.reason.txt").read_text()
    assert "ACME" in reason


def test_pre_demerger_symbol_maps_to_tmpv(engine: Engine) -> None:
    p = res.parse_xbrl(xbrl(symbol="TATAMOTORS", end="2024-12-31", start="2024-10-01"))
    assert rf._stock_for_xbrl(p, STOCKS).symbol == "TMPV"


# --- checklist ---------------------------------------------------------------------


def test_last_quarters_only_includes_quarters_whose_results_are_due() -> None:
    ends = rf.last_quarters(dt.date(2026, 9, 23))
    assert ends[0] == dt.date(2026, 6, 30) and ends[-1] == dt.date(2024, 9, 30)
    assert rf.last_quarters(dt.date(2026, 8, 10))[0] == dt.date(2026, 3, 31)  # Q1 not due yet


def test_checklist_statuses_and_download_pages(engine: Engine) -> None:
    rows = res.build_rows(
        [("INFY", "f1", parsed("xbrl", (2026, 6, 30), revenue=1.0)),
         ("INFY", "f2", parsed("pdf", (2026, 3, 31), revenue=1.0))],
        NOW,
    )  # fmt: skip
    db.replace_results(rows)
    table = rf.checklist(STOCKS, dt.date(2026, 9, 23)).set_index(["symbol", "quarter", "basis"])
    assert len(table) == 5 * 8 * 2
    assert table.loc[("INFY", "FY27Q1", "standalone"), "status"] == "ok (XBRL)"
    assert table.loc[("INFY", "FY27Q1", "standalone"), "download_from"] == ""
    assert table.loc[("INFY", "FY26Q4", "standalone"), "status"].startswith("PDF only")
    missing = table.loc[("TCS", "FY25Q3", "consolidated")]
    assert missing["status"] == "MISSING"
    assert "corporate-filings-financial-results?symbol=TCS" in missing["download_from"]
    assert "integrated-filing" in table.loc[("TCS", "FY26Q1", "standalone"), "download_from"]


# --- IR collector ------------------------------------------------------------------

IR_PAGE = """<a href="/pdf/financial-results/2026-2027/quarter-1/financial-results-for-the-quarter-ended-june-30-2026.pdf">r</a>
<a href="/pdf/financial-results/2021-2022/quarter-1/financial-results-for-quarter-ended-june-30--2021.pdf">r</a>
<a href="/pdf/financial-results/2025-2026/quarter-4/financial-results-for-the-quarter-and-year-ended-March-31-2026.pdf">r</a>
<a href="/pdf/financial-results/2026-2027/quarter-1/press-release-june-2026.pdf">p</a>
<a href="/pdf/financial-results/2026-2027/quarter-1/key-parameters-financial-results-for-the-quarter-ended-june-30-2026.pdf">k</a>
<a href="/pdf/financial-results/2008-2009/june/financial-results-for-the-quarter-ended-june-2008.pdf">old</a>"""  # noqa: E501


def test_ir_links_match_results_pdfs_only() -> None:
    links = ir.result_links(IR_PAGE, "https://bank.example/ir", ir.IR_SOURCES["HDFCBANK"]["pdf"])
    names = [link.rsplit("/", 1)[-1] for link in links]
    assert len(names) == 4
    assert not any("press-release" in n or "key-parameters" in n for n in names)
    today = dt.date(2026, 9, 23)
    assert [ir.recent_enough(link, today) for link in links].count(False) == 1  # the 2008 one


def test_ir_collector_skips_dead_links_and_non_pdfs(engine: Engine, monkeypatch) -> None:
    served = {
        "/ir": httpx.Response(200, text=IR_PAGE),
        "/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
    }
    names = [
        "financial-results-for-the-quarter-ended-june-30-2026.pdf",
        "financial-results-for-quarter-ended-june-30--2021.pdf",
        "financial-results-for-the-quarter-and-year-ended-March-31-2026.pdf",
    ]
    bodies = [httpx.Response(200, content=b"%PDF-1.7 fake"), httpx.Response(404),
              httpx.Response(200, text="<!DOCTYPE html>error")]  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in served:
            return served[path]
        return bodies[next(i for i, n in enumerate(names) if path.endswith(n))]

    good = res.ParsedResult("pdf", dt.date(2026, 6, 30), "standalone", company="HDFCBANK")
    good.values = {"net_profit": (1.0, "INR crore")}
    monkeypatch.setattr(ir, "pdf_pages", lambda path: ["text"])
    monkeypatch.setattr(ir, "parse_results_pdf", lambda pages, stocks: [good])
    monkeypatch.setitem(ir.IR_SOURCES["HDFCBANK"], "page", "https://bank.example/ir")
    real_limiter = ir.DomainRateLimiter
    monkeypatch.setattr(
        ir, "DomainRateLimiter", lambda interval: real_limiter(interval, sleep=lambda _s: None)
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert ir.collect_all(STOCKS, client=client) == {}
    assert len(db.read_result_files()) == 1  # only the real PDF was stored
    assert ir.collect_all(STOCKS, client=client) == {}  # rerun: known URL, nothing new
    assert len(db.read_result_files()) == 1
