"""Deliver alerts and the daily digest to Telegram via the Bot API (plain HTTPS).

Settings come from .env: TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID (the
chat to post to; find it with --find-chat after sending the bot a message).

After each daily run (run_update.py), `deliver`:
1. sends every unsent high-severity alert on its own, marking each sent_at as soon as it's
   delivered (so a retry never repeats one);
2. sends a short failure notice if the alert step itself failed (then no pipeline alert
   exists to carry the failures);
3. sends the day's digest once, then marks that day's remaining alerts as sent.

The token is part of every API URL, so it never goes into logs or error messages: errors
are re-raised as TelegramError with the token redacted and the original exception
dropped, and utils.logging_setup redacts anything token-shaped. Requests have a timeout,
retry network errors and 5xx with backoff, honour 429 retry_after once, and are spaced at
least MIN_INTERVAL_S apart (Telegram allows about one message per second per chat).

Run with:  uv run python -m delivery.telegram [--find-chat | --test | --digest [--date D]]
"""

import argparse
import datetime as dt
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

from config.loader import Stock, load_watchlist
from config.market_calendar import latest_completed_session, load_holidays
from processing.alerts import digest
from storage.db import (
    PROJECT_ROOT,
    init_db,
    mark_alerts_sent,
    read_alerts,
    read_pipeline_runs,
    unsent_alerts,
)
from utils import setup_logging
from utils.logging_setup import redact
from utils.retry import retry

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT_S = 15
MIN_INTERVAL_S = 1.1
MAX_RETRY_AFTER_S = 60
MAX_MESSAGE_CHARS = 4000  # Telegram's limit is 4096


class TelegramError(RuntimeError):
    """A Telegram request failed. The message never contains the token."""


class _Retryable(RuntimeError):
    """Network error or 5xx: worth retrying."""


@dataclass(frozen=True)
class TelegramSettings:
    """Bot token and chat id from .env (either may be missing)."""

    token: str | None
    chat_id: str | None

    @property
    def configured(self) -> bool:
        """True if both the token and the chat id are set."""
        return bool(self.token and self.chat_id)


def load_settings() -> TelegramSettings:
    """TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env / the environment."""
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None
    placeholders = {"your-telegram-bot-token", "your-telegram-chat-id"}
    return TelegramSettings(
        None if token in placeholders else token, None if chat in placeholders else chat
    )


