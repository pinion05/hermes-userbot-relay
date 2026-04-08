#!/usr/bin/env python3
"""
Hermes Userbot Relay (Telethon)
Telegram bot-to-bot message relay for group chats.

Listens for bot messages that @mention other bots,
then reformats and resends as a user account message
so the target bot can actually receive it.

Relay format:
    @irispinion_bot
    to irispinion_bot
    from hermes
    <original body>

    @​hermesspinion_bot 로 멘션하여 답장할 수 있습니다.

Stability features:
    - Watchdog: detects stale event loops and reconnects
    - Graceful shutdown on SIGTERM/SIGINT
"""

import asyncio
import os
import re
import signal
import logging
import time
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

RELAY_DIR = Path(__file__).parent

load_dotenv(RELAY_DIR / ".env")

API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# Known bot usernames that should trigger relay
KNOWN_BOT_USERNAMES = {
    "irispinion_bot",
    "hermesspinion_bot",
}

MENTION_RE = re.compile(r'@(\w+)')
RELAY_FORMAT_RE = re.compile(r'^@\w+\nto \w+', re.MULTILINE)

# Watchdog: if no events arrive in this many seconds, reconnect
WATCHDOG_TIMEOUT = 300  # 5 minutes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("relay")


def get_sender_name(event) -> str:
    """Get the sender's telegram username or first name."""
    sender = event.sender
    if not sender:
        return "unknown"
    if isinstance(sender, User):
        return sender.username or sender.first_name or "unknown"
    return getattr(sender, 'title', 'unknown')


def extract_target_and_body(text: str, entities=None):
    """Find @mention of a known bot in text or entities, return (target_username, body)."""
    # First try text-based @mentions
    mentions = MENTION_RE.findall(text)
    for mention in mentions:
        if mention in KNOWN_BOT_USERNAMES:
            body = MENTION_RE.sub('', text).strip()
            return mention, body

    # Fallback: check entities for MessageEntityMention
    if entities:
        for entity in entities:
            if hasattr(entity, 'offset') and hasattr(entity, 'length'):
                try:
                    entity_text = text[entity.offset:entity.offset + entity.length]
                except (IndexError, TypeError):
                    continue
                if entity_text.startswith('@'):
                    username = entity_text[1:]
                    if username in KNOWN_BOT_USERNAMES:
                        body = text.strip()
                        return username, body

    return None, None


def strip_reasoning(text: str) -> str:
    """Remove 💭 Reasoning: blocks from text, handling internal blank lines."""
    # Normalize multiple blank lines to single blank line
    text = re.sub(r'\n[ \t]*\n', '\n\n', text)
    # Greedy: match reasoning block up to the last blank line before content
    text = re.sub(
        r'💭\s*Reasoning:\s*\n[\s\S]*\n\n(?=[^\n])',
        '', text
    ).strip()
    # If no trailing blank line, reasoning goes to end — strip entirely
    text = re.sub(r'💭\s*Reasoning:\s*\n[\s\S]*', '', text).strip()
    # Clean leading empty lines
    text = re.sub(r'^\s+', '', text)
    return text


# Watchdog state
_last_event_time = time.monotonic()
_watchdog_task = None


async def watchdog_loop(client: TelegramClient):
    """Detect zombie connections and force reconnect."""
    global _last_event_time
    while True:
        await asyncio.sleep(60)
        now = time.monotonic()
        stale_seconds = now - _last_event_time
        if stale_seconds > WATCHDOG_TIMEOUT:
            log.warning(
                "Watchdog: no events for %d seconds (threshold: %d). "
                "Forcing reconnect.",
                stale_seconds, WATCHDOG_TIMEOUT,
            )
            try:
                await client.disconnect()
                await client.connect()
                _last_event_time = time.monotonic()
                log.info("Watchdog: reconnect successful.")
            except Exception as e:
                log.error("Watchdog: reconnect failed: %s", e)
                raise


async def on_bot_message(event):
    """Handle bot messages in groups with @mentions."""
    global _last_event_time
    _last_event_time = time.monotonic()

    # Only group/supergroup chats
    chat = event.chat
    if not isinstance(chat, (Channel, Chat)):
        return

    # Only messages from bots
    sender = event.sender
    if not isinstance(sender, User) or not sender.bot:
        return

    text = event.text or event.message.caption or ""
    if not text:
        return

    # Strip reasoning block
    text = strip_reasoning(text)
    if not text:
        return

    # Skip already-relayed messages
    if RELAY_FORMAT_RE.search(text):
        return

    # Extract target mention and body
    entities = event.message.entities
    target_username, body = extract_target_and_body(text, entities)
    if not target_username:
        return
    if not body:
        body = "(empty)"

    sender_name = get_sender_name(event)
    relay_text = (
        f"@{target_username}\n"
        f"to {target_username}\n"
        f"from {sender_name}\n"
        f"{body}\n\n"
        f"@\u200b{sender_name} 로 멘션하여 답장할 수 있습니다."
    )

    log.info("Relay: %s -> @%s | %.80s", sender_name, target_username, body)

    try:
        await event.client.send_message(
            entity=event.chat_id,
            message=relay_text,
        )
    except Exception as e:
        log.error("Failed to send relay message: %s", e)


async def main():
    log.info("Hermes Userbot Relay starting (Telethon)...")

    client = TelegramClient(
        str(RELAY_DIR / "hermes_relay_telemthon"),
        API_ID,
        API_HASH,
    )

    # Register event handlers before start
    client.add_event_handler(on_bot_message, events.NewMessage(incoming=True))

    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s (ID: %s)", me.first_name, me.id)
    log.info("Listening for bot messages in groups...")
    log.info("Watchdog timeout: %d seconds", WATCHDOG_TIMEOUT)

    _last_event_time = time.monotonic()
    _watchdog_task = asyncio.create_task(watchdog_loop(client))

    # Graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Received shutdown signal.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    # Cleanup
    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await _watchdog_task
        except asyncio.CancelledError:
            pass

    log.info("Shutting down...")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
