import json
import logging
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PollAnswerHandler, ChatMemberHandler, ContextTypes, filters
)

import database as db
from config import BOT_TOKEN, ADMIN_IDS, OWNER_ID

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
STATE = {}


def is_admin(user_id):
    return user_id == OWNER_ID or db.is_admin(user_id)


def is_owner(user_id):
    return user_id == OWNER_ID


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Poll Registration", callback_data="reg", style="success")],
        [InlineKeyboardButton("📋 Poll Details", callback_data="details", style="primary")],
        [InlineKeyboardButton("🏆 Poll Results", callback_data="results", style="primary")],
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


async def pin_message(context, chat_id, message_id):
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status == "administrator" and getattr(bot_member, "can_pin_messages", False):
            await context.bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception as exc:
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
    if data == "admin_announce":
        STATE[uid]={"action":"announcement"}
        await q.edit_message_text("📢 *Announcement*\n\nSend the message to broadcast.",parse_mode="Markdown",reply_markup=single_back("back_admin")); return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; state=STATE.get(uid)
    if not state: return
    if update.effective_chat.type != "private" and state["action"] != "name":
        return
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
        message=update.message.text
        users=db.get_users(); groups=db.get_groups(); uok=ufail=gok=gfail=0
        for u in users:
            try: await context.bot.send_message(chat_id=u["user_id"],text=message); uok+=1; await asyncio.sleep(.05)
            except Exception: ufail+=1
        for g in groups:
            try: await send_group_message(context, g["id"], message); gok+=1; await asyncio.sleep(.05)
            except Exception: gfail+=1
        STATE.pop(uid,None); await update.message.reply_text(f"✅ Announcement sent\n\n👤 DMs: {uok} sent, {ufail} failed\n👥 Groups: {gok} sent, {gfail} failed",reply_markup=single_back("back_admin","🔙 Back to Admin")); return
    if state["action"]=="admin_announcement":
        if not is_owner(uid):
            STATE.pop(uid,None); return
        message = update.message.text
        sent = failed = 0
        for admin in db.get_admins():
            try:
                await context.bot.send_message(chat_id=admin["user_id"], text=message)
                sent += 1
                await asyncio.sleep(.05)
            except Exception:
                failed += 1
        STATE.pop(uid,None)
        await update.message.reply_text(f"✅ Admin announcement sent\n\n👑 Admins: {sent} sent, {failed} failed", reply_markup=single_back("back_admin", "🔙 Back to Admin")); return
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

    text=update.message.text.strip()
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


def main():
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is missing in .env")
    db.init_db(set(ADMIN_IDS) | {OWNER_ID})
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("admin",admin_cmd))
    app.add_handler(CommandHandler("addgroup",add_group_cmd))
    app.add_handler(CommandHandler("help",help_cmd))
    app.add_handler(PollAnswerHandler(poll_answer))
    app.add_handler(ChatMemberHandler(track_group_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.POLL, incoming_poll_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group_message), group=1)
    print("🤖 Bot is running...")
    app.run_polling()


if __name__=="__main__": main()
