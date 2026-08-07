# =============================================================================
# COMPLETE TELEGRAM SECURITY BOT – NSFW + BLOCKLISTS + ALL FEATURES
# (Commands removed: channel, antibot, config, edit, delay, allow, unallow, allowlist)
# Import error for ChatMemberStatus fixed – using strings instead.
# =============================================================================

import os
import re
import html
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime

import pytz
import pymongo
import requests
from PIL import Image
from flask import Flask
from threading import Thread

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup,
    ReactionTypeEmoji)

from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ChatMemberHandler, filters, ContextTypes, TypeHandler, ApplicationHandlerStop
)

# ===================== CONFIGURATION =====================
TOKEN = os.environ.get("")
MONGO_URL = os.environ.get(".")
admin_env = os.environ.get(".")
ADMIN_IDS = [int(x.strip()) for x in admin_env.split(",") if x.strip().isdigit()]
IST = pytz.timezone('Asia/Kolkata')

# Sightengine API keys (set in environment)
SIGHTENGINE_KEYS = [
    {"user": "1263088480", "secret": "Cu9GYpvw5hqauXGr9niPrswzbh35mbqK"},
    {"user": "1263088481", "secret": "Cu9GYpvw5hqauXGr9niPrswzbh35mbqL"},
    {"user": "1263088482", "secret": "Cu9GYpvw5hqauXGr9niPrswzbh35mbqM"}
]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Bulk delete queue
BULK_DELETE_QUEUE = defaultdict(list)

