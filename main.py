import json
import logging
import asyncio
import random
import re
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, ReplyKeyboardRemove
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PollAnswerHandler, ChatMemberHandler, ContextTypes, filters
)

import database as db
from config import BOT_TOKEN, ADMIN_IDS, OWNER_ID

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
STATE = {}
ANNOUNCEMENT_DELAY = 0.1
PIN_PERMISSION_CACHE = {}


def is_admin(user_id):
    return user_id == OWNER_ID or db.is_admin(user_id)


def is_owner(user_id):
    return user_id == OWNER_ID


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Poll Registration", callback_data="reg", style="success")],
        [InlineKeyboardButton("📋 Poll Details", callback_data="details", style="primary")],
        [InlineKeyboardButton("🏆 Poll Results", callback_data="results", style="primary")],
        [InlineKeyboardButton("🎁 Giveaway", callback_data="giveaway", style="success")],
    ])


def admin_menu(owner=False):
    buttons = [
        [InlineKeyboardButton("📝 Poll Registration", callback_data="admin_poll", style="success")],
        [InlineKeyboardButton("🗳 Main Poll", callback_data="admin_mainpoll", style="success")],
        [InlineKeyboardButton("🛑 Finish Registration", callback_data="admin_finishreg", style="danger")],
        [InlineKeyboardButton("👥 Registrations", callback_data="admin_regs", style="primary")],
        [InlineKeyboardButton("📊 Valid Votes", callback_data="admin_valid", style="primary")],
        [InlineKeyboardButton("🏆 Poll Results", callback_data="admin_results", style="primary")],
        [InlineKeyboardButton("➕ Add Group", callback_data="admin_add", style="success"), InlineKeyboardButton("🗑 Remove Group", callback_data="admin_remove", style="danger")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats", style="primary")],
        [InlineKeyboardButton("📢 Announcement", callback_data="admin_announce", style="success")],
        [InlineKeyboardButton("🎲 Lucky Dip", callback_data="lucky_dip", style="success")],
        [InlineKeyboardButton("🎁 Giveaway", callback_data="admin_giveaway", style="success")],
    ]
    if owner:
        buttons.append([InlineKeyboardButton("👑 Manage Admins", callback_data="owner_admins", style="primary")])
        buttons.append([InlineKeyboardButton("📣 Admin Announcement", callback_data="owner_announce", style="success")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_main", style="primary")])
    return InlineKeyboardMarkup(buttons)


def group_keyboard(prefix, back_callback="back_main"):
    rows = db.get_groups()
    buttons = [[InlineKeyboardButton(r["title"], callback_data=f"{prefix}:{r['id']}", style="primary")] for r in rows]
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data=back_callback, style="primary")])
    return InlineKeyboardMarkup(buttons)


def single_back(callback_data="back_main", text="🔙 Back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=callback_data, style="primary")]])


def format_registration_names(poll_id):
    rows = db.registrations(poll_id)
    if not rows:
        return "None"
    return "\n".join(f"{index}. {row['name']}" for index, row in enumerate(rows, 1))


def giveaway_text(giveaway):
    text = f"🎁 *{giveaway['title']}*\n\n{giveaway['description']}"
    if giveaway["winner_text"]:
        text += f"\n\n🏆 *Result*\n{giveaway['winner_text']}"
    return text


async def send_giveaway_media(bot, chat_id, giveaway):
    if giveaway["winner_media_type"] == "photo" and giveaway["winner_media_id"]:
        await bot.send_photo(chat_id=chat_id, photo=giveaway["winner_media_id"], caption=giveaway["winner_caption"] or "🏆 Giveaway winner")


def announcement_recipients():
    group_ids = [row["id"] for row in db.get_groups()]
    user_ids = [row["user_id"] for row in db.get_users()]
    recipients = [(chat_id, 0) for chat_id in group_ids] + [(user_id, 1) for user_id in user_ids]
    return list(dict.fromkeys(recipients))


async def broadcast_worker(bot, job_id, queue_no):
    while True:
        delivery = db.claim_announcement_delivery(job_id, queue_no)
        if not delivery:
            return
        chat_id = delivery["chat_id"]
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=db.get_announcement_job(job_id)["source_chat_id"], message_id=db.get_announcement_job(job_id)["source_message_id"])
        except RetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            db.finish_announcement_delivery(job_id, chat_id, "pending", str(exc))
            continue
        except Forbidden as exc:
            db.finish_announcement_delivery(job_id, chat_id, "blocked", str(exc))
        except Exception as exc:
            db.finish_announcement_delivery(job_id, chat_id, "failed", str(exc))
        else:
            db.finish_announcement_delivery(job_id, chat_id, "sent")
        await asyncio.sleep(ANNOUNCEMENT_DELAY)


async def run_announcement(bot, job_id):
    db.start_announcement_job(job_id)
    await asyncio.gather(*(broadcast_worker(bot, job_id, queue_no) for queue_no in range(10)))
    db.complete_announcement_job(job_id)
    job = db.get_announcement_job(job_id)
    try:
        await bot.send_message(chat_id=job["created_by"], text=f"📢 Announcement completed\n\n👥 Total: {job['total']}\n✅ Sent: {job['sent']}\n❌ Failed: {job['failed']}\n🚫 Blocked/Unavailable: {job['blocked']}")
    except Exception:
        pass


async def pin_message(context, chat_id, message_id):
    if PIN_PERMISSION_CACHE.get(chat_id) is False:
        return
    try:
        can_pin = PIN_PERMISSION_CACHE.get(chat_id)
        if can_pin is None:
            bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            can_pin = bot_member.status == "administrator" and getattr(bot_member, "can_pin_messages", False)
            PIN_PERMISSION_CACHE[chat_id] = can_pin
        if can_pin:
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception as exc:
        PIN_PERMISSION_CACHE[chat_id] = False
        logging.warning("Could not pin message %s in group %s: %s", message_id, chat_id, exc)


