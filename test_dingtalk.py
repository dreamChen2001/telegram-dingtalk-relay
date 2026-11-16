#!/usr/bin/env python3
"""Send one explicit configuration-test message to the configured DingTalk robot."""

from __future__ import annotations

import asyncio
import os

import httpx

from relay import DingTalkNotifier, RelayError, require_env


async def main() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False) as http:
        notifier = DingTalkNotifier(
            http,
            require_env("DINGTALK_WEBHOOK_URL"),
            require_env("DINGTALK_SIGNING_SECRET"),
        )
        await notifier.send_text("Telegram → DingTalk relay configuration test. No Telegram content was sent.")
    print("DingTalk test delivered.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RelayError as exc:
        raise SystemExit(f"Test failed: {exc}") from exc