class TelegramBot:
    """Minimal Bot API client: getUpdates and sendMessage."""

    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._token = token
        self._client = client or httpx.Client(timeout=TIMEOUT_S)
        self._sleep = sleep
        self._last_sent: float | None = None

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        """POST a Bot API method; returns its `result`. Raises TelegramError (redacted)."""
        try:
            return self._post(method, payload)
        except TelegramError:
            raise
        except Exception as exc:
            raise TelegramError(redact(f"{method} failed: {type(exc).__name__}: {exc}")) from None

    @retry(attempts=3, base_delay=2.0, exceptions=(_Retryable,))
    def _post(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"{API}/bot{self._token}/{method}", json=payload)
        except httpx.TransportError as exc:
            raise _Retryable(redact(str(exc))) from None
        if response.status_code >= 500:
            raise _Retryable(f"{method}: HTTP {response.status_code}")
        body = response.json() if response.content else {}
        if response.status_code == 429:
            wait = (body.get("parameters") or {}).get("retry_after", 5)
            if wait > MAX_RETRY_AFTER_S:
                raise TelegramError(f"{method}: rate limited for {wait}s")
            logger.warning("Telegram rate limit: waiting %ss", wait)
            self._sleep(float(wait))
            response = self._client.post(f"{API}/bot{self._token}/{method}", json=payload)
            body = response.json() if response.content else {}
        if not body.get("ok"):
            raise TelegramError(
                redact(f"{method}: HTTP {response.status_code}: {body.get('description', '')}")
            )
        return body["result"]

    def get_updates(self) -> list[dict[str, Any]]:
        """Recent updates (messages sent to the bot)."""
        return self._call("getUpdates", {"timeout": 0})

    def send(self, chat_id: str, text: str) -> None:
        """Send `text` as plain text, split into chunks under Telegram's length limit."""
        for chunk in split_message(text):
            if self._last_sent is not None:
                wait = MIN_INTERVAL_S - (time.monotonic() - self._last_sent)
                if wait > 0:
                    self._sleep(wait)
            self._call(
                "sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            )
            self._last_sent = time.monotonic()


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line breaks into chunks of at most `limit` characters."""
    chunks, current = [], ""
    for line in text.splitlines():
        while len(line) > limit:  # a single overlong line
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            candidate = line
        current = candidate
    if current:
        chunks.append(current)
    return chunks


def chats_from_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct chats that messaged the bot: id, type, name, last message and its date."""
    chats: dict[int, dict[str, Any]] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            continue
        name = chat.get("title") or " ".join(
            p for p in (chat.get("first_name"), chat.get("last_name")) if p
        )
        chats[chat["id"]] = {
            "id": chat["id"],
            "type": chat.get("type"),
            "name": name,
            "username": chat.get("username"),
            "last_text": (message.get("text") or "")[:80],
            "date": dt.datetime.fromtimestamp(message.get("date", 0), dt.UTC),
        }
    return list(chats.values())


# --- delivery ----------------------------------------------------------------------


@dataclass
class DeliveryResult:
    """What a delivery pass sent."""

    alerts_sent: int = 0
    failure_notice: bool = False
    digest_sent: bool = False


def alert_message(row: Any) -> str:
    """One high-severity alert as a Telegram message."""
    return f"[!] stock-intel {row.alert_date:%d %b %Y}\n{row.text}"


def deliver(
    bot: TelegramBot,
    chat_id: str,
    day: dt.date,
    stocks: list[Stock],
    failed: list[str],
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> DeliveryResult:
    """Send unsent high alerts, a failure notice if alerts weren't built, and the digest."""
    result = DeliveryResult()
    for row in unsent_alerts(day, "high").itertuples(index=False):
        bot.send(chat_id, alert_message(row))
        mark_alerts_sent([(row.symbol, row.alert_type, row.subject)], now())
        result.alerts_sent += 1
    if "alerts" in failed:
        steps = ", ".join(failed)
        bot.send(chat_id, f"[!] stock-intel {day:%d %b %Y}: the update had failures: {steps}.")
        result.failure_notice = True
    todays = read_alerts(day, day)
    bot.send(chat_id, digest(day, stocks, todays, read_pipeline_runs()))
    result.digest_sent = True
    rest = unsent_alerts(day)
    mark_alerts_sent(
        list(zip(rest["symbol"], rest["alert_type"], rest["subject"], strict=True)), now()
    )
    return result


def deliver_day(day: dt.date, stocks: list[Stock], failed: list[str]) -> DeliveryResult | None:
    """deliver() with settings from .env. None (and a warning) if Telegram isn't configured.

    Raises:
        TelegramError: if sending fails (the caller falls back to the macOS notification).
    """
    settings = load_settings()
    if not settings.configured:
        logger.warning(
            "Telegram isn't configured (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID); nothing sent"
        )
        return None
    result = deliver(TelegramBot(settings.token), settings.chat_id, day, stocks, failed)
    logger.info(
        "Telegram: %d alert(s)%s and the digest sent",
        result.alerts_sent,
        " plus a failure notice" if result.failure_notice else "",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    """Entry point: --find-chat lists chats that messaged the bot; --test sends a test
    message; --digest sends that day's digest and unsent alerts."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--find-chat", action="store_true", help="list chats from getUpdates")
    group.add_argument("--test", action="store_true", help="send a test message")
    group.add_argument("--digest", action="store_true", help="deliver alerts and the digest")
    parser.add_argument("--date", type=dt.date.fromisoformat, help="digest date (default: latest)")
    args = parser.parse_args(argv)
    setup_logging()
    settings = load_settings()
    if not settings.token:
        logger.error("TELEGRAM_BOT_TOKEN isn't set in .env")
        return 1
    bot = TelegramBot(settings.token)
    if args.find_chat:
        chats = chats_from_updates(bot.get_updates())
        if not chats:
            print("No messages found. Send the bot a message in Telegram, then run this again.")
        for c in chats:
            print(
                f"chat id {c['id']} ({c['type']}): {c['name']}"
                f"{' @' + c['username'] if c['username'] else ''}, last message "
                f"{c['date']:%Y-%m-%d %H:%M} UTC: {c['last_text']!r}"
            )
        return 0
    if not settings.chat_id:
        logger.error("TELEGRAM_CHAT_ID isn't set in .env (find it with --find-chat)")
        return 1
    if args.test:
        bot.send(
            settings.chat_id,
            "stock-intel: test message. Alerts and the daily digest "
            "will arrive here after each scheduled update.",
        )
        logger.info("Test message sent")
        return 0
    init_db()
    day = args.date or latest_completed_session(dt.datetime.now(dt.UTC), load_holidays())
    deliver(bot, settings.chat_id, day, load_watchlist(), [])
    return 0


if __name__ == "__main__":
    sys.exit(main())
