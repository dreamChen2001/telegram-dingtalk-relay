#!/usr/bin/env python3
"""Relay posts from a public Telegram channel preview to a DingTalk robot.

The listener uses Telegram's public ``https://t.me/s/<channel>`` preview page,
not a bot token or logged-in Telegram session. It is intended for the public
channel configured for this project and short-lived GitHub Actions runs.
Supabase stores only cursors and delivery state; post bodies and credentials
are never stored there.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup


LOG = logging.getLogger("telegram_dingtalk_relay")
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_DINGTALK_TEXT_LENGTH = 3_500
PUBLIC_PREVIEW_USER_AGENT = "Mozilla/5.0 (compatible; TelegramDingTalkRelay/1.0)"


class RelayError(RuntimeError):
    """An error that should cause the current scheduled run to fail."""


class DingTalkDeliveryError(RelayError):
    """A safe-to-log DingTalk delivery error."""


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RelayError(f"Missing required environment variable: {name}")
    return value


def integer_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RelayError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RelayError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    tg_channel: str
    dingtalk_webhook_url: str
    dingtalk_signing_secret: str
    supabase_url: str
    supabase_secret_key: str
    initial_sync_mode: str
    initial_history_limit: int
    max_messages_per_run: int
    public_preview_max_pages: int

    @classmethod
    def from_environment(cls) -> "Config":
        channel = require_env("TG_CHANNEL").lstrip("@").strip()
        if not channel or "/" in channel or " " in channel:
            raise RelayError("TG_CHANNEL must be a public Telegram-channel username without @")

        initial_sync_mode = os.environ.get("INITIAL_SYNC_MODE", "skip").strip().lower() or "skip"
        if initial_sync_mode not in {"skip", "send_recent"}:
            raise RelayError("INITIAL_SYNC_MODE must be either skip or send_recent")

        return cls(
            tg_channel=channel,
            dingtalk_webhook_url=require_env("DINGTALK_WEBHOOK_URL"),
            dingtalk_signing_secret=require_env("DINGTALK_SIGNING_SECRET"),
            supabase_url=require_env("SUPABASE_URL").rstrip("/"),
            supabase_secret_key=(
                os.environ.get("SUPABASE_SECRET_KEY", "").strip()
                or require_env("SUPABASE_SERVICE_ROLE_KEY")
            ),
            initial_sync_mode=initial_sync_mode,
            initial_history_limit=integer_env("INITIAL_HISTORY_LIMIT", 10),
            max_messages_per_run=integer_env("MAX_MESSAGES_PER_RUN", 100),
            public_preview_max_pages=integer_env("PUBLIC_PREVIEW_MAX_PAGES", 20),
        )

    @property
    def relay_key(self) -> str:
        return f"telegram-public:{self.tg_channel.lower()}"


@dataclass(frozen=True)
class RelayState:
    last_message_id: int


@dataclass(frozen=True)
class PublicChannelMessage:
    id: int
    raw_text: str
    date: datetime | None
    media_label: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dingtalk_signed_url(webhook_url: str, signing_secret: str, timestamp_ms: int | None = None) -> str:
    """Build a signed DingTalk custom-robot URL without logging it."""

    timestamp = timestamp_ms if timestamp_ms is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    string_to_sign = f"{timestamp}\n{signing_secret}".encode("utf-8")
    digest = hmac.new(signing_secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("utf-8")
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}{urlencode({'timestamp': str(timestamp), 'sign': signature})}"


def truncate_text(value: str, max_length: int = MAX_DINGTALK_TEXT_LENGTH) -> str:
    cleaned = "".join(character for character in value if character == "\n" or ord(character) >= 32)
    if len(cleaned) <= max_length:
        return cleaned
    return f"{cleaned[: max_length - 18].rstrip()}\n\n[内容已截断]"


def format_dingtalk_content(message: PublicChannelMessage, channel_username: str) -> str:
    """Use plain text so a channel post cannot inject DingTalk Markdown."""

    body = "\n".join(part for part in (message.media_label, message.raw_text.strip()) if part) or "[无文字内容]"
    if message.date is None:
        displayed_time = "未知"
    else:
        message_date = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
        displayed_time = message_date.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %Z")

    content = (
        "Telegram 频道更新\n"
        f"频道：@{channel_username}\n"
        f"时间：{displayed_time}\n\n"
        f"{body}\n\n"
        f"原文：https://t.me/{channel_username}/{message.id}"
    )
    return truncate_text(content)


def parse_telegram_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def detect_media_label(node: Any) -> str | None:
    if node.select_one(".tgme_widget_message_photo_wrap"):
        return "📷 图片"
    if node.select_one(".tgme_widget_message_video_player"):
        return "🎬 视频"
    if node.select_one(".tgme_widget_message_document_wrap"):
        return "📎 文件"
    if node.select_one(".tgme_widget_message_voice"):
        return "🎤 语音"
    return None


def parse_public_preview_page(html: str, expected_channel: str) -> list[PublicChannelMessage]:
    """Extract the currently displayed posts from Telegram's public preview HTML."""

    soup = BeautifulSoup(html, "html.parser")
    parsed: dict[int, PublicChannelMessage] = {}
    for node in soup.select("[data-post]"):
        data_post = str(node.get("data-post", ""))
        try:
            post_channel, raw_id = data_post.rsplit("/", 1)
            message_id = int(raw_id)
        except (ValueError, AttributeError):
            continue
        if post_channel.lstrip("@").lower() != expected_channel.lstrip("@").lower() or message_id <= 0:
            continue

        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""
        time_node = node.select_one(".tgme_widget_message_date time[datetime]")
        parsed[message_id] = PublicChannelMessage(
            id=message_id,
            raw_text=text,
            date=parse_telegram_datetime(time_node.get("datetime") if time_node else None),
            media_label=detect_media_label(node),
        )
    return [parsed[message_id] for message_id in sorted(parsed)]


