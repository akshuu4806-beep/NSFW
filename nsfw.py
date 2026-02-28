import os
import time
import asyncio
import requests
import json
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from motor.motor_asyncio import AsyncIOMotorClient

# 🟢 Keep Alive Import
from keep_alive import keep_alive

# ================= CONFIGURATION (Secure via ENV) =================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
OWNER_ID = int(os.getenv("OWNER_ID"))
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "My NSFW Bot")
BOT_USERNAME = os.getenv("BOT_USERNAME")

# Sightengine Keys as JSON String: [{"user":"u1","secret":"s1"},{"user":"u2","secret":"s2"}]
SIGHTENGINE_KEYS = json.loads(os.getenv("SIGHTENGINE_KEYS"))
current_key_index = 0
start_time = time.time()
temp_group_list = {}

# --- MongoDB Setup ---
db_client = AsyncIOMotorClient(MONGO_URL)
mongo_db = db_client["nsfw_bot_database"]
settings_col = mongo_db["settings"]
stats_col = mongo_db["stats"]

# Helper functions for MongoDB
stats_col = mongo_db["stats"] # Naya collection stats ke liye

async def update_stat(field):
    await stats_col.update_one({"_id": "bot_stats"}, {"$inc": {field: 1}}, upsert=True)

async def get_stats():
    doc = await stats_col.find_one({"_id": "bot_stats"})
    return doc if doc else {"total_scans": 0, "nsfw_blocked": 0, "abuse_blocked": 0}

async def get_sudo_users():
    doc = await settings_col.find_one({"_id": "sudo_list"})
    return doc.get("users", []) if doc else []

async def is_sudo(user_id):
    if user_id == OWNER_ID:
        return True
    sudo_list = await get_sudo_users()
    return user_id in sudo_list

async def get_blocked_packs():
    doc = await settings_col.find_one({"_id": "blocked_stickers"})
    return doc.get("packs", []) if doc else []

async def get_blocked_words():
    doc = await settings_col.find_one({"_id": "blocked_words"})
    return doc.get("words", []) if doc else []

async def get_nsfw_status(chat_id):
    doc = await settings_col.find_one({"_id": f"nsfw_status_{chat_id}"})
    return doc.get("status", True) if doc else True

async def delete_msg_later(client, chat_id, message_id, delay=5):
    await asyncio.sleep(delay)
    try: await client.delete_messages(chat_id, message_id)
    except: pass

async def get_silent_admin_tags(client, chat_id):
    tags = ""
    try:
        async for m in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
            if not m.user.is_bot: 
                tags += f"<a href='tg://user?id={m.user.id}'>\u200b</a>"
    except: pass
    return tags  

async def set_nsfw_status(chat_id, status: bool):
    await settings_col.update_one(
        {"_id": f"nsfw_status_{chat_id}"},
        {"$set": {"status": status}},
        upsert=True
    )

# GLOBAL NSFW STATUS (Sare groups ke liye ek master switch)
async def get_global_nsfw():
    doc = await settings_col.find_one({"_id": "global_nsfw_status"})
    return doc.get("status", True) if doc else True

async def set_global_nsfw(status: bool):
    await settings_col.update_one(
        {"_id": "global_nsfw_status"},
        {"$set": {"status": status}},
        upsert=True
    )
# -----------------------------------------------------------

app = Client(
    "nsfw_standalone_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ================= COMMANDS =================

# ================= BUTTONS CONFIGURATION (No Support) =================

# 1. Private /start buttons
START_PRIVATE_BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("➕ Add Me To Your Group ➕", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
    [InlineKeyboardButton("📖 Help", callback_data="help_back"), InlineKeyboardButton("🗑️ Close", callback_data="close_status")]
])

# 2. Group /start & /help button
GOTO_DM_BUTTON = InlineKeyboardMarkup([
    [InlineKeyboardButton("📩 Open in Private", url=f"https://t.me/{BOT_USERNAME}?start=help")]
])

# 3. Private Help Menu Buttons
HELP_PRIVATE_BUTTONS = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔙 Back", callback_data="start_back"), InlineKeyboardButton("🗑️ Close", callback_data="close_status")]
])

# ================= START & HELP COMMANDS =================

