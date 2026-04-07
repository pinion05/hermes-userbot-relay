# hermes-userbot-relay

Telegram bot-to-bot message relay using Pyrogram userbot.

When Telegram bots mention each other in group chats, the target bot cannot receive the message (Telegram API limitation). This relay solves that by running a userbot that detects @mentions in bot messages and resends them as user account messages.

## How It Works

```
[hermes bot] ──"@iris review this"──→ Group
                                         ↓ (iris can't see bot messages)
                                    [Userbot] ← reads via MTProto
                                         ↓ (resends as user)
                                    Group ──"@irispinion_bot\nto irispinion_bot\nfrom hermesspinion_bot\nreview this"
                                                              ↓
                                                        [iris bot] receives ✅
```

## Setup

1. Install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install pyrogram tgcrypto python-dotenv
```

2. Get API credentials from https://my.telegram.org → API development tools

3. Configure `.env`:
```
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

4. First run (requires interactive auth):
```bash
python relay.py
```
Enter your phone number and Telegram confirmation code. Session file will be saved.

5. Run as systemd service:
```bash
cp hermes-userbot-relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-userbot-relay
```

## Relay Format

When a bot mentions another bot, the relay resends:

```
@<target_bot>
to <target_bot>
from <sender_bot>
<message body>

@​<sender_bot> 로 멘션하여 답장할 수 있습니다.
```

## Configuration

Edit `KNOWN_BOT_USERNAMES` in `relay.py` to add more bots that should trigger relay:

```python
KNOWN_BOT_USERNAMES = {
    "irispinion_bot",
    "hermesspinion_bot",
    "your_other_bot",
}
```

## License

MIT
