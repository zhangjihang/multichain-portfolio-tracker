"""Discord webhook sender for posting reports and alerts."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# Discord message limit is 2000 characters
MAX_MESSAGE_LEN = 2000

# Sentinel inserted by formatters to mark logical section boundaries.
# send_webhook splits on this first so code blocks stay intact per message.
MSG_BREAK = "<<<MSG_BREAK>>>"


def _split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split a long message into chunks that fit Discord's limit.

    Tries to split at newlines to keep formatting intact.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find last newline within limit
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _split_into_messages(content: str) -> list[str]:
    """Split content into messages.

    First split on the MSG_BREAK sentinel (logical sections),
    then size-split any oversized section to fit Discord's limit.
    """
    # Split on sentinel (with surrounding newlines consumed)
    sections: list[str] = []
    for raw in content.split(MSG_BREAK):
        s = raw.strip("\n")
        if s:
            sections.append(s)
    if not sections:
        sections = [content]

    messages: list[str] = []
    for section in sections:
        messages.extend(_split_message(section))
    return messages


async def send_webhook(webhook_url: str, content: str, username: str | None = None) -> bool:
    """Send a message to a Discord webhook.

    Splits on MSG_BREAK sentinels (logical sections) first, then on size.
    Returns True if all messages sent successfully.
    """
    if not webhook_url:
        logger.error("No webhook URL provided")
        return False

    messages = _split_into_messages(content)
    payload_base: dict = {}
    if username:
        payload_base["username"] = username

    ok = True
    async with httpx.AsyncClient(timeout=15.0) as client:
        for i, msg in enumerate(messages):
            if i > 0:
                # Small delay to preserve message ordering on Discord's side
                await asyncio.sleep(0.3)
            payload = {**payload_base, "content": msg}
            try:
                resp = await client.post(webhook_url, json=payload)
                if resp.status_code == 204:
                    continue
                resp.raise_for_status()
            except Exception:
                logger.exception("Failed to send webhook message")
                ok = False
    return ok
