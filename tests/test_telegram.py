"""Tests for Telegram delivery (mocked HTTP; no network, tmp database only)."""

import datetime as dt
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine

import delivery.telegram as tg
from config.loader import Stock
from storage import db
from utils.logging_setup import RedactSecretsFilter, redact

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnop"
CHAT = "4242"
INFY = Stock("INFY", "INFY.NS", "Infosys Ltd", "IT", ("Infosys",))
DAY = dt.date(2026, 9, 24)


class FakeTelegram:
    """Records sendMessage calls; responses can be queued."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.responses: list[httpx.Response] = []
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.responses:
            return self.responses.pop(0)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getUpdates":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "date": 1790000000,
                                "text": "hi bot",
                                "chat": {"id": 4242, "type": "private", "first_name": "Kartikey"},
                            },
                        }
                    ],
                },
            )
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})


def bot(fake: FakeTelegram, sleeps: list | None = None) -> tg.TelegramBot:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return tg.TelegramBot(TOKEN, client, sleep=(sleeps if sleeps is not None else []).append)


# --- secrets -----------------------------------------------------------------------


def test_errors_never_contain_the_token() -> None:
    fake = FakeTelegram()
    fake.responses = [httpx.Response(401, json={"ok": False, "description": "Unauthorized"})]
    with pytest.raises(tg.TelegramError) as info:
        bot(fake).send(CHAT, "hello")
    assert TOKEN not in str(info.value) and "Unauthorized" in str(info.value)
    # No chained exception (which would carry the URL) is shown with it.
    assert info.value.__cause__ is None
    assert info.value.__context__ is None or info.value.__suppress_context__


def test_network_errors_are_redacted_and_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    b = tg.TelegramBot(
        TOKEN, httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None
    )
    with pytest.raises(tg.TelegramError) as info:
        b.send(CHAT, "hello")
    assert TOKEN not in str(info.value)


def test_log_records_are_redacted_including_tracebacks() -> None:
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed")
    except RuntimeError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "url %s", (f"bot{TOKEN}",), sys.exc_info()
        )
    RedactSecretsFilter().filter(record)
    formatted = logging.Formatter().format(record)
    assert TOKEN not in formatted and "<redacted-token>" in formatted
    assert redact("no secrets here 12:34") == "no secrets here 12:34"


# --- API behaviour -----------------------------------------------------------------


def test_rate_limit_waits_retry_after() -> None:
    fake, sleeps = FakeTelegram(), []
    fake.responses = [httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3}})]
    bot(fake, sleeps).send(CHAT, "hello")
    assert 3.0 in sleeps and len(fake.sent) == 1


def test_long_messages_are_split_under_the_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(100))
    chunks = tg.split_message(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks) and "\n".join(chunks) == text
    assert tg.split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]


def test_find_chat_reads_updates() -> None:
    [chat] = tg.chats_from_updates(bot(FakeTelegram()).get_updates())
    assert (chat["id"], chat["type"], chat["name"], chat["last_text"]) == (
        4242,
        "private",
        "Kartikey",
        "hi bot",
    )


def test_placeholders_count_as_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(tg, "load_dotenv", lambda path: None)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "your-telegram-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    assert not tg.load_settings().configured
    assert tg.deliver_day(DAY, [INFY], []) is None


# --- delivery ----------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = db.create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    return engine


def add_alert(subject: str, severity: str, text: str, day: dt.date = DAY) -> None:
    db.insert_new_alerts(
        [
            {
                "symbol": "INFY",
                "alert_type": "price_move",
                "subject": subject,
                "alert_date": day,
                "severity": severity,
                "text": text,
                "created_at": dt.datetime.now(dt.UTC),
                "sent_at": None,
            }
        ]
    )


def test_high_alerts_go_alone_digest_once_and_nothing_is_sent_twice(engine: Engine) -> None:
    add_alert("a", "high", "INFY results imported.")
    add_alert("b", "normal", "INFY rose 4.0% on 24 Sep.")
    add_alert("old", "high", "INFY yesterday's high alert.", DAY - dt.timedelta(days=1))
    fake = FakeTelegram()
    result = tg.deliver(bot(fake), CHAT, DAY, [INFY], [])
    texts = [m["text"] for m in fake.sent]
    assert result.alerts_sent == 2 and result.digest_sent
    assert texts[0].startswith("[!] stock-intel 23 Sep 2026") and texts[1].endswith(
        "results imported."
    )
    assert "stock-intel digest for Thu 24 Sep 2026" in texts[2] and "INFY rose 4.0%" in texts[2]
    assert all(m["chat_id"] == CHAT for m in fake.sent)
    assert db.unsent_alerts(DAY).empty  # everything marked sent

    fake.sent.clear()
    tg.deliver(bot(fake), CHAT, DAY, [INFY], [])  # a re-run: only the digest again
    assert len(fake.sent) == 1 and fake.sent[0]["text"].startswith("stock-intel digest")


def test_failure_notice_when_alerts_were_not_built(engine: Engine) -> None:
    fake = FakeTelegram()
    result = tg.deliver(bot(fake), CHAT, DAY, [INFY], ["news: sentiment", "alerts"])
    assert result.failure_notice
    assert "the update had failures: news: sentiment, alerts." in fake.sent[0]["text"]


def test_a_failed_send_leaves_the_alert_unsent(engine: Engine) -> None:
    add_alert("a", "high", "INFY results imported.")
    fake = FakeTelegram()
    fake.responses = [httpx.Response(400, json={"ok": False, "description": "chat not found"})]
    with pytest.raises(tg.TelegramError):
        tg.deliver(bot(fake), CHAT, DAY, [INFY], [])
    assert len(db.unsent_alerts(DAY)) == 1  # will be retried next run