async def send_group_message(context, chat_id, text, parse_mode=None):
    message = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    await pin_message(context, chat_id, message.message_id)
    return message


async def try_group_message(context, chat_id, text, parse_mode=None):
    try:
        return await send_group_message(context, chat_id, text, parse_mode=parse_mode)
    except Exception as exc:
        logging.warning("Could not send group announcement to %s: %s", chat_id, exc)
        return None


def parse_lucky_dip_names(text):
    names = []
    for line in text.splitlines():
        name = re.sub(r"^\s*\d+\s*[.)-]?\s*", "", line).strip()
        if name:
            names.append(name)
    if len(names) == 1 and "," in names[0]:
        names = [name.strip() for name in names[0].split(",") if name.strip()]
    return names


async def run_lucky_dip(bot, group_id, names):
    try:
        try:
            await bot.send_dice(chat_id=group_id, emoji="🎲")
        except Exception as exc:
            logging.warning("Could not send Lucky Dip dice to %s: %s", group_id, exc)

        try:
            await bot.send_message(
                chat_id=group_id,
                text="<blockquote>🎲✨ Winner announcement is coming soon.</blockquote>",
                parse_mode="HTML",
            )
        except Exception as exc:
            logging.warning("Could not send Lucky Dip intro to %s: %s", group_id, exc)

        countdown = await bot.send_message(
            chat_id=group_id,
            text="⏳✨ <b>00:30</b>  •  🎲 Winner announcing soon...",
            parse_mode="HTML",
        )
        for seconds in range(29, -1, -1):
            await asyncio.sleep(1)
            minutes, remaining_seconds = divmod(seconds, 60)
            try:
                await countdown.edit_text(
                    f"⏳✨ <b>{minutes:02d}:{remaining_seconds:02d}</b>  •  🎲 Winner announcing soon...",
                    parse_mode="HTML",
                )
            except Exception as exc:
                logging.warning("Could not update Lucky Dip timer in %s: %s", group_id, exc)

        winner = random.choice(names)
        await bot.send_message(
            chat_id=group_id,
            text=f"<blockquote>🏆✨ The winner is: {escape(winner)}</blockquote>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.exception("Lucky Dip failed in group %s: %s", group_id, exc)


async def verify_group_owner(context, chat_id, user_id):
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status == "creator"


async def track_group_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    member_update = update.my_chat_member
    if not chat or not member_update:
        return
    status = member_update.new_chat_member.status
    if status in ("member", "administrator"):
        db.add_group(chat.id, chat.title or str(chat.id), getattr(chat, "username", None))
    elif status in ("left", "kicked"):
        db.remove_group(chat.id)


async def track_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        db.add_group(chat.id, chat.title or str(chat.id), getattr(chat, "username", None))


def member_is_valid(member):
    status = getattr(member, "status", "")
    if status in ("creator", "administrator", "member"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def verify_poll_votes(context, poll, telegram_total=None):
    """Verify tracked voters against registration and current group membership."""
    votes = db.get_votes(poll["id"])
    options = json.loads(poll["options"])
    valid_counts = [0] * len(options)
    invalid = 0
    checked = 0
    registered_ids = {r["user_id"] for r in db.registrations(poll["id"])}

    for vote in votes:
        user_id = vote["user_id"]
        checked += 1
        valid = not poll["registration_required"] or user_id in registered_ids
        if valid:
            try:
                member = await context.bot.get_chat_member(chat_id=poll["group_id"], user_id=user_id)
                valid = member_is_valid(member)
            except Exception as exc:
                logging.warning("Membership check failed for poll %s user %s: %s", poll["id"], user_id, exc)
                valid = False

        try:
            option_ids = json.loads(vote["option_ids"])
        except Exception:
            option_ids = []

        if valid:
            for option_id in option_ids:
                if isinstance(option_id, int) and 0 <= option_id < len(valid_counts):
                    valid_counts[option_id] += 1
        else:
            invalid += 1

    if telegram_total is None:
        telegram_total = sum(valid_counts) + invalid
    db.save_poll_results(poll["id"], valid_counts, invalid, checked, telegram_total)
    return valid_counts, invalid, checked


def format_result(poll, result_row):
    options = json.loads(poll["options"])
    valid_counts = json.loads(result_row["valid_counts"])
    lines = [f"🏆 *Poll #{poll['id']}*", "", f"🗳️ {poll['question']}", ""]
    for i, option in enumerate(options):
        count = valid_counts[i] if i < len(valid_counts) else 0
        lines.append(f"{i + 1}. {option} — *{count} valid votes*")
    lines += [
        "",
        f"🗳️ Telegram total votes: {result_row['telegram_total']}",
        f"✅ Valid votes (registered + group member): {sum(valid_counts)}",
        f"❌ Invalid/non-member votes: {result_row['invalid_total']}",
        f"👤 Voters checked: {result_row['checked_total']}",
    ]
    if max(valid_counts, default=0) > 0:
        top = max(valid_counts)
        winners = [options[i] for i, c in enumerate(valid_counts) if c == top]
        lines += (["", f"🥇 *Winner: {winners[0]}* ({top})"] if len(winners) == 1 else ["", f"🤝 *Tie:* {', '.join(winners)} ({top} each)"])
    else:
        lines += ["", "🥇 No valid votes yet."]
    return "\n".join(lines)


def poll_admin_buttons(poll_id, back="admin_mainpoll"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Check Valid Votes", callback_data=f"checkvotes:{poll_id}", style="primary")],
        [InlineKeyboardButton("🛑 Finish Poll", callback_data=f"finish:{poll_id}", style="danger")],
        [InlineKeyboardButton("🔙 Back", callback_data=back, style="primary")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE.pop(uid, None)
    u = update.effective_user
    db.save_user(uid, u.first_name, u.username)
    try:
        await context.bot.set_message_reaction(chat_id=update.effective_chat.id, message_id=update.message.message_id, reaction=[ReactionTypeEmoji("⚡")])
    except Exception:
        pass
    emoji_message = await update.message.reply_text("⚡", reply_markup=ReplyKeyboardRemove())
    await asyncio.sleep(2)
    try:
        await emoji_message.delete()
    except Exception:
        pass
    await update.message.reply_text("🤖 *Poll Management Bot*\n\nChoose an option:", parse_mode="Markdown", reply_markup=main_menu())
    if is_admin(uid):
        await update.message.reply_text("⚙️ You are an admin. Use /admin to open the admin panel.")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return
    STATE.pop(update.effective_user.id, None)
    await update.message.reply_text("⚙️ *ADMIN PANEL*", parse_mode="Markdown", reply_markup=admin_menu(is_owner(update.effective_user.id)))


async def add_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    STATE[update.effective_user.id] = {"action": "add_group"}
    await update.message.reply_text(
        "➕ *Add Your Group*\n\nSend either a chat ID or username:\n`-1001234567890`\n`@groupusername`",
        parse_mode="Markdown",
        reply_markup=single_back("back_main"),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if data == "back_main":
        STATE.pop(uid, None)
        await q.edit_message_text("🤖 *Poll Management Bot*\n\nChoose an option:", parse_mode="Markdown", reply_markup=main_menu())
        return
    if data == "back_admin":
        STATE.pop(uid, None)
        if is_admin(uid):
            await q.edit_message_text("⚙️ *ADMIN PANEL*", parse_mode="Markdown", reply_markup=admin_menu(is_owner(uid)))
        return

    # USER REGISTRATION
    if data == "reg":
        STATE.pop(uid, None)
        await q.edit_message_text("📝 *Poll Registration*\n\nSelect Group:", parse_mode="Markdown", reply_markup=group_keyboard("regg"))
        return
    if data.startswith("regg:"):
        gid = int(data.split(":")[1]); group = db.get_group(gid); poll = db.active_poll(gid)
        if not poll:
            await q.edit_message_text(f"ℹ️ No open registration in *{group['title']}*.", parse_mode="Markdown", reply_markup=single_back("reg")); return
        count = db.registration_count(poll["id"]); slots = poll["slots"]
        if slots and count >= slots:
            await q.edit_message_text(f"🛑 Registration is full.\n\nSlots: {slots}\nRegistered: {count}", reply_markup=single_back("reg")); return
        STATE[uid] = {"action": "name", "poll_id": poll["id"]}
        slot_text = f"\n🎟️ Slots: {slots}\n👥 Registered: {count}" if slots else f"\n👥 Registered: {count}"
        await q.edit_message_text(f"📝 *{group['title']}*\n\n🗳️ {poll['question']}{slot_text}\n\nPlease send your name:", parse_mode="Markdown", reply_markup=single_back("reg")); return

    # USER DETAILS / RESULTS
    if data == "details":
        STATE.pop(uid, None)
        await q.edit_message_text("📋 *Poll Details*\n\nSelect Group:", parse_mode="Markdown", reply_markup=group_keyboard("detg")); return
    if data.startswith("detg:"):
        gid = int(data.split(":")[1]); group = db.get_group(gid); poll = db.active_poll(gid)
        if not poll:
            await q.edit_message_text(f"📋 *{group['title']}*\n\nNo open registration.", parse_mode="Markdown", reply_markup=single_back("details")); return
        count = db.registration_count(poll["id"]); slots = poll["slots"]
        slot_text = f"{count}/{slots}" if slots else str(count)
        await q.edit_message_text(f"📋 *{group['title']}*\n\n🗳️ {poll['question']}\n👥 Registered: {slot_text}\n📊 Registration: {'OPEN' if poll['registration_open'] else 'CLOSED'}", parse_mode="Markdown", reply_markup=single_back("details")); return
    if data == "results":
        STATE.pop(uid, None)
        await q.edit_message_text("🏆 *Poll Results*\n\nSelect Group:", parse_mode="Markdown", reply_markup=group_keyboard("resg")); return
    if data == "giveaway":
        STATE.pop(uid, None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Current Giveaways", callback_data="giveaway_current", style="success")],
            [InlineKeyboardButton("🏆 Giveaway Winners", callback_data="giveaway_winners", style="primary")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main", style="primary")],
        ])
        await q.edit_message_text("🎁 *Giveaway*\n\nChoose what you want to view:", parse_mode="Markdown", reply_markup=keyboard)
        return
    if data == "giveaway_current":
        current = db.get_giveaways("active")
        if not current:
            await q.edit_message_text("🎁 *Current Giveaways*\n\nNo current giveaways right now.", parse_mode="Markdown", reply_markup=single_back("giveaway")); return
        text = "🎁 *Current Giveaways*\n\n" + "\n\n".join(giveaway_text(giveaway) for giveaway in current)
        await q.edit_message_text(text[:4000], parse_mode="Markdown", reply_markup=single_back("giveaway"))
        return
    if data == "giveaway_winners":
        completed = db.get_giveaways("completed")
        if not completed:
            await q.edit_message_text("🏆 *Giveaway Winners*\n\nNo giveaway winners posted yet.", parse_mode="Markdown", reply_markup=single_back("giveaway")); return
        text = "🏆 *Giveaway Winners*\n\n" + "\n\n".join(giveaway_text(giveaway) for giveaway in completed)
        await q.edit_message_text(text[:4000], parse_mode="Markdown", reply_markup=single_back("giveaway"))
        for giveaway in completed:
            try:
                await send_giveaway_media(context.bot, uid, giveaway)
            except Exception as exc:
                logging.warning("Could not send giveaway winner media %s: %s", giveaway["id"], exc)
        return
    if data.startswith("resg:"):
        gid = int(data.split(":")[1]); group = db.get_group(gid); polls = db.completed_polls(gid)
        if not polls:
            await q.edit_message_text(f"🏆 *{group['title']}*\n\nNo completed polls yet.", parse_mode="Markdown", reply_markup=single_back("results")); return
        kb = [[InlineKeyboardButton(f"#{p['id']} — {p['question'][:35]}", callback_data=f"resp:{p['id']}", style="primary")] for p in polls]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="results", style="primary")])
        await q.edit_message_text(f"🏆 *{group['title']}*\n\nSelect a completed poll:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("resp:"):
        poll = db.get_poll(int(data.split(":")[1])); result = db.get_poll_results(poll["id"])
        await q.edit_message_text(format_result(poll, result) if result else "⏳ No verified result yet.", parse_mode="Markdown" if result else None, reply_markup=single_back(f"resg:{poll['group_id']}")); return

    if not is_admin(uid):
        return
    if q.message.chat.type != "private":
        return

    if data == "owner_admins":
        if not is_owner(uid):
            return
        lines = ["👑 *Manage Admins*", "", "Current admins:"]
        for admin in db.get_admins():
            label = " (owner)" if admin["user_id"] == OWNER_ID else ""
            lines.append(f"• `{admin['user_id']}`{label}")
        keyboard = [[InlineKeyboardButton("➕ Add Admin", callback_data="owner_add_admin", style="success")], [InlineKeyboardButton("➖ Remove Admin", callback_data="owner_remove_admin", style="danger")], [InlineKeyboardButton("🔙 Back", callback_data="back_admin", style="primary")]]
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)); return
    if data == "owner_add_admin":
        if not is_owner(uid):
            return
        STATE[uid] = {"action": "add_admin"}
        await q.edit_message_text("➕ *Add Admin*\n\nSend the new admin's numeric Telegram user ID.", parse_mode="Markdown", reply_markup=single_back("owner_admins")); return
    if data == "owner_remove_admin":
        if not is_owner(uid):
            return
        keyboard = [[InlineKeyboardButton(str(admin["user_id"]), callback_data=f"ownerremove:{admin['user_id']}", style="danger")] for admin in db.get_admins() if admin["user_id"] != OWNER_ID]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="owner_admins", style="primary")])
        await q.edit_message_text("➖ *Remove Admin*\n\nSelect an admin:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)); return
    if data == "owner_announce":
        if not is_owner(uid):
            return
        STATE[uid] = {"action": "admin_announcement"}
        await q.edit_message_text("📣 *Admin Announcement*\n\nSend the message to broadcast to all admins.", parse_mode="Markdown", reply_markup=single_back("back_admin")); return
    if data.startswith("ownerremove:"):
        if not is_owner(uid):
            return
        admin_id = int(data.split(":")[1])
        if admin_id != OWNER_ID:
            db.remove_admin(admin_id)
        await q.edit_message_text("✅ Admin removed.", reply_markup=single_back("owner_admins")); return

    # ADMIN: GROUPS
    if data == "admin_add":
        STATE[uid] = {"action": "add_group"}
        await q.edit_message_text("➕ *Add Group*\n\nSend either a chat ID or username:\n`-1001234567890`\n`@groupusername`", parse_mode="Markdown", reply_markup=single_back("back_admin")); return
    if data == "admin_remove":
        await q.edit_message_text("🗑 *Remove Group*\n\nSelect group:", parse_mode="Markdown", reply_markup=group_keyboard("remove", "back_admin")); return
    if data.startswith("remove:"):
        db.remove_group(int(data.split(":")[1])); await q.edit_message_text("✅ Group removed.", reply_markup=single_back("back_admin")); return

    # ADMIN: POLL REGISTRATION CREATION
    if data == "admin_poll":
        STATE[uid] = {"action": "select_poll_group"}
        await q.edit_message_text("📝 *Poll Registration*\n\nSelect group:", parse_mode="Markdown", reply_markup=group_keyboard("pollgroup", "back_admin")); return
    if data.startswith("pollgroup:"):
        STATE[uid] = {"action": "poll_question", "group_id": int(data.split(":")[1])}
        await q.edit_message_text("📝 *Poll Registration*\n\nSend the main poll question:", parse_mode="Markdown", reply_markup=single_back("admin_poll")); return
    if data == "admin_finishreg":
        polls = db.registration_polls()
        if not polls:
            await q.edit_message_text("🛑 No open registrations.", reply_markup=single_back("back_admin")); return
        kb = []
        for p in polls:
            g = db.get_group(p["group_id"]); title = g["title"] if g else str(p["group_id"])
            kb.append([InlineKeyboardButton(f"#{p['id']} — {title} ({db.registration_count(p['id'])}/{p['slots']})", callback_data=f"finishreg:{p['id']}", style="danger")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_admin", style="primary")])
        await q.edit_message_text("🛑 *Finish Registration*\n\nSelect registration:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("finishreg:"):
        pid = int(data.split(":")[1]); poll = db.get_poll(pid)
        if not poll or poll["status"] != "registration":
            await q.edit_message_text("❌ Registration not found/open.", reply_markup=single_back("admin_finishreg")); return
        db.close_registration(pid)
        names = format_registration_names(pid)
        await try_group_message(context, poll["group_id"], f"✅ Registration closed for poll #{pid}.\n\nRegistered people:\n{names}")
        await q.edit_message_text(f"✅ Registration closed\n\nPoll #{pid}\n👥 Registered: {db.registration_count(pid)}/{poll['slots']}\n\nRegistered people:\n{names}\n\nNow use 🗳 Main Poll to post the actual Telegram poll.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗳 Main Poll", callback_data="admin_mainpoll", style="success")],[InlineKeyboardButton("🔙 Admin Panel", callback_data="back_admin", style="primary")]])); return

    # ADMIN: MAIN TELEGRAM POLL
    if data == "admin_mainpoll":
        await q.edit_message_text("🗳 *Main Poll*\n\nSelect a group:", parse_mode="Markdown", reply_markup=group_keyboard("mainpollgroup", "back_admin")); return
    if data.startswith("mainpollgroup:"):
        gid = int(data.split(":")[1]); group = db.get_group(gid)
        kb = [[InlineKeyboardButton("➕ Send New Poll", callback_data=f"newmainpoll:{gid}", style="success")]]
        for p in db.ready_polls():
            if p["group_id"] == gid:
                options = json.loads(p["options"])
                if options:
                    kb.append([InlineKeyboardButton(f"▶️ #{p['id']} — {p['question'][:30]}", callback_data=f"postpoll:{p['id']}", style="success")])
        for p in db.live_polls(gid):
            kb.append([InlineKeyboardButton(f"📊 #{p['id']} — {p['question'][:30]} (LIVE)", callback_data=f"mainlive:{p['id']}", style="primary")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_mainpoll", style="primary")])
        await q.edit_message_text(f"🗳 *Main Poll*\n\nGroup: *{group['title']}*\n\nChoose an action:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("newmainpoll:"):
        STATE[uid] = {"action": "main_poll_question", "group_id": int(data.split(":")[1])}
        await q.edit_message_text("🗳 *New Main Poll*\n\nSend the poll question:", parse_mode="Markdown", reply_markup=single_back("admin_mainpoll")); return
    if data.startswith("postpoll:"):
        pid = int(data.split(":")[1]); poll = db.get_poll(pid)
        if not poll or poll["status"] != "ready":
            await q.edit_message_text("❌ This registration is not ready.", reply_markup=single_back("admin_mainpoll")); return
        options = json.loads(poll["options"])
        if not options:
            await q.edit_message_text("✅ Registration is complete. Use ➕ Send New Poll to enter the question and answer options.", reply_markup=single_back(f"mainpollgroup:{poll['group_id']}")); return
        try:
            msg = await context.bot.send_poll(chat_id=poll["group_id"], question=poll["question"], options=options, is_anonymous=False, allows_multiple_answers=False)
            await pin_message(context, poll["group_id"], msg.message_id)
            await try_group_message(context, poll["group_id"], f"🟢 Poll #{pid} has started.\n\n🗳️ {poll['question']}")
        except Exception as exc:
            await q.edit_message_text(f"❌ Could not post poll.\n\n{exc}", reply_markup=single_back("admin_mainpoll")); return
        db.set_poll_message(pid, msg.message_id, msg.poll.id); db.mark_live(pid)
        await q.edit_message_text(f"🗳️ *Main Poll #{pid} posted!*\n\n👥 Registered users: {db.registration_count(pid)}\n🔒 Non-anonymous: ON\n🔎 Voter tracking: ON\n\nMembers can now vote in the group.", parse_mode="Markdown", reply_markup=poll_admin_buttons(pid)); return
    if data.startswith("mainlive:"):
        pid = int(data.split(":")[1]); poll = db.get_poll(pid)
        await q.edit_message_text(f"📊 *Poll #{pid}*\n\n{poll['question']}\n\n👥 Registered: {db.registration_count(pid)}\n🗳️ Tracked voters: {db.vote_count(pid)}", parse_mode="Markdown", reply_markup=poll_admin_buttons(pid)); return

    # ADMIN: VALID VOTES
    if data == "admin_valid":
        polls = db.live_polls()
        if not polls:
            await q.edit_message_text("📊 No live polls.", reply_markup=single_back("back_admin")); return
        kb=[]
        for p in polls:
            g=db.get_group(p["group_id"]); title=g["title"] if g else str(p["group_id"])
            kb.append([InlineKeyboardButton(f"#{p['id']} — {title}", callback_data=f"checkvotes:{p['id']}", style="primary")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_admin", style="primary")])
        await q.edit_message_text("📊 *Valid Votes*\n\nSelect live poll:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("checkvotes:"):
        pid=int(data.split(":")[1]); poll=db.get_poll(pid)
        if not poll or poll["status"] not in ("live", "completed"):
            await q.edit_message_text("❌ Poll is not live.", reply_markup=single_back("back_admin")); return
        try:
            current_total = sum(o.voter_count for o in (await context.bot.stop_poll(chat_id=poll["group_id"], message_id=poll["telegram_message_id"])).options) if False else None
        except Exception:
            current_total = None
        valid_counts, invalid, checked = await verify_poll_votes(context, poll, current_total)
        result=db.get_poll_results(pid)
        await q.edit_message_text(format_result(poll,result), parse_mode="Markdown", reply_markup=poll_admin_buttons(pid) if poll["status"]=="live" else single_back("admin_results")); return

    # ADMIN: FINISH POLL
    if data.startswith("finish:"):
        pid=int(data.split(":")[1]); poll=db.get_poll(pid)
        if not poll or poll["status"] != "live":
            await q.edit_message_text("❌ Live poll not found.", reply_markup=single_back("admin_mainpoll")); return
        try:
            stopped=await context.bot.stop_poll(chat_id=poll["group_id"], message_id=poll["telegram_message_id"])
        except Exception as exc:
            await q.edit_message_text(f"❌ Could not stop Telegram poll.\n\n{exc}", reply_markup=single_back("admin_mainpoll")); return
        telegram_total=sum(o.voter_count for o in stopped.options)
        await verify_poll_votes(context,poll,telegram_total)
        db.mark_completed(pid)
        result=db.get_poll_results(pid)
        await try_group_message(context, poll["group_id"], format_result(poll, result) + "\n\n🛑 Telegram poll finished.", parse_mode="Markdown")
        await q.edit_message_text(format_result(poll,result)+"\n\n🛑 Telegram poll finished.", parse_mode="Markdown", reply_markup=single_back("back_admin","🔙 Back to Admin")); return

    # ADMIN: REGISTRATIONS
    if data == "admin_regs":
        await q.edit_message_text("👥 *Registrations*\n\nSelect group:", parse_mode="Markdown", reply_markup=group_keyboard("areg","back_admin")); return
    if data.startswith("areg:"):
        gid=int(data.split(":")[1]); polls=db.all_polls(gid)
        if not polls:
            await q.edit_message_text("No registrations yet.", reply_markup=single_back("admin_regs")); return
        kb=[]
        active_polls = [p for p in polls if p["status"] != "completed"]
        past_polls = [p for p in polls if p["status"] == "completed"]
        for p in active_polls:
            kb.append([InlineKeyboardButton(f"🟢 #{p['id']} — {p['question'][:30]} ({db.registration_count(p['id'])})", callback_data=f"reglist:{p['id']}", style="primary")])
        for p in past_polls:
            kb.append([InlineKeyboardButton(f"📁 #{p['id']} — {p['question'][:30]} ({db.registration_count(p['id'])})", callback_data=f"reglist:{p['id']}", style="primary")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_regs", style="primary")])
        await q.edit_message_text("👥 Select poll to view registered users:\n\n🟢 Active polls\n📁 Past polls", reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("reglist:"):
        pid=int(data.split(":")[1]); poll=db.get_poll(pid); rows=db.registrations(pid)
        if not poll:
            await q.edit_message_text("❌ Poll not found.", reply_markup=single_back("admin_regs")); return
        lines=[f"👥 Poll #{pid}",f"🗳️ {poll['question']}",f"🎟️ Slots: {poll['slots']}",f"👤 Registered: {len(rows)}","", "Registered users:" ]
        for i,r in enumerate(rows,1):
            username = f"@{r['username']}" if r['username'] else "No username"
            lines.append(f"{i}. {r['name']} | {username} | ID: {r['user_id']}")
        if not rows: lines.append("None")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=single_back(f"areg:{poll['group_id']}")); return

    if data == "admin_results":
        polls=db.completed_polls_all()
        if not polls:
            await q.edit_message_text("🏆 No completed polls.", reply_markup=single_back("back_admin")); return
        kb=[]
        for p in polls:
            g=db.get_group(p["group_id"]); title=g["title"] if g else str(p["group_id"])
            kb.append([InlineKeyboardButton(f"#{p['id']} — {title}", callback_data=f"respadmin:{p['id']}", style="primary")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_admin", style="primary")])
        await q.edit_message_text("🏆 *Poll Results*\n\nSelect poll:",parse_mode="Markdown",reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("respadmin:"):
        pid=int(data.split(":")[1]); poll=db.get_poll(pid); result=db.get_poll_results(pid)
        await q.edit_message_text(format_result(poll,result) if result else "No verified result.",parse_mode="Markdown" if result else None,reply_markup=single_back("admin_results")); return

    if data == "admin_stats":
        s=db.stats(); await q.edit_message_text(f"📊 *Statistics*\n\nGroups: {s['groups']}\nPolls: {s['polls']}\nRegistrations: {s['registrations']}\nTracked votes: {s['votes']}",parse_mode="Markdown",reply_markup=single_back("back_admin")); return
    if data == "admin_giveaway":
        giveaways = db.get_giveaways()
        keyboard = [[InlineKeyboardButton("➕ Add Giveaway", callback_data="giveaway_add", style="success")]]
        for giveaway in giveaways:
            keyboard.append([InlineKeyboardButton(f"#{giveaway['id']} {giveaway['title'][:35]} ({giveaway['status']})", callback_data=f"giveaway_manage:{giveaway['id']}", style="primary")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_admin", style="primary")])
        await q.edit_message_text("🎁 *Giveaway Management*\n\nCreate, publish, or post results for giveaways.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)); return
    if data == "giveaway_add":
        STATE[uid] = {"action": "giveaway_title"}
        await q.edit_message_text("🎁 *New Giveaway*\n\nSend the giveaway title.", parse_mode="Markdown", reply_markup=single_back("admin_giveaway")); return
    if data.startswith("giveaway_manage:"):
        giveaway = db.get_giveaway(int(data.split(":")[1]))
        if not giveaway:
            await q.edit_message_text("❌ Giveaway not found.", reply_markup=single_back("admin_giveaway")); return
        keyboard = []
        if giveaway["status"] != "completed":
            keyboard.append([InlineKeyboardButton("📢 Publish to Groups", callback_data=f"giveaway_publish:{giveaway['id']}", style="success")])
        if giveaway["status"] != "completed":
            keyboard.append([InlineKeyboardButton("🏆 Post Winners", callback_data=f"giveaway_finish:{giveaway['id']}", style="primary")])
        else:
            keyboard.append([InlineKeyboardButton("✏️ Update Winners", callback_data=f"giveaway_finish:{giveaway['id']}", style="primary")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="admin_giveaway", style="primary")])
        await q.edit_message_text(giveaway_text(giveaway), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)); return
    if data.startswith("giveaway_publish:"):
        giveaway = db.get_giveaway(int(data.split(":")[1]))
        await asyncio.gather(*(try_group_message(context, group["id"], giveaway_text(giveaway), parse_mode="Markdown") for group in db.get_groups()))
        db.set_giveaway_status(giveaway["id"], "active")
        await q.edit_message_text("✅ Giveaway published to active groups.", reply_markup=single_back("admin_giveaway")); return
    if data.startswith("giveaway_finish:"):
        STATE[uid] = {"action": "giveaway_winner", "giveaway_id": int(data.split(":")[1])}
        await q.edit_message_text("🏆 Send the giveaway winner(s) as text or upload a winner screenshot with an optional caption.", reply_markup=single_back("admin_giveaway")); return
    if data == "admin_announce":
        STATE[uid]={"action":"announcement"}
        await q.edit_message_text("📢 *Announcement*\n\nSend the message to broadcast.",parse_mode="Markdown",reply_markup=single_back("back_admin")); return
    if data == "lucky_dip":
        STATE[uid] = {"action": "lucky_dip_names"}
        await q.edit_message_text("🎲 *Lucky Dip*\n\nSend 2 to 15 names, one per line.", parse_mode="Markdown", reply_markup=single_back("back_admin")); return
    if data.startswith("lucky_group:"):
        state = STATE.get(uid)
        if not state or state.get("action") != "lucky_dip_group":
            return
        group_id = int(data.split(":")[1])
        names = state["names"]
        STATE.pop(uid, None)
        await q.edit_message_text("🎲 Lucky Dip started. The winner will be announced in 30 seconds.", reply_markup=single_back("back_admin"))
        asyncio.create_task(run_lucky_dip(context.bot, group_id, names))
        return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; state=STATE.get(uid)
    if not state: return
    if update.effective_chat.type != "private" and state["action"] != "name":
        return
    text = update.message.text.strip()
    if state["action"]=="add_group":
        parts = update.message.text.split()
        if not parts:
            await update.message.reply_text("❌ Send a numeric chat ID or @username.", reply_markup=single_back("back_admin")); return
        reference = parts[0]
        chat_id = int(reference) if reference.lstrip("-").isdigit() else reference
        username = reference.lstrip("@") if isinstance(chat_id, str) else (parts[1].lstrip("@") if len(parts) > 1 else None)
        try:
            chat = await context.bot.get_chat(chat_id)
            if not await verify_group_owner(context, chat.id, uid):
                raise RuntimeError("Only the group owner can add this group.")
        except Exception as exc:
            await update.message.reply_text(f"❌ Group was not added.\n\n{exc}\n\nThe bot must be an administrator to verify group ownership.", reply_markup=single_back("back_admin")); return
        db.add_group(chat.id, chat.title or str(chat.id), username or getattr(chat, "username", None)); STATE.pop(uid,None)
        back_callback = "back_admin" if is_admin(uid) else "back_main"
        back_text = "🔙 Back to Admin" if is_admin(uid) else "🏠 Main Menu"
        await update.message.reply_text(f"✅ Group added!\n\n{chat.title}\nID: {chat.id}\nUsername: @{username or getattr(chat, 'username', '') or 'none'}", reply_markup=single_back(back_callback, back_text)); return
    if state["action"]=="announcement":
        if not is_admin(uid): STATE.pop(uid,None); return
        job_id = db.create_announcement_job(uid, update.effective_chat.id, update.message.message_id, announcement_recipients())
        STATE.pop(uid,None)
        asyncio.create_task(run_announcement(context.bot, job_id))
        await update.message.reply_text(f"📢 Announcement started\n\n👥 Total: {db.get_announcement_job(job_id)['total']}\n\nThe announcement is running in the background.", reply_markup=single_back("back_admin","🔙 Back to Admin")); return
    if state["action"]=="admin_announcement":
        if not is_owner(uid):
            STATE.pop(uid,None); return
        job_id = db.create_announcement_job(uid, update.effective_chat.id, update.message.message_id, [(admin["user_id"], 1) for admin in db.get_admins()])
        STATE.pop(uid,None)
        asyncio.create_task(run_announcement(context.bot, job_id))
        await update.message.reply_text("📢 Admin announcement started in the background.", reply_markup=single_back("back_admin", "🔙 Back to Admin")); return
    if state["action"]=="add_admin":
        if not is_owner(uid):
            STATE.pop(uid,None); return
        try:
            admin_id = int(update.message.text.strip())
            if admin_id <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Send a valid numeric Telegram user ID."); return
        db.add_admin(admin_id); STATE.pop(uid,None)
        await update.message.reply_text(f"✅ Admin added: {admin_id}", reply_markup=single_back("owner_admins")); return

    if state["action"]=="giveaway_title":
        state["title"] = text
        state["action"] = "giveaway_description"
        await update.message.reply_text("🎁 Send the giveaway description, rules, and prize details.", reply_markup=single_back("admin_giveaway")); return
    if state["action"]=="giveaway_description":
        giveaway_id = db.create_giveaway(state["title"], text)
        STATE.pop(uid, None)
        await update.message.reply_text(f"✅ Giveaway #{giveaway_id} created.", reply_markup=single_back("admin_giveaway")); return
    if state["action"]=="giveaway_winner":
        giveaway = db.get_giveaway(state["giveaway_id"])
        db.set_giveaway_status(giveaway["id"], "completed", text)
        completed = db.get_giveaway(giveaway["id"])
        await asyncio.gather(*(try_group_message(context, group["id"], giveaway_text(completed), parse_mode="Markdown") for group in db.get_groups()))
        STATE.pop(uid, None)
        await update.message.reply_text("✅ Giveaway result published to active groups.", reply_markup=single_back("admin_giveaway")); return

    if state["action"] == "lucky_dip_names":
        names = parse_lucky_dip_names(text)
        if not 2 <= len(names) <= 15:
            await update.message.reply_text("❌ Send between 2 and 15 names, one per line.", reply_markup=single_back("back_admin")); return
        state["action"] = "lucky_dip_group"
        state["names"] = names
        await update.message.reply_text("✅ Names received. Select the group where the winner should be announced:", reply_markup=group_keyboard("lucky_group", "back_admin")); return

    if state["action"]=="name":
        pid=state["poll_id"]
        ok,reason,count,slots=db.register(pid,uid,text)
        if ok:
            msg=f"✅ Registration successful!\n\n👥 Registered: {count}/{slots}"
            if slots and count>=slots:
                names = format_registration_names(pid)
                msg += f"\n🛑 Slots full — registration closed automatically.\n\nRegistered people:\n{names}"
                poll = db.get_poll(pid)
                await try_group_message(context, poll["group_id"], f"✅ Registration is complete for poll #{pid}.\n\nRegistered people:\n{names}")
        elif reason=="duplicate": msg="⚠️ You are already registered for this poll."
        elif reason=="full": msg=f"🛑 All {slots} slots are full. Registration is closed."
        else: msg="❌ Registration is closed."
        await update.message.reply_text(msg,reply_markup=single_back("back_main","🏠 Main Menu")); STATE.pop(uid,None); return

    if not is_admin(uid) or update.effective_chat.type != "private": return
    if state["action"]=="poll_question":
        state["question"] = text; state["action"] = "poll_slots"
        await update.message.reply_text("🎟️ How many registration slots?\n\nExample: 10\nSend 0 for unlimited.", reply_markup=single_back("admin_poll")); return
    if state["action"]=="main_poll_question":
        state["question"] = text; state["action"] = "main_poll_options"
        await update.message.reply_text("Send 2–10 options separated by commas.", reply_markup=single_back("admin_mainpoll")); return
    if state["action"]=="main_poll_options":
        options = [x.strip() for x in text.split(",") if x.strip()]
        if not 2 <= len(options) <= 10:
            await update.message.reply_text("❌ Please provide 2 to 10 options."); return
        try:
            poll = await send_direct_poll(context, state["group_id"], state["question"], options)
        except Exception as exc:
            STATE.pop(uid, None)
            await update.message.reply_text(f"❌ Could not post poll.\n\n{exc}", reply_markup=single_back("admin_mainpoll")); return
        STATE.pop(uid, None)
        await update.message.reply_text(f"✅ Poll #{poll['id']} started in the group.\n\n🗳️ {poll['question']}", reply_markup=poll_admin_buttons(poll["id"])); return
    if state["action"]=="poll_slots":
        try: slots=int(text)
        except ValueError: await update.message.reply_text("❌ Enter a whole number, e.g. 10."); return
        if slots<0: await update.message.reply_text("❌ Slots cannot be negative."); return
        pid = db.create_poll(state["group_id"], state["question"], [], slots)
        STATE.pop(uid,None)
        await update.message.reply_text(f"✅ *Poll Registration #{pid} created*\n\n🎟️ Slots: {'Unlimited' if slots==0 else slots}\n👥 Registration is OPEN.\n\nWhen registration is full it closes automatically. Otherwise use Admin → 🛑 Finish Registration.",parse_mode="Markdown",reply_markup=single_back("back_admin","🔙 Back to Admin")); return


async def announcement_media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = STATE.get(uid)
    if not state or update.effective_chat.type != "private":
        return
    if state.get("action") == "giveaway_winner":
        if not is_admin(uid) or not update.message.photo:
            return
        giveaway = db.get_giveaway(state["giveaway_id"])
        caption = update.message.caption or "🏆 Giveaway winner"
        db.set_giveaway_status(giveaway["id"], "completed", caption, "photo", update.message.photo[-1].file_id, caption)
        completed = db.get_giveaway(giveaway["id"])
        async def publish_winner(group):
            await try_group_message(context, group["id"], giveaway_text(completed), parse_mode="Markdown")
            try:
                await send_giveaway_media(context.bot, group["id"], completed)
            except Exception as exc:
                logging.warning("Could not publish giveaway winner screenshot %s: %s", giveaway["id"], exc)

        await asyncio.gather(*(publish_winner(group) for group in db.get_groups()))
        STATE.pop(uid, None)
        await update.message.reply_text("✅ Giveaway winner screenshot published to active groups.", reply_markup=single_back("admin_giveaway"))
        return
    if state.get("action") not in ("announcement", "admin_announcement"):
        return
    if state["action"] == "admin_announcement" and not is_owner(uid):
        STATE.pop(uid, None)
        return
    if state["action"] == "announcement" and not is_admin(uid):
        STATE.pop(uid, None)
        return
    recipients = announcement_recipients() if state["action"] == "announcement" else [(admin["user_id"], 1) for admin in db.get_admins()]
    job_id = db.create_announcement_job(uid, update.effective_chat.id, update.message.message_id, recipients)
    STATE.pop(uid, None)
    asyncio.create_task(run_announcement(context.bot, job_id))
    await update.message.reply_text("📢 Announcement started in the background.", reply_markup=single_back("back_admin", "🔙 Back to Admin"))


async def send_direct_poll(context, group_id, question, options, allows_multiple_answers=False):
    pid = db.create_poll(group_id, question, options, registration_required=0)
    db.close_registration(pid)
    poll = db.get_poll(pid)
    message = await context.bot.send_poll(
        chat_id=group_id,
        question=question,
        options=options,
        is_anonymous=False,
        allows_multiple_answers=allows_multiple_answers,
    )
    await pin_message(context, group_id, message.message_id)
    await try_group_message(context, group_id, f"🟢 Poll #{pid} has started.\n\n🗳️ {question}")
    db.set_poll_message(pid, message.message_id, message.poll.id)
    db.mark_live(pid)
    return db.get_poll(pid)


async def incoming_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != "private":
        return
    state = STATE.get(uid)
    if not state or state.get("action") != "main_poll_question" or not is_admin(uid):
        return
    incoming = update.message.poll
    options = [option.text for option in incoming.options]
    try:
        poll = await send_direct_poll(
            context,
            state["group_id"],
            incoming.question,
            options,
            incoming.allows_multiple_answers,
        )
    except Exception as exc:
        STATE.pop(uid, None)
        await update.message.reply_text(f"❌ Could not post poll.\n\n{exc}", reply_markup=single_back("admin_mainpoll"))
        return
    STATE.pop(uid, None)
    await update.message.reply_text(
        f"✅ Poll #{poll['id']} started in the group.",
        reply_markup=poll_admin_buttons(poll["id"]),
    )


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer=update.poll_answer
    if not answer: return
    poll=db.get_poll_by_telegram_poll(answer.poll_id)
    if poll: db.save_vote(poll["id"],answer.user.id,answer.option_ids)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start — main menu\n/addgroup — add a group\n/admin — admin panel for authorized admins")


async def resume_announcements(application):
    for job_id in db.recover_announcement_jobs():
        application.create_task(run_announcement(application.bot, job_id))


def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is missing in .env")
    db.init_db(set(ADMIN_IDS) | {OWNER_ID})
    app=Application.builder().token(BOT_TOKEN).post_init(resume_announcements).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_cmd))
    app.add_handler(CommandHandler("addgroup",add_group_cmd))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(PollAnswerHandler(poll_answer))
    app.add_handler(ChatMemberHandler(track_group_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.POLL, incoming_poll_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & ~filters.POLL & ~filters.COMMAND, announcement_media_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group_message), group=1)
    print("Bot is running...")
    app.run_polling()


if __name__=="__main__": main()