# ===================== FLASK KEEP‑ALIVE (for Render) =====================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ===================== DATABASE CLASS =====================
class PersistentDB:
    def __init__(self):
        self.client = pymongo.MongoClient(MONGO_URL)
        self.db = self.client["shield_bot_db"]
        self.group_config = self.db["group_config"]
        self.allowlist = self.db["allowlist"]
        self.allowlist.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
        self.warnings = self.db["warnings"]
        self.users = self.db["users"]
        self.groups = self.db["groups"]
        self.global_stats = self.db["global_stats"]
        self.sudos = self.db["sudos"]
        self.blocked_stickers = self.db["blocked_stickers"]
        self.blocked_words = self.db["blocked_words"]
        self.local_blocked_words = self.db["local_blocked_words"]
        self.local_blocked_stickers = self.db["local_blocked_stickers"]
        self.gbans = self.db["gbans"]
        self._init_stats()

    def _init_stats(self):
        stats = self.global_stats.find_one({"_id": 1})
        if not stats:
            self.global_stats.insert_one({
                "_id": 1, "scanned": 0, "bio_caught": 0,
                "media_deleted": 0, "warnings_issued": 0,
                "nsfw_blocked": 0, "abuse_caught": 0,
                "bot_start_time": datetime.now(IST).timestamp()
            })

    def update_stat(self, column):
        self.global_stats.update_one({"_id": 1}, {"$inc": {column: 1}})

    def get_global_stats(self):
        stats = self.global_stats.find_one({"_id": 1})
        return (stats.get("scanned", 0), stats.get("bio_caught", 0),
                stats.get("media_deleted", 0), stats.get("warnings_issued", 0),
                stats.get("nsfw_blocked", 0), stats.get("abuse_caught", 0),
                stats.get("bot_start_time", datetime.now(IST).timestamp()))

    # ---------- Group config ----------
    def get_config(self, chat_id):
        s = self.group_config.find_one({"_id": chat_id})
        if s:
            return (s.get("delay_minutes", 1), s.get("warn_limit", 3),
                    s.get("action", "mute"), s.get("copyright_enabled", 0),
                    s.get("anti_channel", 1), s.get("nsfw_enabled", 1),
                    s.get("anti_bot", 1), s.get("bio_check", 1))
        else:
            return (1, 3, "mute", 0, 1, 1, 1, 1)

    def set_warn_limit(self, chat_id, limit):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"warn_limit": limit}}, upsert=True)

    def set_action(self, chat_id, action):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"action": action}}, upsert=True)

    def set_nsfw(self, chat_id, enabled):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"nsfw_enabled": 1 if enabled else 0}}, upsert=True)

    def set_bio_check(self, chat_id, enabled):
        self.group_config.update_one({"_id": chat_id}, {"$set": {"bio_check": 1 if enabled else 0}}, upsert=True)

    # ---------- Global NSFW ----------
    def set_global_nsfw(self, enabled):
        self.global_stats.update_one({"_id": 1}, {"$set": {"global_nsfw": 1 if enabled else 0}}, upsert=True)

    def get_global_nsfw(self):
        stats = self.global_stats.find_one({"_id": 1})
        return stats.get("global_nsfw", 1) == 1

    # ---------- Allowlist (still needed for internal checks, but no commands) ----------
    def add_to_allowlist(self, chat_id, user_id):
        if not self.is_allowed(chat_id, user_id):
            self.allowlist.insert_one({"chat_id": chat_id, "user_id": user_id})

    def remove_from_allowlist(self, chat_id, user_id):
        return self.allowlist.delete_one({"chat_id": chat_id, "user_id": user_id}).deleted_count > 0

    def is_allowed(self, chat_id, user_id):
        return self.allowlist.find_one({"chat_id": chat_id, "user_id": user_id}) is not None

    def get_allowlist(self, chat_id):
        return [doc["user_id"] for doc in self.allowlist.find({"chat_id": chat_id})]

    # ---------- Users & Groups ----------
    def add_user(self, user_or_id):
        user_id = user_or_id.id if hasattr(user_or_id, "id") else int(user_or_id)
        payload = {"_id": user_id}
        if hasattr(user_or_id, "username"):
            payload.update({
                "username": (user_or_id.username or "").strip(),
                "first_name": (user_or_id.first_name or "").strip(),
                "full_name": (user_or_id.full_name or user_or_id.first_name or "").strip(),
                "username_lc": (user_or_id.username or "").lower(),
                "first_name_lc": (user_or_id.first_name or "").lower(),
                "full_name_lc": (user_or_id.full_name or user_or_id.first_name or "").lower(),
            })
        self.users.update_one({"_id": user_id}, {"$set": payload}, upsert=True)

    def find_user_by_name_or_username(self, identifier):
        q = (identifier or "").strip().lstrip('@').lower()
        if not q:
            return None
        exact = self.users.find_one({
            "$or": [
                {"username_lc": q},
                {"first_name_lc": q},
                {"full_name_lc": q},
            ]
        })
        if exact:
            return exact
        rx = f"^{re.escape(q)}"
        matches = list(self.users.find({
            "$or": [
                {"username_lc": {"$regex": rx}},
                {"first_name_lc": {"$regex": rx}},
                {"full_name_lc": {"$regex": rx}},
            ]
        }).limit(2))
        return matches[0] if len(matches) == 1 else None

    def add_group(self, chat_id, title="Unknown Group"):
        self.groups.update_one({"_id": chat_id}, {"$set": {"title": title}}, upsert=True)

    def get_groups(self):
        return [(g["_id"], g.get("title", "Unknown Group")) for g in self.groups.find()]

    def remove_group(self, chat_id):
        self.groups.delete_one({"_id": chat_id})
        self.group_config.delete_one({"_id": chat_id})

    def get_all_targets(self):
        users = [u["_id"] for u in self.users.find()]
        groups = [g["_id"] for g in self.groups.find()]
        return list(set(users + groups))

    # ---------- Warnings ----------
    def add_warning(self, chat_id, user_id):
        key = f"{chat_id}_{user_id}"
        w = self.warnings.find_one_and_update(
            {"_id": key},
            {"$inc": {"count": 1}, "$set": {"chat_id": chat_id, "user_id": user_id}},
            upsert=True,
            return_document=pymongo.ReturnDocument.AFTER
        )
        return w["count"]

    def reset_warnings(self, chat_id, user_id):
        key = f"{chat_id}_{user_id}"
        self.warnings.delete_one({"_id": key})

    def decrease_warning(self, chat_id, user_id):
        key = f"{chat_id}_{user_id}"
        row = self.warnings.find_one({"_id": key})
        if row and row["count"] > 0:
            new_count = row["count"] - 1
            if new_count <= 0:
                self.warnings.delete_one({"_id": key})
            else:
                self.warnings.update_one({"_id": key}, {"$set": {"count": new_count}})

    # ---------- Sudo ----------
    def is_sudo(self, user_id):
        return self.sudos.find_one({"_id": user_id}) is not None

    def add_sudo(self, user_id):
        self.sudos.update_one({"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True)

    def remove_sudo(self, user_id):
        if self.is_sudo(user_id):
            self.sudos.delete_one({"_id": user_id})
            return True
        return False

    def get_sudos(self):
        return [u["_id"] for u in self.sudos.find()]

    # ---------- GBAN ----------
    def add_gban(self, user_id, reason="No reason"):
        self.gbans.update_one({"_id": user_id}, {"$set": {"reason": reason}}, upsert=True)

    def remove_gban(self, user_id):
        return self.gbans.delete_one({"_id": user_id}).deleted_count > 0

    def is_gbanned(self, user_id):
        row = self.gbans.find_one({"_id": user_id})
        if row:
            return True, row.get("reason", "No reason")
        return False, ""

    def get_gbans(self):
        return [(u["_id"], u.get("reason", "No reason")) for u in self.gbans.find()]

db = PersistentDB()

# ===================== HELPER FUNCTIONS =====================
async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        return True
    try:
        chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return chat_member.status in ['administrator', 'creator']
    except:
        return False

def has_link(text):
    if not text:
        return False
    link_patterns = [r'http[s]?://\S+', r'www\.\S+', r't\.me/\S+', r'\S+\.(com|org|net|in|co|io|xyz|me|info)\b']
    for pattern in link_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

async def extract_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return None, None, "No message found."
    chat_id = message.chat_id
    args = context.args or []
    base_reason = " ".join(args[1:]).strip() if len(args) > 1 else "No reason"

    if message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        reason = " ".join(args) if args else "No reason"
        return user.id, user.first_name, reason

    if not args:
        return None, None, "❗ Please reply to someone, or provide a User ID/Username/Name."

    async def _resolve_from_identifier(raw_identifier: str):
        raw_identifier = (raw_identifier or "").strip().strip(",.;:!?")
        if not raw_identifier:
            return None, None
        if "t.me/" in raw_identifier.lower():
            raw_identifier = raw_identifier.rstrip('/').split('/')[-1]
        if raw_identifier.startswith('@'):
            raw_identifier = raw_identifier.split()[0].rstrip(",.;:!?)]}\"'")
        if raw_identifier.lstrip('-').isdigit():
            user_id = int(raw_identifier)
            try:
                chat_user = await context.bot.get_chat(user_id)
                return user_id, chat_user.first_name or "User"
            except Exception:
                return user_id, str(user_id)
        username_key = raw_identifier.lstrip('@')
        cached_user = db.find_user_by_name_or_username(username_key)
        if cached_user:
            return cached_user.get("_id"), cached_user.get("full_name") or cached_user.get("first_name") or "User"
        candidate = f"@{username_key}" if username_key else ""
        if len(candidate) > 1:
            try:
                chat_user = await context.bot.get_chat(candidate)
                return chat_user.id, chat_user.first_name or "User"
            except Exception:
                pass
        cached_user = db.find_user_by_name_or_username(raw_identifier)
        if cached_user:
            return cached_user.get("_id"), cached_user.get("full_name") or cached_user.get("first_name") or "User"
        if update.effective_chat and update.effective_chat.type in ['group', 'supergroup']:
            try:
                admins = await context.bot.get_chat_administrators(chat_id)
                q = raw_identifier.lstrip('@').lower()
                for admin in admins:
                    u = admin.user
                    if not u:
                        continue
                    admin_username = (u.username or "").lower()
                    admin_first = (u.first_name or "").lower()
                    admin_full = (u.full_name or u.first_name or "").lower()
                    if q in {admin_username, admin_first, admin_full}:
                        return u.id, u.full_name or u.first_name or "User"
            except Exception:
                pass
        return None, None

    entities = message.entities or message.caption_entities
    if entities:
        for entity in entities:
            if entity.type == 'text_mention':
                return entity.user.id, entity.user.first_name, base_reason
            if entity.type == 'mention' and message.text:
                entity_username = message.text[entity.offset: entity.offset + entity.length]
    # ...
                if entity_username:
                    resolved_id, resolved_name = await _resolve_from_identifier(entity_username)
                    if resolved_id:
                        return resolved_id, resolved_name, base_reason

    first_arg = (args[0] or "").strip() if args else ""
    if first_arg.startswith('@') or 't.me/' in first_arg.lower():
        resolved_id, resolved_name = await _resolve_from_identifier(first_arg)
        if resolved_id:
            return resolved_id, resolved_name, base_reason

    for split_at in range(len(args), 0, -1):
        candidate_identifier = " ".join(args[:split_at]).strip()
        reason = " ".join(args[split_at:]).strip() or "No reason"
        resolved_id, resolved_name = await _resolve_from_identifier(candidate_identifier)
        if resolved_id:
            return resolved_id, resolved_name, reason

    return None, None, "❌ User not found. Provide a valid ID, username, or reply."

async def store_user_info(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_user = await context.bot.get_chat(user_id)
        db.add_user(chat_user)
    except Exception:
        db.add_user(user_id)

# ===================== NSFW + BLOCKLIST CLASS =====================
class NSFWBlocklist:
    def __init__(self, db, sightengine_keys=None):
        self.db = db
        self.sightengine_keys = sightengine_keys or SIGHTENGINE_KEYS

    # ---------- Wrappers for DB methods ----------
    def set_global_nsfw(self, enabled):
        self.db.set_global_nsfw(enabled)

    def get_global_nsfw(self):
        return self.db.get_global_nsfw()

    def set_nsfw(self, chat_id, enabled):
        self.db.set_nsfw(chat_id, enabled)

    def get_nsfw(self, chat_id):
        return self.db.get_config(chat_id)[5] == 1

    def add_blocked_sticker(self, set_name):
        self.db.blocked_stickers.update_one({"_id": set_name}, {"$set": {"_id": set_name}}, upsert=True)

    def remove_blocked_sticker(self, set_name):
        return self.db.blocked_stickers.delete_one({"_id": set_name}).deleted_count > 0

    def get_blocked_stickers(self):
        return [s["_id"] for s in self.db.blocked_stickers.find()]

    def add_blocked_word(self, word):
        word = word.lower()
        self.db.blocked_words.update_one({"_id": word}, {"$set": {"_id": word}}, upsert=True)

    def remove_blocked_word(self, word):
        word = word.lower()
        return self.db.blocked_words.delete_one({"_id": word}).deleted_count > 0

    def get_blocked_words(self):
        return [w["_id"] for w in self.db.blocked_words.find()]

    def add_local_word(self, chat_id, word):
        word = word.lower()
        self.db.local_blocked_words.update_one(
            {"chat_id": chat_id, "word": word},
            {"$set": {"chat_id": chat_id, "word": word}},
            upsert=True
        )

    def remove_local_word(self, chat_id, word):
        word = word.lower()
        return self.db.local_blocked_words.delete_one({"chat_id": chat_id, "word": word}).deleted_count > 0

    def get_local_words(self, chat_id):
        return [w["word"] for w in self.db.local_blocked_words.find({"chat_id": chat_id})]

    def add_local_sticker(self, chat_id, set_name):
        self.db.local_blocked_stickers.update_one(
            {"chat_id": chat_id, "set_name": set_name},
            {"$set": {"chat_id": chat_id, "set_name": set_name}},
            upsert=True
        )

    def remove_local_sticker(self, chat_id, set_name):
        return self.db.local_blocked_stickers.delete_one({"chat_id": chat_id, "set_name": set_name}).deleted_count > 0

    def get_local_stickers(self, chat_id):
        return [s["set_name"] for s in self.db.local_blocked_stickers.find({"chat_id": chat_id})]

    # ---------- NSFW detection ----------
    async def check_image_nsfw_api(self, file_path: str) -> bool:
        try:
            img = Image.open(file_path)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(file_path, 'JPEG')
        except Exception as e:
            logger.warning(f"Image conversion skipped: {e}")

        for creds in self.sightengine_keys:
            api_user = creds.get("user")
            api_secret = creds.get("secret")
            if not api_user or not api_secret:
                continue
            try:
                def call_api():
                    with open(file_path, 'rb') as f:
                        files = {'media': f}
                        params = {
                            'models': 'nudity-2.0',
                            'api_user': api_user,
                            'api_secret': api_secret
                        }
                        return requests.post('https://api.sightengine.com/1.0/check.json',
                                             files=files, data=params, timeout=20)
                response = await asyncio.to_thread(call_api)
                result = response.json()
                if result.get('status') == 'success':
                    nudity = result.get('nudity', {})
                    if (nudity.get('sexual_activity', 0) > 0.45 or
                        nudity.get('sexual_display', 0) > 0.45 or
                        nudity.get('erotica', 0) > 0.45):
                        return True
                    return False
                if result.get('error', {}).get('type') == 'limit_reached':
                    logger.warning(f"Sightengine key {api_user} limit reached. Trying next...")
                    continue
                else:
                    logger.error(f"Sightengine API error: {result}")
                    continue
            except Exception as e:
                logger.error(f"Sightengine exception with key {api_user}: {e}")
                continue
        logger.error("All Sightengine keys failed or out of limits.")
        return False

    # ---------- Content filter processor ----------
    async def process_content_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not update.message:
            return False

        user = update.message.from_user
        chat_id = update.effective_chat.id
        msg_text = update.message.text or update.message.caption

        config = self.db.get_config(chat_id)
        warn_limit = config[1]
        action = config[2]
        nsfw_enabled = config[5]

        # 1. Blocked words (global + local)
        blocked_word_found = False
        caught_word = ""
        if nsfw_enabled and msg_text:
            all_blocked_words = self.get_blocked_words() + self.get_local_words(chat_id)
            for word in all_blocked_words:
                if re.search(r'\b' + re.escape(word) + r'\b', msg_text.lower()):
                    blocked_word_found = True
                    caught_word = word
                    break

        if blocked_word_found:
            self.db.update_stat('abuse_caught')
            try:
                await update.message.delete()
            except Exception as e:
                if "can't be deleted" in str(e).lower() or "not enough rights" in str(e).lower():
                    try:
                        await context.bot.send_message(chat_id, "⚠️ **Please give me delete messages permission.**", parse_mode='Markdown')
                    except:
                        pass
            current_time = asyncio.get_event_loop().time()
            if current_time - context.chat_data.get(f"last_word_alert_{user.id}", 0) > 10:
                context.chat_data[f"last_word_alert_{user.id}"] = current_time
                try:
                    alert_msg = await context.bot.send_message(
                        chat_id,
                        f"🚫 {user.mention_html()}, blocked word: <b>{html.escape(caught_word)}</b>",
                        parse_mode='HTML'
                    )
                    context.job_queue.run_once(self._delete_msg_job, 3, chat_id=chat_id, data=alert_msg.message_id)
                except Exception:
                    pass
            return True

        # 2. Blocked stickers (global + local)
        blocked_sticker_found = False
        if (not blocked_word_found and nsfw_enabled and
            update.message.sticker and update.message.sticker.set_name):
            all_blocked_stickers = self.get_blocked_stickers() + self.get_local_stickers(chat_id)
            if update.message.sticker.set_name in all_blocked_stickers:
                blocked_sticker_found = True

        if blocked_sticker_found:
            self.db.update_stat('nsfw_blocked')
            try:
                await update.message.delete()
            except Exception:
                pass
            current_time = asyncio.get_event_loop().time()
            if current_time - context.chat_data.get(f"last_sticker_alert_{user.id}", 0) > 10:
                context.chat_data[f"last_sticker_alert_{user.id}"] = current_time
                admin_tags = "".join([f'<a href="tg://user?id={aid}">&#8203;</a>' for aid in ADMIN_IDS])
                admin_alert = f"🚨 <b>Blocked Sticker & Deleted</b>\n\n👤 <b>Sender:</b> {user.mention_html()}\n{admin_tags}"
                try:
                    alert_msg = await context.bot.send_message(chat_id, admin_alert, parse_mode='HTML', disable_notification=True)
                    context.job_queue.run_once(self._delete_msg_job, 30, chat_id=chat_id, data=alert_msg.message_id)
                except Exception:
                    pass
            return True

        # 3. NSFW media detection
        if nsfw_enabled:
            file_id = None
            temp_file_path = f"temp_nsfw_{chat_id}_{update.message.message_id}.jpg"

            if update.message.photo:
                file_id = update.message.photo[-1].file_id
            elif update.message.sticker:
                if (not getattr(update.message.sticker, 'is_animated', False) and
                    not getattr(update.message.sticker, 'is_video', False)):
                    file_id = update.message.sticker.file_id
                elif update.message.sticker.thumbnail:
                    file_id = update.message.sticker.thumbnail.file_id
            elif update.message.video and update.message.video.thumbnail:
                file_id = update.message.video.thumbnail.file_id
            elif update.message.document and update.message.document.thumbnail:
                file_id = update.message.document.thumbnail.file_id
            elif update.message.animation and update.message.animation.thumbnail:
                file_id = update.message.animation.thumbnail.file_id

            if file_id:
                try:
                    file = await context.bot.get_file(file_id)
                    await file.download_to_drive(temp_file_path)
                    is_explicit = await self.check_image_nsfw_api(temp_file_path)

                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

                    if is_explicit:
                        self.db.update_stat('nsfw_blocked')
                        try:
                            await update.message.delete()
                        except Exception:
                            pass
                        current_time = asyncio.get_event_loop().time()
                        if current_time - context.chat_data.get(f"last_nsfw_alert_{user.id}", 0) > 10:
                            context.chat_data[f"last_nsfw_alert_{user.id}"] = current_time
                            admin_tags = "".join([f'<a href="tg://user?id={aid}">&#8203;</a>' for aid in ADMIN_IDS])
                            admin_alert = f"🚨 <b>NSFW Content Detected</b>\n\n👤 <b>Sender:</b> {user.mention_html()}{admin_tags}"
                            try:
                                alert_msg = await context.bot.send_message(chat_id, admin_alert, parse_mode='HTML', disable_notification=True)
                                context.job_queue.run_once(self._delete_msg_job, 30, chat_id=chat_id, data=alert_msg.message_id)
                            except Exception:
                                pass
                        return True
                except Exception as e:
                    logger.error(f"NSFW Processing Error: {e}")
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)

        return False

    @staticmethod
    async def _delete_msg_job(context: ContextTypes.DEFAULT_TYPE):
        chat_id = context.job.chat_id
        message_id = context.job.data
        try:
            await context.bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    # ---------- Command Handlers (only NSFW & blocklists) ----------
    async def nsfw_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat = update.effective_chat
        args = context.args

        if not args:
            if chat.type in ['group', 'supergroup']:
                is_admin = await is_user_admin(update, context) or db.is_sudo(user_id) or user_id in ADMIN_IDS
                if not is_admin:
                    await update.message.reply_text("❌ You are not an admin.")
                    return
                local_enabled = self.get_nsfw(chat.id)
                global_enabled = self.get_global_nsfw()
                local_status = "🟢 ON" if local_enabled else "🔴 OFF"
                global_status = "🟢 ON" if global_enabled else "🔴 OFF"
                text = (f"🔞 **NSFW Filter Status**\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📌 **This Group:** {local_status}\n"
                        f"🌍 **Global Setting:** {global_status}\n\n"
                        f"_Admins can toggle local with_ `/nsfw on/off`")
                await update.message.reply_text(text, parse_mode='Markdown')
                return
            else:
                global_enabled = self.get_global_nsfw()
                await update.message.reply_text(f"🌍 **Global NSFW Setting:** {'🟢 ON' if global_enabled else '🔴 OFF'}", parse_mode='Markdown')
                return

        if args[0].lower() == "all" and len(args) == 2:
            if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
                await update.message.reply_text("❌ Only Owner and Sudo Admins can use global control.")
                return
            state_str = args[1].lower()
            if state_str not in ['on', 'off']:
                await update.message.reply_text("❗ **Usage:** `/nsfw all on` or `off`", parse_mode='Markdown')
                return
            state = (state_str == "on")
            self.set_global_nsfw(state)
            groups = db.get_groups()
            for chat_id, _ in groups:
                self.set_nsfw(chat_id, state)
            await update.message.reply_text(
                f"✅ **Global NSFW Update:** Set to **{'ENABLED' if state else 'DISABLED'}** for ALL {len(groups)} groups.",
                parse_mode='Markdown'
            )
            return

        if len(args) == 2 and args[0].isdigit():
            if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
                await update.message.reply_text("❌ Only Owner and Sudo Admins can use remote control.")
                return
            s_no = int(args[0])
            state_str = args[1].lower()
            groups = db.get_groups()
            if s_no < 1 or s_no > len(groups):
                await update.message.reply_text("❌ Invalid Serial Number.")
                return
            target_chat_id = groups[s_no - 1][0]
            target_title = groups[s_no - 1][1]
            state = (state_str == "on")
            self.set_nsfw(target_chat_id, state)
            await update.message.reply_text(
                f"✅ **NSFW Filter** is now **{'ENABLED' if state else 'DISABLED'}** for group:\n📍 **{html.escape(target_title)}**",
                parse_mode='HTML'
            )
            return

        if chat.type not in ['group', 'supergroup']:
            await update.message.reply_text("❌ This command works in groups only.")
            return
        if not await is_user_admin(update, context) and not db.is_sudo(user_id):
            await update.message.reply_text("❌ You are not an admin in this group.")
            return
        state_str = args[0].lower()
        if state_str not in ['on', 'off']:
            await update.message.reply_text("❗ **Usage:** `/nsfw on` or `/nsfw off`", parse_mode='Markdown')
            return
        state = (state_str == "on")
        self.set_nsfw(chat.id, state)
        await update.message.reply_text(
            f"🔞 **NSFW Filter** is now **{'ENABLED' if state else 'DISABLED'}** in this group.",
            parse_mode='Markdown'
        )

    async def addsticker_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        set_name = None
        if context.args:
            set_name = context.args[0]
        elif update.message.reply_to_message and update.message.reply_to_message.sticker:
            set_name = update.message.reply_to_message.sticker.set_name
        if not set_name:
            await update.message.reply_text("❗ **Usage:** Reply to a sticker, or type `/addsticker <pack_name>`", parse_mode='Markdown')
            return
        self.add_blocked_sticker(set_name)
        await update.message.reply_text(f"✅ Sticker pack `{set_name}` blocked globally!", parse_mode='Markdown')

    async def rmsticker_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        set_name = None
        if context.args:
            set_name = context.args[0]
        elif update.message.reply_to_message and update.message.reply_to_message.sticker:
            set_name = update.message.reply_to_message.sticker.set_name
        if not set_name:
            await update.message.reply_text("❗ **Usage:** Reply to a sticker, or type `/rmsticker <pack_name>`", parse_mode='Markdown')
            return
        if self.remove_blocked_sticker(set_name):
            await update.message.reply_text(f"✅ Sticker pack `{set_name}` unblocked.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Pack not found in blocklist.")

    async def stickerlist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        packs = self.get_blocked_stickers()
        if not packs:
            await update.message.reply_text("📭 Blocked sticker list is empty.")
            return
        text = "🚫 **Blocked Sticker Packs:**\n\n" + "\n".join([f"• `{p}`" for p in packs])
        await update.message.reply_text(text, parse_mode='Markdown')

    async def addword_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        word = ""
        if context.args:
            word = " ".join(context.args).lower()
        elif update.message.reply_to_message and update.message.reply_to_message.text:
            word = update.message.reply_to_message.text.strip().lower()
        if not word:
            await update.message.reply_text("❗ **Usage:** Reply to a text message, or type `/addword <word>`", parse_mode='Markdown')
            return
        self.add_blocked_word(word)
        await update.message.reply_text(f"✅ Word/Text `{word}` blocked globally!", parse_mode='Markdown')

    async def rmword_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        word = ""
        if context.args:
            word = " ".join(context.args).lower()
        elif update.message.reply_to_message and update.message.reply_to_message.text:
            word = update.message.reply_to_message.text.strip().lower()
        if not word:
            await update.message.reply_text("❗ **Usage:** Reply to a text message, or type `/rmword <word>`", parse_mode='Markdown')
            return
        if self.remove_blocked_word(word):
            await update.message.reply_text(f"✅ Word/Text `{word}` unblocked.", parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Word not found in blocklist.")

    async def wordlist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        words = self.get_blocked_words()
        if not words:
            await update.message.reply_text("📭 Blocked word list is empty.")
            return
        text = "🚫 **Blocked Words:**\n\n" + "\n".join([f"• `{w}`" for w in words])
        await update.message.reply_text(text, parse_mode='Markdown')

    # Local blocklist commands
    async def blockword_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == 'private':
            await update.message.reply_text("❌ This command only works in groups.")
            return
        chat_id = update.effective_chat.id
        if update.message:
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)

        if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
            msg = await update.message.reply_text("❌ You are not an admin.")
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return

        word = ""
        is_reply = False
        if context.args:
            word = " ".join(context.args).lower()
        elif update.message.reply_to_message and update.message.reply_to_message.text:
            word = update.message.reply_to_message.text.strip().lower()
            is_reply = True

        if not word:
            msg = await update.message.reply_text("❗ **Usage:** Reply to a message, or type `/blockword <word>`", parse_mode='Markdown')
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return

        self.add_local_word(chat_id, word)
        msg = await update.message.reply_text(f"✅ Word `{word}` blocked **only in this group**.", parse_mode='Markdown')
        context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
        if is_reply:
            try:
                await update.message.reply_to_message.delete()
            except Exception:
                pass

    async def unblockword_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == 'private':
            await update.message.reply_text("❌ This command only works in groups.")
            return
        chat_id = update.effective_chat.id
        if update.message:
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)
        if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
            msg = await update.message.reply_text("❌ You are not an admin.")
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        word = ""
        if context.args:
            word = " ".join(context.args).lower()
        elif update.message.reply_to_message and update.message.reply_to_message.text:
            word = update.message.reply_to_message.text.strip().lower()
        if not word:
            msg = await update.message.reply_text("❗ **Usage:** Reply to a message, or type `/unblockword <word>`", parse_mode='Markdown')
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        if self.remove_local_word(chat_id, word):
            msg = await update.message.reply_text(f"✅ Word `{word}` is now allowed in this group.", parse_mode='Markdown')
        else:
            msg = await update.message.reply_text("❌ Word not found in this group's blocklist.")
        context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)

    async def blocksticker_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == 'private':
            await update.message.reply_text("❌ This command only works in groups.")
            return
        chat_id = update.effective_chat.id
        if update.message:
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)
        if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
            msg = await update.message.reply_text("❌ You are not an admin.")
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        set_name = None
        if context.args:
            set_name = context.args[0]
        elif update.message.reply_to_message and update.message.reply_to_message.sticker:
            set_name = update.message.reply_to_message.sticker.set_name
        if not set_name:
            msg = await update.message.reply_text("❗ **Usage:** Reply to a sticker, or type `/blocksticker <pack_name>`", parse_mode='Markdown')
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        self.add_local_sticker(chat_id, set_name)
        msg = await update.message.reply_text(f"✅ Sticker pack `{set_name}` blocked in this group.", parse_mode='Markdown')
        context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)

    async def unblocksticker_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == 'private':
            await update.message.reply_text("❌ This command only works in groups.")
            return
        chat_id = update.effective_chat.id
        if update.message:
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)
        if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
            msg = await update.message.reply_text("❌ You are not an admin.")
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        set_name = None
        if context.args:
            set_name = context.args[0]
        elif update.message.reply_to_message and update.message.reply_to_message.sticker:
            set_name = update.message.reply_to_message.sticker.set_name
        if not set_name:
            msg = await update.message.reply_text("❗ **Usage:** Reply to a sticker, or type `/unblocksticker <pack_name>`", parse_mode='Markdown')
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        if self.remove_local_sticker(chat_id, set_name):
            msg = await update.message.reply_text(f"✅ Sticker pack `{set_name}` is now allowed in this group.", parse_mode='Markdown')
        else:
            msg = await update.message.reply_text("❌ Sticker pack not found in this group's blocklist.")
        context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)

    async def listlocal_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type == 'private':
            await update.message.reply_text("❌ This command only works in groups.")
            return
        chat_id = update.effective_chat.id
        if update.message:
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=update.message.message_id)
        if not await is_user_admin(update, context) and not db.is_sudo(update.effective_user.id):
            msg = await update.message.reply_text("❌ You are not an admin.")
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        words = self.get_local_words(chat_id)
        stickers = self.get_local_stickers(chat_id)
        if not words and not stickers:
            msg = await update.message.reply_text("📭 This group's custom blocklist is empty.")
            context.job_queue.run_once(self._delete_msg_job, 5, chat_id=chat_id, data=msg.message_id)
            return
        text = f"⚙️ **Local Blocklist for {update.effective_chat.title}**\n\n"
        if words:
            text += "🚫 **Blocked Words:**\n" + "\n".join([f"• `{w}`" for w in words]) + "\n\n"
        if stickers:
            text += "🚫 **Blocked Stickers:**\n" + "\n".join([f"• `{s}`" for s in stickers])
        msg = await update.message.reply_text(text, parse_mode='Markdown')
        context.job_queue.run_once(self._delete_msg_job, 15, chat_id=chat_id, data=msg.message_id)

    def _is_authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id
        if user_id in ADMIN_IDS or db.is_sudo(user_id):
            return True
        asyncio.create_task(update.message.reply_text("❌ Only Owner and Sudo Admins can use this command."))
        return False

    def register_handlers(self, application):
        application.add_handler(CommandHandler("nsfw", self.nsfw_command))
        application.add_handler(CommandHandler("addsticker", self.addsticker_command))
        application.add_handler(CommandHandler("rmsticker", self.rmsticker_command))
        application.add_handler(CommandHandler("stickerlist", self.stickerlist_command))
        application.add_handler(CommandHandler("addword", self.addword_command))
        application.add_handler(CommandHandler("rmword", self.rmword_command))
        application.add_handler(CommandHandler("wordlist", self.wordlist_command))
        application.add_handler(CommandHandler("blockword", self.blockword_command))
        application.add_handler(CommandHandler("unblockword", self.unblockword_command))
        application.add_handler(CommandHandler("blocksticker", self.blocksticker_command))
        application.add_handler(CommandHandler("unblocksticker", self.unblocksticker_command))
        application.add_handler(CommandHandler("listlocal", self.listlocal_command))

