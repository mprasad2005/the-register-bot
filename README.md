# Telegram Poll Management Bot

## Setup
1. Copy `.env.example` to `.env`.
2. Put BotFather token in `BOT_TOKEN`.
3. Put your Telegram numeric user ID in `ADMIN_IDS`.
4. Put the owner Telegram user ID in `OWNER_ID`. If omitted, the smallest ID in `ADMIN_IDS` is used as the owner.
5. Install: `pip install -r requirements.txt`
6. Run: `python main.py`

## Manage Admins
The owner can open **Admin Panel -> Manage Admins** to add or remove admins by numeric Telegram user ID. Changes are stored in the bot database and remain after restart. The owner cannot be removed from the panel.

## Groups
The three initial groups are placeholders in `config.py`. Replace their numeric IDs with the real Telegram chat IDs.

The bot must be added to each target group as an administrator with permission to post messages/create polls.

## Add Group
Any user can open the bot in a private chat, use `/addgroup`, and send either:
`-1001234567890`
or
`@groupusername`

Only the Telegram owner of the group can add it. The bot verifies the sender's `creator` status before saving the group, so the bot must be an administrator in that group to perform the verification. The same action is available from the authorized admin panel, but being a bot admin does not bypass the group-owner check. Adding a group does not grant bot admin permissions.

Adding a group does not require the bot to be an administrator. The bot automatically remembers groups where it is added or where it receives a group message, so those groups do not need to be manually added for announcements. Announcements require permission to send messages, and messages are pinned only when the bot has permission to pin messages. The bot also sends announcements when polls start and finish.

The bot uses Telegram button styles: blue `primary`, green `success`, and red `danger`. Telegram controls the exact shades based on the user's app theme.

## Create Poll
Admin -> Create Poll -> select group -> question -> comma-separated options.

Registration stays open while poll status is `registration`. Run:
`/close POLL_ID`
to close registration and post the poll.

## Important Telegram limitation
Telegram's native poll does not provide a built-in mechanism to restrict voting to the users registered in this bot. This version therefore records registrations separately and posts a normal Telegram poll after registration closes. If you need strict "registered users only can vote", the voting layer must be implemented as bot-based inline-button voting instead of a native Telegram poll.
