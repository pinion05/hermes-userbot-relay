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
"""

import os
import re
import logging
from pathlib import Path
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram import idle

RELAY_DIR = Path(__file__).parent

load_dotenv(RELAY_DIR / ".env")

# Known bot usernames that should trigger relay
KNOWN_BOT_USERNAMES = {
    "irispinion_bot",
    "hermesspinion_bot",
}

MENTION_RE = re.compile(r'@(\w+)')
RELAY_FORMAT_RE = re.compile(r'^@\w+\nto \w+', re.MULTILINE)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.DEBUG,
)
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
    # Pyrogram MessageEntity has no .text attribute; extract from text using offset/length
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


@app.on_message(filters.group & filters.bot)
async def on_bot_message(client: Client, message: Message):
    text = message.text or message.caption or ""
    sender = get_sender_name(message)
    log.debug("Bot message from %s | text: %.200s | entities: %s", sender, text, [(e.type, getattr(e, 'text', '')) for e in (message.entities or [])])
    if not text:
        return

    # skip reasoning-only messages (no content after stripping reasoning)
    # Reasoning block: starts with 💭 Reasoning:, consume GREEDILY until
    # the LAST blank line (or end of text), so internal \n\n in reasoning
    # doesn't cause premature cutoff.
    text = re.sub(
        r'💭\s*Reasoning:\s*\n[\s\S]*\n\s*\n(?=[^\n])',
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
    relay_text = f"@{target_username}\nto {target_username}\nfrom {sender_name}\n{body}\n\n@\u200b{sender_name} 로 멘션하여 답장할 수 있습니다."

    log.info("Relay: %s -> @%s | %.80s", sender_name, target_username, body)

    await client.send_message(
        chat_id=message.chat.id,
        text=relay_text,
    )


async def main():
    log.info("Hermes Userbot Relay starting...")
    await app.start()
    me = await app.get_me()
    log.info("Logged in as %s (ID: %s)", me.first_name, me.id)
    log.info("Listening for bot messages in groups...")
    await idle()


if __name__ == "__main__":
    app.run(main())