@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    # Deep Linking Check: Agar user group se button daba kar aaya hai
    if len(message.command) > 1 and message.command[1] == "help":
        return await help_logic(client, message)

    if message.chat.type == enums.ChatType.PRIVATE:
        text = (
            f"👋 **Hello {message.from_user.mention}!**\n\n"
            f"Main **{BOT_DISPLAY_NAME}** hoon—ek advanced NSFW filter bot.\n\n"
            "✨ **Advanced Features:**\n"
            "• AI-based Nudity Detection\n"
            "• Auto-Abuse Word Delete\n"
            " Explict the unwanted content.\n"
            
        )
        await message.reply_text(text, reply_markup=START_PRIVATE_BUTTONS)
    else:
        await message.reply_text(
            f"Hey {message.from_user.mention}, main **{BOT_DISPLAY_NAME}** hoon! "
            "Mujhe use karne ka tarika mere DM mein dekhein.",
            reply_markup=GOTO_DM_BUTTON
        )

@app.on_message(filters.command("help"))
async def help_cmd(client, message):
    if message.chat.type == enums.ChatType.PRIVATE:
        await help_logic(client, message)
    else:
        # Group mein /help karne par
        await message.reply_text(
            "📖 **Help Menu** maine aapke private chat mein bhej diya hai (ya niche button dabayein).",
            reply_markup=GOTO_DM_BUTTON
        )

# Reusable Help Logic
async def help_logic(client, message):
    help_text = (
        "📖 **NSFW Filter Bot - Help Menu**\n\n"
        "🛡️ **Admin Commands:**\n"
        "• `/nsfw on/off` - Toggle filter.\n"
        "• `/status` - Check stats & uptime.\n\n"
        "👑 **Owner/Sudo Commands:**\n"
        "• `/addword` / `/rmword` - Abuse list.\n"
        "• `/addpack` / `/rmpack` - Sticker list.\n"
        "• `/addsudo` - Add new admin.\n"
        "• `/broadcast` - Global message.\n\n"
        "💡 **Note:** Commands reply ke saath bhi work karte hain!"
    )
    # Agar ye callback se aaya hai toh edit karega, message se aaya hai toh reply
    if hasattr(message, 'reply_text'):
        await message.reply_text(help_text, reply_markup=HELP_PRIVATE_BUTTONS)
    else:
        await message.edit_text(help_text, reply_markup=HELP_PRIVATE_BUTTONS)

# ================= CALLBACK HANDLERS =================

@app.on_callback_query()
async def callback_handler(client, query: CallbackQuery):
    if query.data == "help_back":
        await help_logic(client, query.message)
        
    elif query.data == "start_back":
        text = (
            f"👋 **Hello!**\n\n"
            "Main ek **Advanced NSFW aur Abuse Filter** bot hoon.\n"
            "Mujhe use karne ke liye niche diye buttons ka use karein."
        )
        await query.message.edit_text(text, reply_markup=START_PRIVATE_BUTTONS)
        
    elif query.data == "close_status":
        await query.message.delete()

@app.on_message(filters.command("status"))
async def status_cmd(client, message):
    # Uptime calculation
    uptime_sec = int(time.time() - start_time)
    h = uptime_sec // 3600
    m = (uptime_sec % 3600) // 60
    s = uptime_sec % 60
    
    stats = await get_stats()
    groups_count = await client.get_dialogs_count()
    
    status_text = (
        "📊 **Bot Operational Status**\n\n"
        f"⏱️ **Uptime:** `{h}h {m}m {s}s`\n"
        f"👥 **Monitored Groups:** `{groups_count}`\n"
        f"🔍 **Total Scans:** `{stats.get('total_scans', 0)}`\n"
        f"🚫 **NSFW Blocked:** `{stats.get('nsfw_blocked', 0)}`\n"
        f"🤬 **Abuse Blocked:** `{stats.get('abuse_blocked', 0)}`"
    )
    
    # Delete button (Unlocked for all)
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ Delete Status", callback_data="del_status")]])
    await message.reply_text(status_text, reply_markup=reply_markup)

@app.on_callback_query(filters.regex("del_status"))
async def del_status_callback(client, callback_query: CallbackQuery):
    await callback_query.message.delete()

