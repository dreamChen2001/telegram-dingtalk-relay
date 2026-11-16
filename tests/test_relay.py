from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx

from relay import (
    PublicChannelMessage,
    SupabaseStateStore,
    dingtalk_signed_url,
    format_dingtalk_content,
    parse_public_preview_page,
    truncate_text,
)


class RelayFormattingTests(unittest.TestCase):
    def test_signature_preserves_access_token_and_uses_expected_hmac(self) -> None:
        secret = "SEC_test_secret"
        timestamp = 1_700_000_000_000
        url = dingtalk_signed_url(
            "https://oapi.dingtalk.com/robot/send?access_token=placeholder",
            secret,
            timestamp_ms=timestamp,
        )

        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["access_token"], ["placeholder"])
        self.assertEqual(query["timestamp"], [str(timestamp)])
        expected = base64.b64encode(
            hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(query["sign"], [expected])

    def test_text_message_includes_channel_time_and_permalink(self) -> None:
        message = PublicChannelMessage(
            id=42,
            raw_text="first line\nsecond line",
            date=datetime(2026, 11, 16, 3, 4, 5, tzinfo=timezone.utc),
        )
        rendered = format_dingtalk_content(message, "heyue151")
        self.assertIn("频道：@heyue151", rendered)
        self.assertIn("2026-11-16 11:04:05 CST", rendered)
        self.assertIn("first line\nsecond line", rendered)
        self.assertIn("https://t.me/heyue151/42", rendered)

    def test_media_message_has_a_safe_label_and_long_text_is_truncated(self) -> None:
        message = PublicChannelMessage(
            id=43,
            raw_text="x" * 5_000,
            date=datetime(2026, 11, 16, tzinfo=timezone.utc),
            media_label="📷 图片",
        )
        rendered = format_dingtalk_content(message, "heyue151")
        self.assertIn("📷 图片", rendered)
        self.assertIn("[内容已截断]", rendered)
        self.assertLessEqual(len(rendered), 3_500)

    def test_control_characters_are_removed(self) -> None:
        self.assertEqual(truncate_text("safe\x00text\nnext"), "safetext\nnext")

    def test_public_preview_parser_reads_only_expected_channel_posts(self) -> None:
        html = """
        <div class="tgme_widget_message" data-post="heyue151/101">
          <div class="tgme_widget_message_text">alpha<br>beta</div>
          <a class="tgme_widget_message_date"><time datetime="2026-11-16T03:04:05+00:00"></time></a>
        </div>
        <div class="tgme_widget_message" data-post="heyue151/102">
          <a class="tgme_widget_message_photo_wrap"></a>
        </div>
        <div class="tgme_widget_message" data-post="another_channel/103">
          <div class="tgme_widget_message_text">must be ignored</div>
        </div>
        """
        messages = parse_public_preview_page(html, "heyue151")
        self.assertEqual([message.id for message in messages], [101, 102])
        self.assertEqual(messages[0].raw_text, "alpha\nbeta")
        self.assertEqual(messages[0].date, datetime(2026, 11, 16, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(messages[1].media_label, "📷 图片")


class SupabaseHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_secret_key_only_uses_apikey_header(self) -> None:
        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(request.headers)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            store = SupabaseStateStore(http, "https://example.supabase.co", "sb_secret_example")
            self.assertIsNone(await store.get_state("telegram:example"))

        self.assertEqual(received_headers["apikey"], "sb_secret_example")
        self.assertNotIn("authorization", received_headers)

    async def test_legacy_service_role_key_keeps_compatibility_header(self) -> None:
        received_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.update(request.headers)
            return httpx.Response(200, json=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            store = SupabaseStateStore(http, "https://example.supabase.co", "legacy-jwt-key")
            self.assertIsNone(await store.get_state("telegram:example"))

        self.assertEqual(received_headers["authorization"], "Bearer legacy-jwt-key")


if __name__ == "__main__":
    unittest.main()
