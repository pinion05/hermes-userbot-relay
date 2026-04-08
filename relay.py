#!/usr/bin/env python3
"""
Hermes Userbot Relay
Telegram bot-to-bot message relay for group chats.

Listens for bot messages that @mention other bots,
then reformats and resends as a user account message
so the target bot can actually receive it.

Relay format:
    @irispinion_bot
    to hermes
    <original body>

Stability features:
    - on_disconnect handler: explicit reconnection on transport disconnect
    - Watchdog: detects "zombie connections" where TCP is alive but
      no updates arrive, and forces a reconnect
    - Proper exception handling with crash-on-unrecoverable
    - Reduced logging noise in production
"""

import asyncio
import os
import re
import signal
import logging
import time
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.types import Message

RELAY_DIR = Path(__file__).parent

load_dotenv(RELAY_DIR / ".env")

# Known bot usernames that should trigger relay
KNOWN_BOT_USERNAMES = {
    "irispinion_bot",
    "hermesspinion_bot",
}

MENTION_RE = re.compile(r'@(\w+)')
RELAY_FORMAT_RE = re.compile(r'^@\w+\nto \w+', re.MULTILINE)

# Watchdog: if no updates of any kind arrive in this many seconds,
# force a reconnect. Telegram's server can silently stop sending
# updates while keeping the TCP connection alive ("zombie connection").
WATCHDOG_TIMEOUT = 300  # 5 minutes

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# Suppress noisy pyrogram internals
logging.getLogger("pyrogram.session.session").setLevel(logging.WARNING)
logging.getLogger("pyrogram.connection.connection").setLevel(logging.WARNING)

log = logging.getLogger("relay")


def get_sender_name(message: Message) -> str:
    """Get the sender's telegram username or first name."""
    if not message.from_user:
        return "unknown"
    return message.from_user.username or message.from_user.first_name or "unknown"


def extract_target_and_body(text: str, entities=None):
    """Find @mention of a known bot in text or entities, return (target_username, body)."""
    # First try text-based @mentions
    mentions = MENTION_RE.findall(text)
    for mention in mentions:
        if mention in KNOWN_BOT_USERNAMES:
            body = MENTION_RE.sub('', text).strip()
            return mention, body

    # Fallback: check entities for mention type (MessageEntityType.MENTION)
    if entities:
        for entity in entities:
            entity_type = getattr(entity, 'type', None)
            if entity_type is None:
                continue
            type_name = entity_type.value if hasattr(entity_type, 'value') else str(entity_type)
            if type_name in ('mention', 'text_mention'):
                try:
                    entity_text = text[entity.offset:entity.offset + entity.length]
                except (AttributeError, IndexError):
                    continue
                if entity_text.startswith('@'):
                    username = entity_text[1:]
                else:
                    username = entity_text
                if username in KNOWN_BOT_USERNAMES:
                    body = text.strip()
                    return username, body

    return None, None


app = Client(
    str(RELAY_DIR / "hermes_relay"),
    api_id=int(os.environ.get("TELEGRAM_API_ID", "0")),
    api_hash=os.environ.get("TELEGRAM_API_HASH", ""),
)

# Watchdog state
_last_update_time = time.monotonic()
_watchdog_task = None