class SupabaseStateStore:
    """A minimal PostgREST client for the two tables in the supplied migration."""

    def __init__(self, http: httpx.AsyncClient, project_url: str, secret_key: str) -> None:
        self.http = http
        self.base_url = f"{project_url.rstrip('/')}/rest/v1"
        self.headers = {"apikey": secret_key, "Content-Type": "application/json"}
        # New sb_secret keys are opaque, not JWTs, and must only use apikey.
        # The legacy service_role JWT retains Authorization compatibility.
        if not secret_key.startswith("sb_"):
            self.headers["Authorization"] = f"Bearer {secret_key}"

    async def _request(self, method: str, resource: str, **kwargs: Any) -> httpx.Response:
        request_headers = {**self.headers, **kwargs.pop("headers", {})}
        try:
            response = await self.http.request(method, f"{self.base_url}/{resource}", headers=request_headers, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            # Do not include URL/body: either can contain sensitive data.
            raise RelayError(f"Supabase {method} {resource} request failed") from exc

    async def get_state(self, relay_key: str) -> RelayState | None:
        response = await self._request(
            "GET", "tg_relay_state", params={"relay_key": f"eq.{relay_key}", "select": "last_message_id"}
        )
        rows = response.json()
        return RelayState(last_message_id=int(rows[0]["last_message_id"])) if rows else None

    async def initialise_state(self, relay_key: str, last_message_id: int) -> RelayState:
        await self._request(
            "POST",
            "tg_relay_state?on_conflict=relay_key",
            headers={**self.headers, "Prefer": "resolution=ignore-duplicates"},
            json={
                "relay_key": relay_key,
                "last_message_id": last_message_id,
                "initialized_at": utc_now(),
                "last_run_at": utc_now(),
            },
        )
        state = await self.get_state(relay_key)
        if state is None:
            raise RelayError("Supabase did not return an initial relay state")
        return state

    async def mark_heartbeat(self, relay_key: str) -> None:
        await self._request("PATCH", "tg_relay_state", params={"relay_key": f"eq.{relay_key}"}, json={"last_run_at": utc_now()})

    async def advance_cursor(self, relay_key: str, message_id: int) -> None:
        await self._request(
            "PATCH",
            "tg_relay_state",
            params={"relay_key": f"eq.{relay_key}"},
            json={"last_message_id": message_id, "last_success_at": utc_now(), "last_run_at": utc_now()},
        )

    async def _get_delivery(self, relay_key: str, message_id: int) -> dict[str, Any] | None:
        response = await self._request(
            "GET",
            "tg_relay_delivery",
            params={
                "relay_key": f"eq.{relay_key}",
                "telegram_message_id": f"eq.{message_id}",
                "select": "status,attempts",
            },
        )
        rows = response.json()
        return rows[0] if rows else None

    async def begin_delivery(self, relay_key: str, message: PublicChannelMessage, source_url: str) -> bool:
        """Return False only when a matching post is already marked sent."""

        existing = await self._get_delivery(relay_key, message.id)
        if existing and existing.get("status") == "sent":
            return False
        if existing:
            await self._request(
                "PATCH",
                "tg_relay_delivery",
                params={"relay_key": f"eq.{relay_key}", "telegram_message_id": f"eq.{message.id}"},
                json={
                    "status": "pending",
                    "attempts": int(existing.get("attempts", 0)) + 1,
                    "last_error": None,
                    "updated_at": utc_now(),
                },
            )
            return True

        message_date = message.date
        if message_date and message_date.tzinfo is None:
            message_date = message_date.replace(tzinfo=timezone.utc)
        await self._request(
            "POST",
            "tg_relay_delivery",
            headers={**self.headers, "Prefer": "resolution=ignore-duplicates"},
            json={
                "relay_key": relay_key,
                "telegram_message_id": message.id,
                "telegram_date": message_date.astimezone(timezone.utc).isoformat() if message_date else None,
                "source_url": source_url,
                "status": "pending",
                "attempts": 1,
                "updated_at": utc_now(),
            },
        )
        return True

    async def mark_sent(self, relay_key: str, message_id: int) -> None:
        await self._request(
            "PATCH",
            "tg_relay_delivery",
            params={"relay_key": f"eq.{relay_key}", "telegram_message_id": f"eq.{message_id}"},
            json={"status": "sent", "sent_at": utc_now(), "last_error": None, "updated_at": utc_now()},
        )

    async def mark_failed(self, relay_key: str, message_id: int, error_label: str) -> None:
        await self._request(
            "PATCH",
            "tg_relay_delivery",
            params={"relay_key": f"eq.{relay_key}", "telegram_message_id": f"eq.{message_id}"},
            json={"status": "failed", "last_error": error_label[:500], "updated_at": utc_now()},
        )


class DingTalkNotifier:
    def __init__(self, http: httpx.AsyncClient, webhook_url: str, signing_secret: str) -> None:
        self.http = http
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret

    async def send_text(self, content: str) -> None:
        try:
            response = await self.http.post(
                dingtalk_signed_url(self.webhook_url, self.signing_secret),
                json={"msgtype": "text", "text": {"content": content}},
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DingTalkDeliveryError("DingTalk request failed") from exc
        if body.get("errcode", 0) != 0:
            raise DingTalkDeliveryError(f"DingTalk rejected the message (errcode={body.get('errcode')})")


async def send_with_retry(notifier: DingTalkNotifier, content: str) -> None:
    last_error: DingTalkDeliveryError | None = None
    for attempt in range(1, 4):
        try:
            await notifier.send_text(content)
            return
        except DingTalkDeliveryError as exc:
            last_error = exc
            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


async def fetch_preview_page(http: httpx.AsyncClient, channel: str, before: int | None = None) -> list[PublicChannelMessage]:
    params = {"before": str(before)} if before else None
    try:
        response = await http.get(f"https://t.me/s/{channel}", params=params, headers={"User-Agent": PUBLIC_PREVIEW_USER_AGENT})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RelayError("Telegram public-preview request failed") from exc
    messages = parse_public_preview_page(response.text, channel)
    if not messages:
        raise RelayError("Telegram public preview returned no readable posts; verify TG_CHANNEL is public and reachable")
    return messages


async def fetch_messages_after(
    http: httpx.AsyncClient, channel: str, after_id: int, max_pages: int
) -> list[PublicChannelMessage]:
    """Paginate backwards until the durable cursor is found, then return ascending new posts."""

    before: int | None = None
    collected: dict[int, PublicChannelMessage] = {}
    boundary_found = after_id == 0
    for _ in range(max_pages):
        page = await fetch_preview_page(http, channel, before)
        oldest_id = page[0].id
        for message in page:
            if message.id > after_id:
                collected[message.id] = message
        if after_id == 0 or oldest_id <= after_id:
            boundary_found = True
            break
        if before is not None and oldest_id >= before:
            raise RelayError("Telegram public-preview pagination did not advance")
        before = oldest_id

    if not boundary_found:
        raise RelayError("Telegram backlog exceeds PUBLIC_PREVIEW_MAX_PAGES; no cursor was advanced")
    return [collected[message_id] for message_id in sorted(collected)]


async def relay_messages(
    config: Config,
    store: SupabaseStateStore,
    notifier: DingTalkNotifier,
    messages: Iterable[PublicChannelMessage],
) -> int:
    delivered_count = 0
    for message in messages:
        source_url = f"https://t.me/{config.tg_channel}/{message.id}"
        should_send = await store.begin_delivery(config.relay_key, message, source_url)
        if should_send:
            try:
                await send_with_retry(notifier, format_dingtalk_content(message, config.tg_channel))
            except DingTalkDeliveryError as exc:
                await store.mark_failed(config.relay_key, message.id, type(exc).__name__)
                raise
            await store.mark_sent(config.relay_key, message.id)
            delivered_count += 1
        await store.advance_cursor(config.relay_key, message.id)
    return delivered_count


async def run_relay() -> None:
    config = Config.from_environment()
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        store = SupabaseStateStore(http, config.supabase_url, config.supabase_secret_key)
        notifier = DingTalkNotifier(http, config.dingtalk_webhook_url, config.dingtalk_signing_secret)
        state = await store.get_state(config.relay_key)
        if state is None:
            current_page = await fetch_preview_page(http, config.tg_channel)
            if config.initial_sync_mode == "skip":
                await store.initialise_state(config.relay_key, current_page[-1].id)
                await store.mark_heartbeat(config.relay_key)
                LOG.info("Initialised cursor without forwarding historical posts")
                return
            await store.initialise_state(config.relay_key, 0)
            messages = current_page[-config.initial_history_limit :]
        else:
            # Await the pagination coroutine before limiting its result.  Without
            # these parentheses, Python tries to slice the coroutine object
            # first, which only affects scheduled (post-initialisation) runs.
            messages = (
                await fetch_messages_after(
                    http, config.tg_channel, state.last_message_id, config.public_preview_max_pages
                )
            )[: config.max_messages_per_run]

        delivered_count = await relay_messages(config, store, notifier, messages)
        await store.mark_heartbeat(config.relay_key)
        LOG.info("Run completed; delivered %d new message(s)", delivered_count)


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    # A DingTalk Webhook URL is a credential. Do not allow dependency loggers
    # to write full request URLs into public GitHub Actions logs.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    try:
        asyncio.run(run_relay())
    except RelayError as exc:
        LOG.error("Relay failed: %s", exc)
        return 1
    except Exception as exc:
        LOG.error("Relay failed unexpectedly (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