nsfw_blocklist = NSFWBlocklist(db)

# ===================== COMMAND HANDLERS (main) =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and context.args and context.args[0] == "help":
        await help_command(update, context)
        return

    bot_user = await context.bot.get_me()
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == 'private':
        existing = db.users.find_one({"_id": user.id})
        if not existing:
            msg_text = (
                f"🆕 <b>New User Started the Bot!</b>\n\n"
                f"👤 <b>Name:</b> {html.escape(user.full_name or user.first_name)}\n"
                f"🔗 <b>Username:</b> @{user.username if user.username else 'No username'}\n"
                f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
                f"🔗 <b>Profile:</b> <a href='tg://user?id={user.id}'>Click here</a>"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(admin_id, msg_text, parse_mode='HTML')
                except Exception as e:
                    logger.error(f"Failed to send new user notification to {admin_id}: {e}")
        db.add_user(user)
    else:
        db.add_group(chat.id, chat.title)

    CHANNEL_URL = "https://t.me/+rjE5xZlIK4U3ODA1"
    user_name = update.effective_user.first_name
    text = (
        f"🛡️ **𝗪𝗲𝗹𝗰𝗼𝗺𝗲, {html.escape(user_name)}!**\n\n"
        f"𝗜 𝗮𝗺 **{bot_user.first_name}** – 𝗮𝗻 𝗔𝗜-𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗦𝗲𝗰𝘂𝗿𝗶𝘁𝘆 𝗦𝘆𝘀𝘁𝗲𝗺,"
        " 𝗱𝗲𝘀𝗶𝗴𝗻𝗲𝗱 𝘁𝗼 𝗽𝗿𝗼𝘁𝗲𝗰𝘁 𝘆𝗼𝘂𝗿 𝗴𝗿𝗼𝘂𝗽.⚡\n\n"
        "✨ **𝗞𝗲𝘆 𝗦𝗵𝗶𝗲𝗹𝗱𝘀:**\n"
        "🔞 **𝗔𝗜 𝗡𝗦𝗙𝗪 𝗚𝘂𝗮𝗿𝗱**: Scans & deletes explicit media using AI.\n"
        "🤬 **𝗔𝗯𝘂𝘀𝗲 𝗦𝗵𝗶𝗲𝗹𝗱**: Instantly removes abusive words (blocklist).\n"
        "💡 _Click the buttons below to explore more!_"
    )
        

    keyboard = [
        [InlineKeyboardButton("➕ 𝐀𝐝𝐝 𝐭𝐨 𝐆𝐫𝐨𝐮𝐩", url=f"https://t.me/{bot_user.username}?startgroup=true")],
        [InlineKeyboardButton("𝐇𝐞𝐥𝐩❓", callback_data="help_main"), InlineKeyboardButton("📢 Support Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("👑 Owner & Sudo Menu 🔒", callback_data="sudo_menu")],
        [InlineKeyboardButton("𝗖𝗹𝗼𝘀𝗲 🗑", callback_data="delete_msg")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **BOT COMMANDS MENU**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 **USER COMMANDS**\n"
        "• `/start` : Check bot status\n"
        "• `/status` : Check security stats\n"
        "• `/help` : Show this menu\n\n"
        "🛡️ **GROUP ADMIN COMMANDS**\n"
        "• `/nsfw on/off` : Toggle AI NSFW filter in this group\n"
        "  `/blockword <word>` : Block a word locally\n"
        "  `/unblockword <word>` : Unblock a local word\n"
        "  `/blocksticker <pack>` : Block a sticker pack locally\n"
        "  `/unblocksticker <pack>` : Unblock a local sticker pack\n"
        "  `/listlocal` : List all local blocklists\n"
    )

    if update.effective_chat.type == 'private':
        await update.message.reply_text(help_text, parse_mode='Markdown')
        return

    bot_info = await context.bot.get_me()
    user_name = update.effective_user.first_name
    dm_url = f"https://t.me/{bot_info.username}?start=help"
    group_text = (
        f"💡 **Hey {html.escape(user_name)}!**\n\n"
        "I've sent the **Help Menu** to your DMs to keep this group clean. "
        "Click the button below to see it! 🚀"
    )
    keyboard = [[InlineKeyboardButton("💬 Open DM", url=dm_url)], [InlineKeyboardButton("🗑 Close", callback_data="delete_msg")]]
    await update.message.reply_text(group_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ===================== OTHER COMMANDS (keep) =====================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_info = await context.bot.get_me()
    bot_name = bot_info.first_name
    stats = db.get_global_stats()
    scanned = stats[0]
    nsfw_blocked = stats[4]
    abuse_caught = stats[5]
    start_timestamp = stats[6]

    groups = db.get_groups()
    group_count = len(groups) if groups else 0
    bot_start_time = datetime.fromtimestamp(start_timestamp, IST)
    uptime_delta = datetime.now(IST) - bot_start_time
    total_seconds = int(uptime_delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    total_threats = nsfw_blocked + abuse_caught
    threat_percent = int((total_threats / scanned) * 100) if scanned else 0
    if threat_percent < 10:
        threat_level = "LOW 🟢"
    elif threat_percent < 30:
        threat_level = "MODERATE 🟡"
    else:
        threat_level = "HIGH 🔴"

    def progress_bar(percent):
        percent = max(0, min(percent, 100))
        bars = int(percent / 10)
        return "█" * bars + "░" * (10 - bars)

    text = (
        f"<b>{bot_name}</b>\n"
        "<code>┌──────────────────────────────┐</code>\n"
        "<code>│ MATRIX AI SECURITY TERMINAL │</code>\n"
        "<code>└──────────────────────────────┘</code>\n\n"
        "🟢 <b>STATUS</b> : <code>LIVE PROTECTION</code>\n"
        "<code>system.scan() running...</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>[ THREAT ANALYTICS ]</b>\n\n"
        f"<code>> scanned_total     {progress_bar(100)} {scanned}</code>\n"
        f"<code>> nsfw_blocked     {progress_bar(nsfw_blocked)} {nsfw_blocked}</code>\n"
        f"<code>> abuse_caught     {progress_bar(abuse_caught)} {abuse_caught}</code>\n\n"
        "<b>[ NETWORK ]</b>\n"
        f"<code>> monitored_groups : {group_count}</code>\n\n"
        "<b>[ SYSTEM ]</b>\n"
        f"<code>> uptime : {uptime_str}</code>\n\n"
        "<b>[ AI THREAT LEVEL ]</b>\n"
        f"<code>{progress_bar(threat_percent)} {threat_percent}%</code>\n"
        f"<b>{threat_level}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<code>AI core :: scanning • filtering • neutralizing</code>\n"
    )
    keyboard = [[InlineKeyboardButton("🗑 Delete", callback_data="delete_msg")]]
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# --- Owner/Sudo commands (broadcast, grouplist, getlink, gmsg, greply, greact, cleangroups, sudo mgmt, gban) ---
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    reply_msg = update.message.reply_to_message
    args = context.args
    target_chat_id = None
    start_idx = 0
    if args and args[0].isdigit():
        s_no = int(args[0])
        groups = db.get_groups()
        if 1 <= s_no <= len(groups):
            target_chat_id = groups[s_no - 1][0]
            start_idx = 1
        else:
            await update.message.reply_text("❌ Invalid Serial Number.")
            return
    remaining = args[start_idx:]
    args_text = " ".join(remaining)
    should_pin = "-pin" in args_text
    should_unpin = "-unpin" in args_text
    clean_text = args_text.replace("-pin", "").replace("-unpin", "").strip()
    if not reply_msg and not clean_text and not should_unpin:
        await update.message.reply_text(
            "❗ <b>Usage:</b>\n"
            "• <code>/broadcast [-pin/-unpin] &lt;text&gt;</code>  (all groups)\n"
            "• <code>/broadcast &lt;sno&gt; [-pin/-unpin] &lt;text&gt;</code>  (specific group)\n"
            "• Reply to a message with <code>/broadcast [sno] [-pin/-unpin]</code>",
            parse_mode='HTML'
        )
        return
    status_msg = await update.message.reply_text("⏳ <b>Starting Broadcast...</b>\nThis may take a moment.", parse_mode='HTML')
    targets = [target_chat_id] if target_chat_id else db.get_all_targets()
    success, failed, pinned = 0, 0, 0
    for target_id in targets:
        try:
            if should_unpin:
                try: await context.bot.unpin_all_chat_messages(target_id)
                except: pass
            sent_message = None
            if reply_msg:
                sent_message = await reply_msg.copy(target_id)
            elif clean_text:
                sent_message = await context.bot.send_message(target_id, clean_text)
            if should_pin and sent_message:
                try:
                    await context.bot.pin_chat_message(chat_id=target_id, message_id=sent_message.message_id)
                    pinned += 1
                except: pass
            if sent_message or should_unpin:
                success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
    await status_msg.edit_text(
        f"✅ <b>Broadcast Complete!</b>\n\n"
        f"🎯 Successfully Sent: <code>{success}</code>\n"
        f"📌 Successfully Pinned: <code>{pinned}</code>\n"
        f"❌ Failed/Blocked: <code>{failed}</code>",
        parse_mode='HTML'
    )

async def grouplist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    groups = db.get_groups()
    if not groups:
        await update.message.reply_text("📭 I am not currently active in any groups.")
        return
    text = "📋 <b>Active Group List:</b>\n\n"
    for idx, (cid, title) in enumerate(groups, 1):
        safe_title = html.escape(title or 'Unknown Group')
        text += f"<b>{idx}.</b> {safe_title} (<code>{cid}</code>)\n"
    if len(text) > 4000:
        text = text[:4000] + "\n... (List too long, truncated)"
    await update.message.reply_text(text, parse_mode='HTML')

async def getlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❗ <b>Usage:</b> <code>/getlink <serial_no></code>", parse_mode='HTML')
        return
    s_no = int(context.args[0])
    groups = db.get_groups()
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
    target_chat_id = groups[s_no - 1][0]
    target_title = groups[s_no - 1][1]
    try:
        chat = await context.bot.get_chat(target_chat_id)
        invite_link = chat.invite_link or await context.bot.export_chat_invite_link(target_chat_id)
        await update.message.reply_text(f"🔗 <b>Link for {html.escape(target_title or 'Group')}:</b>\n{invite_link}", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Could not generate link.\nError: <code>{e}</code>", parse_mode='HTML')

async def gmsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❗ <b>Usage:</b> <code>/gmsg <serial_no> [-pin/-unpin] <message></code>", parse_mode='HTML')
        return
    s_no = int(context.args[0])
    groups = db.get_groups()
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
    target_chat_id = groups[s_no - 1][0]
    target_title = groups[s_no - 1][1]
    args_text = " ".join(context.args[1:])
    should_pin = "-pin" in args_text
    should_unpin = "-unpin" in args_text
    clean_text = args_text.replace("-pin", "").replace("-unpin", "").strip()
    try:
        if should_unpin:
            try: await context.bot.unpin_all_chat_messages(target_chat_id)
            except: pass
        sent_message = None
        if update.message.reply_to_message:
            sent_message = await update.message.reply_to_message.copy(target_chat_id)
        elif clean_text:
            sent_message = await context.bot.send_message(target_chat_id, clean_text)
        elif not should_unpin:
            await update.message.reply_text("Please provide text or reply to a message/media.")
            return
        if should_pin and sent_message:
            try: await context.bot.pin_chat_message(chat_id=target_chat_id, message_id=sent_message.message_id)
            except: pass
        status_text = f"✅ Message sent to <b>{html.escape(target_title or 'Group')}</b>."
        if should_pin: status_text += "\n📌 Message Pinned!"
        if should_unpin: status_text += "\n🧹 Previous messages Unpinned!"
        await update.message.reply_text(status_text, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Failed.\nError: <code>{e}</code>", parse_mode='HTML')

async def greply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    args = context.args
    if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("❗ **Usage:** `/greply <serial_no> <message_id> <your_message>`", parse_mode='Markdown')
        return
    s_no, msg_id = int(args[0]), int(args[1])
    text = " ".join(args[2:])
    groups = db.get_groups()
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
    target_chat_id = groups[s_no - 1][0]
    try:
        await context.bot.send_message(chat_id=target_chat_id, text=text, reply_to_message_id=msg_id)
        await update.message.reply_text("✅ Reply sent successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed.\nError: `{e}`", parse_mode='Markdown')

async def greact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    args = context.args
    if len(args) < 3 or not args[0].isdigit() or not args[1].isdigit():
        await update.message.reply_text("❗ **Usage:** `/greact <serial_no> <message_id> <emoji>`", parse_mode='Markdown')
        return
    s_no, msg_id = int(args[0]), int(args[1])
    emoji = args[2]
    groups = db.get_groups()
    if s_no < 1 or s_no > len(groups):
        await update.message.reply_text("❌ Invalid Serial Number.")
        return
    target_chat_id = groups[s_no - 1][0]
    try:
        await context.bot.set_message_reaction(chat_id=target_chat_id, message_id=msg_id, reaction=[ReactionTypeEmoji(emoji)])
        await update.message.reply_text(f"✅ Reaction {emoji} added successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed.\nError: `{e}`", parse_mode='Markdown')

async def cleangroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        return
    status_msg = await update.message.reply_text("⏳ <b>Scanning Database...</b>", parse_mode='HTML')
    groups = db.get_groups()
    removed_count = 0
    for chat_id, _ in groups:
        try:
            await context.bot.get_chat(chat_id)
            await asyncio.sleep(0.1)
        except (Forbidden, BadRequest):
            db.remove_group(chat_id)
            removed_count += 1
        except Exception:
            pass
    await status_msg.edit_text(
        f"✅ <b>Database Cleanup Complete!</b>\n\n"
        f"🗑️ <b>Removed Dead Groups:</b> <code>{removed_count}</code>",
        parse_mode='HTML'
    )

async def addsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Only the Bot Owner can use this command.")
        return
    target_id, target_name, error_msg = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text(error_msg)
        return
    if target_id in ADMIN_IDS:
        await update.message.reply_text("This user is already the Bot Owner.")
        return
    db.add_sudo(target_id)
    safe_name = target_name or str(target_id)
    await update.message.reply_text(f"👑 **{safe_name}** (`{target_id}`) has been promoted to Sudo Admin.", parse_mode='Markdown')

async def rmsudo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Only the Bot Owner can use this command.")
        return
    target_id, target_name, error_msg = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text(error_msg)
        return
    safe_name = target_name or str(target_id)
    if db.remove_sudo(target_id):
        await update.message.reply_text(f"❌ **{safe_name}** (`{target_id}`) removed from Sudo Admins.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"**{safe_name}** (`{target_id}`) is not a Sudo Admin.", parse_mode='Markdown')

async def sudolist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Only the Bot Owner can use this command.")
        return
    sudo_ids = db.get_sudos()
    if not sudo_ids:
        await update.message.reply_text("📭 The Sudo list is empty.")
        return
    text = "👑 **Sudo Admins:**\n\n"
    for idx, uid in enumerate(sudo_ids, 1):
        user_info = db.users.find_one({"_id": uid})
        if user_info:
            name = user_info.get("full_name") or user_info.get("first_name") or "Unknown"
            mention = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
            text += f"{idx}. {mention} (<code>{uid}</code>)\n"
        else:
            text += f"{idx}. <code>{uid}</code>\n"
    if len(text) > 4000:
        text = text[:4000] + "\n... (List too long, truncated)"
    await update.message.reply_text(text, parse_mode='HTML')

async def gban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        await update.message.reply_text("❌ Only Owner and Sudo Admins can use this command.")
        return
    target_id, target_name, reason = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text(reason)
        return
    if target_id in ADMIN_IDS or db.is_sudo(target_id):
        await update.message.reply_text("❌ You cannot GBan an Admin or Sudo user.")
        return
    db.add_gban(target_id, reason)
    safe_reason = html.escape(reason)
    safe_name = html.escape(target_name or str(target_id))
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🚨 <b>GLOBAL BAN NOTICE</b> 🚨\n\nYou have been Globally Banned.\n\n📝 <b>Reason:</b> {safe_reason}",
            parse_mode='HTML'
        )
    except Exception:
        pass
    all_groups = db.get_groups()
    banned_count = 0
    for chat_id, _ in all_groups:
        try:
            await context.bot.ban_chat_member(chat_id, target_id)
            banned_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(
        f"🌍 <b>GBANNED SUCCESSFULLY!</b>\n\n"
        f"👤 <b>User:</b> {safe_name} (<code>{target_id}</code>)\n"
        f"📝 <b>Reason:</b> {safe_reason}\n"
        f"🔨 <b>Banned in:</b> {banned_count} groups",
        parse_mode='HTML'
    )

async def ungban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        await update.message.reply_text("❌ Only Owner and Sudo Admins can use this command.")
        return
    target_id, target_name, reason = await extract_target(update, context)
    if not target_id:
        await update.message.reply_text(reason)
        return
    if db.remove_gban(target_id):
        try:
            await context.bot.send_message(target_id, "✅ **UNBAN NOTICE** ✅\n\nYour Global Ban has been lifted!")
        except Exception:
            pass
        safe_name = html.escape(target_name or str(target_id))
        await update.message.reply_text(f"✅ **UN-GBANNED!**\n\n👤 **User:** {safe_name} (`{target_id}`) removed from GBan list.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ User `{target_id}` is not globally banned.", parse_mode='Markdown')

async def gbanlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS and not db.is_sudo(update.effective_user.id):
        await update.message.reply_text("❌ Only Owner and Sudo Admins can use this command.")
        return
    gbans = db.get_gbans()
    if not gbans:
        await update.message.reply_text("📭 GBan list is empty.")
        return
    text = "🌍 **Globally Banned Users:**\n\n"
    for idx, (uid, reason) in enumerate(gbans, 1):
        text += f"{idx}. `{uid}` - {reason}\n"
    if len(text) > 4000:
        text = text[:4000] + "\n... (List too long, truncated)"
    await update.message.reply_text(text, parse_mode='Markdown')

# ===================== BUTTON HANDLER =====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if "delete_msg" in query.data or "delmsg" in query.data:
        try:
            await query.message.delete()
        except Exception as e:
            if "can't be deleted" in str(e).lower() or "not enough rights" in str(e).lower():
                await query.answer("⚠️ Please give me delete messages permission.", show_alert=True)
        return

    if query.data == "help_main":
        is_private = update.effective_chat.type == 'private'
        if is_private:
            help_text = (
                "🤖 **BOT COMMANDS MENU**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "👤 **USER COMMANDS**\n"
                "• `/start` : Check bot status\n"
                "• `/status` : Check security stats\n"
                "• `/help` : Show this menu\n\n"
                "🛡️ **GROUP ADMIN COMMANDS**\n"
                "• `/nsfw on/off` : Toggle AI NSFW filter in this group\n"
                "  `/blockword <word>` : Block a word locally\n"
                "  `/unblockword <word>` : Unblock a local word\n"
                "  `/blocksticker <pack>` : Block a sticker pack locally\n"
                "  `/unblocksticker <pack>` : Unblock a local sticker pack\n"
                "  `/listlocal` : List all local blocklists\n"
            )

            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]]
            await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        else:
            bot_info = await context.bot.get_me()
            user_name = update.effective_user.first_name
            dm_url = f"https://t.me/{bot_info.username}?start=help"
            group_text = f"💡 **Hey {html.escape(user_name)}!**\n\nPlease click the button below to get the help menu in your DMs.."
            keyboard = [
                [InlineKeyboardButton("💬 Open DM", url=dm_url)],
                [InlineKeyboardButton("⬅️ Back", callback_data="back_to_start"), InlineKeyboardButton("🗑 Close", callback_data="delete_msg")]
            ]
            await query.edit_message_text(group_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        try: await query.answer()
        except: pass
        return

    if query.data == "back_to_start":
        await start_command(update, context)
        try: await query.answer()
        except: pass
        return

    if query.data == "sudo_menu":
        if user_id not in ADMIN_IDS and not db.is_sudo(user_id):
            await query.answer("❌ ACCESS DENIED!\n\nThis menu is locked. Only the Bot Owner and Sudo Admins can open it.", show_alert=True)
            return
        sudo_text = (
            "👑 **OWNER & SUDO COMMANDS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• `/broadcast <text>` : Message to all active groups\n"
            "• `/grouplist` : List all monitored groups\n"
            "• `/getlink <s_no>` : Get invite link of a group\n"
            "• `/gmsg <s_no> <text>` : Send a direct message\n"
            "• `/greply <s_no> <msg_id> <txt>` : Reply to a group message\n"
            "• `/greact <s_no> <msg_id> <emoji>` : Add reaction to message\n"
            "• `/cleangroups` : Remove dead groups from database\n"
            "• `/nsfw all on/off` : Global NSFW Control\n\n"
            "🛠️ **CUSTOM BLOCKLISTS**\n"
            "• `/addsticker`, `/rmsticker`, `/stickerlist`\n"
            "• `/addword`, `/rmword`, `/wordlist`\n\n"
            "👮‍♂️ **ADMIN MANAGEMENT**\n"
            "• `/addsudo` : Promote user to Sudo\n"
            "• `/rmsudo` : Demote Sudo Admin\n"
            "• `/sudolist` : List all Sudo Admins\n"
            "• `/gban` : Ban globally\n"
            "• `/ungban` : Unban globally\n"
            "• `/gbanlist` : List of gban user\n"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_start")]]
        await query.edit_message_text(sudo_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        try: await query.answer()
        except: pass
        return

    # If none matched, just answer silently
    try: await query.answer()
    except: pass

# ===================== MESSAGE HANDLER =====================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.message.from_user
    chat_id = update.effective_chat.id

    # GBAN check
    is_gbanned, _ = db.is_gbanned(user.id)
    if is_gbanned:
        try:
            await update.message.delete()
            await context.bot.ban_chat_member(chat_id, user.id)
        except:
            pass
        return

    db.update_stat('scanned')

    if update.effective_chat.type == 'private':
        if user:
            db.add_user(user)
        return

    if update.message.new_chat_members or update.message.left_chat_member:
        return

    db.add_group(chat_id, update.effective_chat.title)
    delay_min, warn_limit, action, _, _, nsfw_enabled, _, bio_check_enabled = db.get_config(chat_id)

    if not user:
        return
    msg_text = update.message.text or update.message.caption

    # Run NSFW + blocklist filters
    if await nsfw_blocklist.process_content_filters(update, context):
        return

# ===================== JOB & MIDDLEWARE =====================
async def delete_msg_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    message_id = context.job.data
    if chat_id and message_id:
        BULK_DELETE_QUEUE[chat_id].append(message_id)

async def flush_bulk_deletes(context: ContextTypes.DEFAULT_TYPE):
    for chat_id, msg_ids in list(BULK_DELETE_QUEUE.items()):
        if not msg_ids:
            continue
        to_delete = msg_ids.copy()
        BULK_DELETE_QUEUE[chat_id].clear()
        for i in range(0, len(to_delete), 100):
            batch = to_delete[i:i+100]
            try:
                await context.bot.delete_messages(chat_id=chat_id, message_ids=batch)
            except Exception:
                for mid in batch:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    except:
                        pass

async def track_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    chat = result.chat
    new_status = result.new_chat_member.status
    context.chat_data['is_bot_admin'] = (new_status == 'administrator')  # using string
    if new_status in ['left', 'kicked', 'banned']:
        db.remove_group(chat.id)
        logger.info(f"Bot removed from group: {chat.title} ({chat.id})")
    elif new_status in ['member', 'administrator']:
        db.add_group(chat.id, chat.title)
        logger.info(f"Bot added to group: {chat.title} ({chat.id})")

async def enforce_bot_admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.effective_chat or update.effective_chat.type not in ['group', 'supergroup']:
            return
        if update.my_chat_member:
            return
        chat_id = update.effective_chat.id
        is_admin = context.chat_data.get('is_bot_admin')
        if is_admin is None:
            try:
                bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
                is_admin = bot_member.status in ['administrator', 'creator']
                context.chat_data['is_bot_admin'] = is_admin
            except Exception:
                is_admin = False
        if is_admin:
            return
        raise ApplicationHandlerStop()
    except ApplicationHandlerStop:
        raise
    except Exception:
        raise ApplicationHandlerStop()

# ===================== MAIN =====================
def main():
    app_bot = Application.builder().token(TOKEN).connect_timeout(60).read_timeout(60).write_timeout(60).pool_timeout(60).build()

    app_bot.add_handler(TypeHandler(Update, enforce_bot_admin_status), group=-1)

    # Register NSFW + blocklist handlers
    nsfw_blocklist.register_handlers(app_bot)

    # Register main command handlers
    app_bot.add_handler(CommandHandler("start", start_command))
    app_bot.add_handler(CommandHandler("help", help_command))
    app_bot.add_handler(CommandHandler("status", status_command))
    app_bot.add_handler(CommandHandler("broadcast", broadcast_command))
    app_bot.add_handler(CommandHandler("grouplist", grouplist_command))
    app_bot.add_handler(CommandHandler("getlink", getlink_command))
    app_bot.add_handler(CommandHandler("gmsg", gmsg_command))
    app_bot.add_handler(CommandHandler("greply", greply_command))
    app_bot.add_handler(CommandHandler("greact", greact_command))
    app_bot.add_handler(CommandHandler("cleangroups", cleangroups_command))
    app_bot.add_handler(CommandHandler("addsudo", addsudo_command))
    app_bot.add_handler(CommandHandler("rmsudo", rmsudo_command))
    app_bot.add_handler(CommandHandler("sudolist", sudolist_command))
    app_bot.add_handler(CommandHandler("gban", gban_command))
    app_bot.add_handler(CommandHandler("ungban", ungban_command))
    app_bot.add_handler(CommandHandler("gbanlist", gbanlist_command))

    app_bot.add_handler(CallbackQueryHandler(button_handler))
    app_bot.add_handler(MessageHandler((~filters.COMMAND), message_handler))
    app_bot.add_handler(ChatMemberHandler(track_bot_status, ChatMemberHandler.MY_CHAT_MEMBER))

    app_bot.job_queue.run_repeating(flush_bulk_deletes, interval=2, first=2)

    logger.info("Bot started polling...")
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    keep_alive()  # optional for Render, can remove if not needed
    main()