async def watchdog_loop():
    """
    Periodically check if we're receiving updates.
    If no updates arrive for WATCHDOG_TIMEOUT seconds, the connection
    is likely a "zombie" — TCP alive but update stream dead.
    Force a reconnect by stopping and restarting the client session.
    """
    global _last_update_time
    while True:
        await asyncio.sleep(60)  # Check every minute
        now = time.monotonic()
        stale_seconds = now - _last_update_time
        if stale_seconds > WATCHDOG_TIMEOUT:
            log.warning(
                "Watchdog: no updates for %d seconds (threshold: %d). "
                "Forcing reconnect to clear zombie connection.",
                stale_seconds, WATCHDOG_TIMEOUT,
            )
            try:
                # Restart the MTProto session at the transport level.
                # We do NOT use app.restart() because it calls terminate() which
                # clears the dispatcher groups, losing all decorator-registered
                # handlers (on_message, on_raw_update, on_disconnect).
                # Instead, restart only app.session which creates a new TCP
                # connection and recv_worker while keeping the dispatcher intact.
                await app.session.stop()
                await app.session.start()
                # After session restart, re-sync update state with Telegram
                # so we get the missed updates
                from pyrogram import raw
                await app.invoke(raw.functions.updates.GetState())
                _last_update_time = time.monotonic()
                log.info("Watchdog: session reconnect successful, updates should resume.")
            except Exception as e:
                log.error("Watchdog: session reconnect failed: %s", e)
                # Let systemd handle it via Restart=on-failure
                raise


@app.on_raw_update()
async def on_raw_update(client, update, users, chats):
    """Track any incoming update for the watchdog."""
    global _last_update_time
    _last_update_time = time.monotonic()


@app.on_message(filters.group & filters.bot)
async def on_bot_message(client: Client, message: Message):
    text = message.text or message.caption or ""
    sender = get_sender_name(message)
    log.debug("Bot message from %s | text: %.200s", sender, text)
    if not text:
        return

    # skip reasoning-only messages (no content after stripping reasoning)
    # Normalize multiple blank lines to single blank line first,
    # then strip reasoning block greedily up to the last blank line.
    text = re.sub(r'\n[ \t]*\n', '\n\n', text)
    text = re.sub(
        r'💭\s*Reasoning:\s*\n[\s\S]*\n\n(?=[^\n])',
        '', text
    ).strip()
    # If no trailing blank line, reasoning goes to end — strip it entirely
    text = re.sub(r'💭\s*Reasoning:\s*\n[\s\S]*', '', text).strip()
    cleaned = text
    # also clean up leading empty lines
    cleaned = re.sub(r'^\s+', '', cleaned)
    if not cleaned:
        return
    text = cleaned

    # skip already-relayed messages
    if RELAY_FORMAT_RE.search(text):
        return

    target_username, body = extract_target_and_body(text, message.entities)
    if not target_username:
        log.debug("No known bot mention found in: %.80s", text)
        return
    if not body:
        body = "(empty)"

    sender_name = get_sender_name(message)
    relay_text = (
        f"@{target_username}\n"
        f"to {target_username}\n"
        f"from {sender_name}\n"
        f"{body}\n\n"
        f"@\u200b{sender_name} 로 멘션하여 답장할 수 있습니다."
    )

    log.info("Relay: %s -> @%s | %.80s", sender_name, target_username, body)

    try:
        await client.send_message(
            chat_id=message.chat.id,
            text=relay_text,
        )
    except Exception as e:
        log.error("Failed to send relay message: %s", e)


@app.on_disconnect()
async def on_disconnect(client):
    """
    Called when Pyrogram's session detects a transport-level disconnect.
    Pyrogram's session.py calls this from session.stop() when the recv_worker
    exits (TCP close or transport error). After this callback, Pyrogram
    automatically calls session.restart() which does stop() + start().

    However, the auto-restart can fail silently (e.g., auth key issues,
    persistent network problems). We log it for observability.
    """
    log.warning("Connection lost. Pyrogram will attempt auto-reconnect.")
    _last_update_time = time.monotonic()


async def main():
    log.info("Hermes Userbot Relay starting...")
    global _watchdog_task

    await app.start()
    me = await app.get_me()
    log.info("Logged in as %s (ID: %s)", me.first_name, me.id)
    log.info("Listening for bot messages in groups...")
    log.info("Watchdog timeout: %d seconds", WATCHDOG_TIMEOUT)

    _last_update_time = time.monotonic()
    _watchdog_task = asyncio.create_task(watchdog_loop())

    # Graceful shutdown on SIGTERM/SIGINT
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
    await app.stop()


if __name__ == "__main__":
    app.run(main())