# --- SUDO MANAGEMENT COMMANDS (Sirf Owner) ---
@app.on_message(filters.command("addsudo"))
async def add_sudo_cmd(client, message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("🚫 Only For Bot Owner.")
        
    target_id = None
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1 and message.command[1].isdigit():
        target_id = int(message.command[1])
        
    if not target_id:
        return await message.reply_text("❗ Usage: `/addsudo <User_ID>` ya kisi user ke message par reply karein.")
        
    await settings_col.update_one({"_id": "sudo_list"}, {"$addToSet": {"users": target_id}}, upsert=True)
    await message.reply_text(f"✅ User ID `{target_id}` ko Sudo Admin bana diya gaya hai.")

@app.on_message(filters.command("rmsudo"))
async def rm_sudo_cmd(client, message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("🚫 Only For Bot Owner.")
        
    target_id = None
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1 and message.command[1].isdigit():
        target_id = int(message.command[1])
        
    if not target_id:
        return await message.reply_text("❗ Usage: `/rmsudo <User_ID>` ya kisi user ke message par reply karein.")
        
    await settings_col.update_one({"_id": "sudo_list"}, {"$pull": {"users": target_id}})
    await message.reply_text(f"✅ User ID `{target_id}` ko Sudo list se hata diya gaya hai.")

@app.on_message(filters.command("sudolist"))
async def sudo_list_cmd(client, message):
    if message.from_user.id != OWNER_ID:
        return await message.reply_text("🚫 Ye command sirf Bot Owner ke liye hai.")
        
    sudos = await get_sudo_users()
    if not sudos:
        return await message.reply_text("📭 Koi Sudo Admin nahi hai.")
        
    text = "👑 **Sudo Admins List:**\n" + "\n".join(f"• `{uid}`" for uid in sudos)
    await message.reply_text(text)

# Temporary storage serial numbers ke liye
temp_group_list = {}

@app.on_message(filters.command("grouplist") & filters.user(OWNER_ID))
async def grouplist_cmd(client, message):
    global temp_group_list
    text = "📋 **Active Groups (S.No):**\n\n"
    curr = 1
    async for dialog in client.get_dialogs():
        if dialog.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            temp_group_list[curr] = dialog.chat.id # S.No save ho raha hai
            text += f"{curr}. **{dialog.chat.title}** (`{dialog.chat.id}`)\n"
            curr += 1
    await message.reply_text(text or "📭 No groups found.")

@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_handler(client, message):
    args = message.command
    is_pin = "pin" in args
    is_unpin = "unpin" in args
    
    if is_unpin:
        sent = 0
        async for dialog in client.get_dialogs():
            try: 
                await client.unpin_all_chat_messages(dialog.chat.id)
                sent += 1
            except: pass
        return await message.reply_text(f"✅ Unpinned messages in `{sent}` groups.")

    status = await message.reply_text("⏳ Broadcasting...")
    msg_to_copy = message.reply_to_message if message.reply_to_message else message
    
    count = 0
    async for dialog in client.get_dialogs():
        try:
            m = await msg_to_copy.copy(dialog.chat.id)
            if is_pin:
                await client.pin_chat_message(dialog.chat.id, m.id, disable_notification=True)
            count += 1
            await asyncio.sleep(0.3) # Protection
        except: pass
    await status.edit_text(f"📢 Sent to `{count}` chats.")

@app.on_message(filters.command(["nsfw", "getlink", "unpin", "gmsg"]) & filters.user(OWNER_ID))
async def sn_tools(client, message):
    args = message.command
    if len(args) < 2: return
    
    cmd = args[0].lower()
    sn = int(args[1]) if args[1].isdigit() else None
    chat_id = temp_group_list.get(sn) if sn else None

    if not chat_id and args[1].lower() != "all":
        return await message.reply_text("❌ SN not found. Pehle /grouplist chalao.")

    try:
        if cmd == "nsfw":
            status = args[2].lower() == "on"
            await set_nsfw_status(chat_id, status)
            await message.reply_text(f"✅ Group {sn} NSFW: {status}")
        elif cmd == "getlink":
            link = await client.export_chat_invite_link(chat_id)
            await message.reply_text(f"🔗 Link: {link}")
        elif cmd == "unpin":
            await client.unpin_all_chat_messages(chat_id)
            await message.reply_text(f"✅ Unpinned group {sn}")
    except Exception as e: await message.reply_text(f"❌ Error: {e}")

@app.on_message(filters.command("getlink") & filters.user(OWNER_ID))
async def getlink_cmd(client, message):
    if len(message.command) < 2: return await message.reply_text("❗ S.No batayein.")
    sn = int(message.command[1])
    chat_id = temp_group_list.get(sn)
    if not chat_id: return await message.reply_text("❌ Galat Serial Number.")
    try:
        link = await client.export_chat_invite_link(chat_id)
        await message.reply_text(f"🔗 Link: {link}")
    except Exception as e: await message.reply_text(f"❌ Error: {e}")

@app.on_message(filters.command("gmsg") & filters.user(OWNER_ID))
async def gmsg_cmd(client, message):
    if len(message.command) < 3: return await message.reply_text("❗ Usage: `/gmsg <S.No> <text>`")
    sn = int(message.command[1])
    text = " ".join(message.command[2:])
    chat_id = temp_group_list.get(sn)
    if chat_id:
        await client.send_message(chat_id, text)
        await message.reply_text("✅ Message bhej diya gaya.")
    else: await message.reply_text("❌ Serial Number list mein nahi hai.")

# --- NSFW TOGGLE COMMAND (Group Admins & Owner) ---
@app.on_message(filters.command("nsfw") & filters.group)
async def nsfw_toggle_cmd(client, message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Check: Owner, Sudo, ya Group Admin hona zaroori hai
    is_authorized = False
    if await is_sudo(user_id):
        is_authorized = True
    else:
        try:
            member = await client.get_chat_member(chat_id, user_id)
            if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                is_authorized = True
        except: pass

    if not is_authorized:
        return await message.reply_text("❌ **You are not an administrator.**")
        
    args = message.command[1:]
    if not args or args[0].lower() not in ["on", "off"]:
        return await message.reply_text("❗ Usage: `/nsfw on` or `/nsfw off`")
        
    new_status = args[0].lower() == "on"
    await set_nsfw_status(chat_id, new_status)
    await message.reply_text(f"✅ Filter is now **{'ON' if new_status else 'OFF'}**")

@app.on_message(filters.command("rmpack"))
async def rm_pack_cmd(client, message):
    if not await is_sudo(message.from_user.id):
        return await message.reply_text("🚫 Only For Global Admins.")
    
    pack_name = None
    args = message.command[1:]
    
    if message.reply_to_message and message.reply_to_message.sticker:
        pack_name = message.reply_to_message.sticker.set_name
    elif args:
        pack_name = args[0]
        
    if not pack_name:
        return await message.reply_text("❗ Usage: `/rmpack <pack_name>` ya sticker par reply karein.")
        
    await settings_col.update_one({"_id": "blocked_stickers"}, {"$pull": {"packs": pack_name}})
    await message.reply_text(f"✅ Sticker pack `{pack_name}` Unblock Successfully.")

@app.on_message(filters.command("stickerlist"))
async def list_pack_cmd(client, message):
    if not await is_sudo(message.from_user.id):
        return await message.reply_text("🚫 Only For Global Admins .")
    packs = await get_blocked_packs()
    if not packs:
        return await message.reply_text("📭 There is no any bloced stickerpack.")
    await message.reply_text("📝 **Blocked Sticker Packs:**\n" + "\n".join(f"• `{p}`" for p in packs))

# --- STICKER COMMANDS (Sudo Only) ---
@app.on_message(filters.command("addpack"))
async def add_pack_cmd(client, message):
    if not await is_sudo(message.from_user.id):
        return await message.reply_text("🚫 Only For Global Admins.")
    
    pack_name = None
    # Check if replied to a sticker
    if message.reply_to_message and message.reply_to_message.sticker:
        pack_name = message.reply_to_message.sticker.set_name
    # Check if pack name is given in arguments
    elif len(message.command) > 1:
        pack_name = message.command[1]
        
    if not pack_name:
        return await message.reply_text("❗ **Usage:**\n1. Reply on sticker `/addpack` likhein.\n2. Ya `/addpack <pack_name>` likhein.")
        
    await settings_col.update_one({"_id": "blocked_stickers"}, {"$addToSet": {"packs": pack_name}}, upsert=True)
    await message.reply_text(f"✅ Sticker pack `{pack_name}` block kar diya gaya hai.")

# --- ABUSE WORD COMMANDS (Sudo Only) ---
@app.on_message(filters.command("addword"))
async def add_word_cmd(client, message):
    if not await is_sudo(message.from_user.id):
        return await message.reply_text("🚫 Only For Global Admins.")
    
    word = None
    # Check if replied to a text message
    if message.reply_to_message and message.reply_to_message.text:
        # Pura message hi as a word block hoga
        word = message.reply_to_message.text.strip().lower()
    # Check if word is given in arguments
    elif len(message.command) > 1:
        word = message.command[1].lower()
        
    if not word:
        return await message.reply_text("❗ **Usage:**\n1. Reply on bad word `/addword` likhein.\n2. Ya `/addword <gaali>` likhein.")
        
    await settings_col.update_one({"_id": "blocked_words"}, {"$addToSet": {"words": word}}, upsert=True)
    await message.reply_text(f"✅ Word `{word}` unblock successfully.")

@app.on_message(filters.command("rmword"))
async def rm_word_cmd(client, message):
    if not await is_sudo(message.from_user.id):
        return await message.reply_text("🚫 Only For Global Admins.")
    
    args = message.command[1:]
    word = args[0].lower() if args else None
    
    if not word:
        return await message.reply_text("❗ Usage: `/rmword <word>`")
        
    await settings_col.update_one({"_id": "blocked_words"}, {"$pull": {"words": word}})
    await message.reply_text(f"✅ Word `{word}` unblock successfully.")

@app.on_message(filters.command("wordlist"))
async def list_word_cmd(client, message):
    if not await is_sudo(message.from_user.id):
        return await message.reply_text("🚫 Only For Global Admins.")
    words = await get_blocked_words()
    if not words:
        return await message.reply_text("📭 This is not a blocked word.")
    await message.reply_text("📝 **Blocked Words:**\n" + "\n".join(f"• `{w}`" for w in words))

# --- Helper: Message delete karne ke liye ---
async def delete_msg_later(client, chat_id, message_id, delay=5):
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except:
        pass

# --- Helper: Admins ko silent tag karne ke liye ---
async def get_silent_admin_tags(client, chat_id):
    tags = ""
    async for member in client.get_chat_members(chat_id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
        if not member.user.is_bot:
            # \u200b invisible character hai notification bhejne ke liye
            tags += f"<a href='tg://user?id={member.user.id}'>\u200b</a>"
    return tags

# ================= MASTER SCANNER (Updated with Permission Checks) =================

@app.on_message((filters.text | filters.photo | filters.sticker | filters.video | filters.animation | filters.document) & ~filters.service)
async def master_scanner(client, message):
    global current_key_index
    if not message.from_user: return
    
    # 🛑 1. DM PROTECTION: Agar DM (Private Chat) hai toh bot kuch dlt nahi karega
    if message.chat.type == enums.ChatType.PRIVATE:
        return

    # 🛑 2. ADMIN & PERMISSION CHECK: 
    # Bot group mein admin hai ya nahi, aur delete permission hai ya nahi?
    try:
        self_member = await client.get_chat_member(message.chat.id, "me")
        
        # Agar bot admin nahi hai, toh bilkul silent rahega
        if self_member.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return 
            
        # Agar admin hai par delete permission nahi hai
        if not self_member.privileges.can_delete_messages:
            # Ye message 1 baar hi dikhega jab bot scan karne ki koshish karega
            await message.reply_text("❗ I have no 'Delete Messages' permission to perform moderation.")
            return

    except Exception as e:
        print(f"Error checking bot permissions: {e}")
        return

    # --- Yahan se purana scanning logic shuru hoga ---
    
    # 3. Total Scans Counter
    await update_stat("total_scans") 

    # 🌍 Global & Local Toggle Check
    is_global_nsfw_on = await get_global_nsfw()
    if not is_global_nsfw_on: return

    is_nsfw_on = await get_nsfw_status(message.chat.id)
    if not is_nsfw_on: return

    # Admin silent tags report ke liye
    admin_tags = await get_silent_admin_tags(client, message.chat.id)
    text_to_check = message.text or message.caption or ""

    # 🛑 Step 4: Abuse Word Check
    if text_to_check and not text_to_check.startswith("/"):
        text_lower = text_to_check.lower()
        blocked_words = await get_blocked_words()
        found_word = next((w for w in blocked_words if w in text_lower), None)
        
        if found_word:
            try:
                await message.delete()
                await update_stat("abuse_blocked")
                warn_msg = await client.send_message(
                    chat_id=message.chat.id,
                    text=f"🤬 **Abuse Deleted:** {message.from_user.mention}\n⚠️ **Word:** `{found_word}`\n⏱️ _Deleting in 5s..._{admin_tags}",
                    parse_mode=enums.ParseMode.HTML
                )
                asyncio.create_task(delete_msg_later(client, message.chat.id, warn_msg.id))
            except Exception: pass
            return 

    # 🛑 Step 5: Blocked Sticker Pack Check
    if message.sticker and message.sticker.set_name:
        blocked_packs = await get_blocked_packs()
        if message.sticker.set_name in blocked_packs:
            try:
                await message.delete()
                await update_stat("nsfw_blocked")
                warn_msg = await client.send_message(
                    chat_id=message.chat.id,
                    text=f"🚫 **Blocked Sticker Deleted!**\n👤 **User:** {message.from_user.mention}\n📦 **Pack:** `{message.sticker.set_name}`\n⏱️ _Deleting in 5s..._{admin_tags}",
                    parse_mode=enums.ParseMode.HTML
                )
                asyncio.create_task(delete_msg_later(client, message.chat.id, warn_msg.id))
            except Exception: pass
            return 

    # 🛑 Step 6: AI Media Check (Photo/Video/GIF)
    # [Aapka Sightengine wala logic yahan continue rahega...]

    # 🛑 Step 6: AI Media Check (Universal)
    file_id = None
    if message.photo: file_id = message.photo.file_id 
    elif message.sticker: file_id = message.sticker.thumbs[0].file_id if message.sticker.thumbs else message.sticker.file_id
    elif message.video: file_id = message.video.thumbs[0].file_id if message.video.thumbs else message.video.file_id
    elif message.animation: file_id = message.animation.thumbs[0].file_id if message.animation.thumbs else message.animation.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('image/'):
        file_id = message.document.file_id

    if file_id:
        try:
            # Telegram se URL nikalna
            api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
            file_path = requests.get(api_url).json()["result"]["file_path"]
            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            
            # Key Rotation ke saath Scan
            key = SIGHTENGINE_KEYS[current_key_index]
            r = requests.get("https://api.sightengine.com/1.0/check.json", params={
                'url': file_url, 'models': 'nudity-2.0,wad,offensive,gore',
                'api_user': key["user"], 'api_secret': key["secret"]
            }).json()

            if r.get('status') == 'success':
                nude = r.get('nudity', {}).get('none', 1.0) < 0.5
                gore = r.get('gore', {}).get('prob', 0.0) > 0.5
                if nude or gore:
                    await message.delete()
                    await update_stat("nsfw_block")
                    
                    warn_msg = await client.send_message(
                        chat_id=message.chat.id,
                        text=f"🚨 **NSFW Content Deleted** 🚨\n\n"
                             f"👤 **User:** {message.from_user.mention}\n"
                             f"⏱️ _Deleting in 5s..._{admin_tags}",
                        parse_mode=enums.ParseMode.HTML
                    )
                    asyncio.create_task(delete_msg_later(client, message.chat.id, warn_msg.id))
            elif 'limit' in str(r).lower():
                current_key_index = (current_key_index + 1) % len(SIGHTENGINE_KEYS)
        except Exception: pass

app = Client("nsfw_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ================= EXECUTION =================
if __name__ == "__main__":
    print(f"🤖 Starting {BOT_DISPLAY_NAME}...")
    
    # 🟢 Server ko background mein start karein
    keep_alive() 
    
    # Bot start karein
    app.run()
