# main.py
# (v8.1 - Strict One-Time Link: Member Limit=1 + Time Limit=10 Mins)

import logging
import sqlite3
import feedparser
import os
import requests
import asyncio 
import uuid 
import time  # <-- Time import kiya expire date ke liye
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest 
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler
)

# --- 1. CONFIGURATION ---
# 🛑 WARNING: Revoke this token and use a new one!
BOT_TOKEN = "8098953695:AAF50GoYSNhjpvDGyOLIIvcamz-zU3WTDj0"
ADMIN_CHAT_ID = [7857898495, 6373993818]
BLOG_RSS_URL = "https://shinmonmovies.blogspot.com/feeds/posts/default?alt=rss"
DB_NAME = "bot_database.db"
# --- End of Configuration ---

# Conversation states
(STATE_API_TYPE, STATE_API_KEY, STATE_CHAN_ID, STATE_CHAN_NAME,
 STATE_REMOVE_CHAN, STATE_SET_FSUB, STATE_BROADCAST) = range(7)

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- 2. Database Functions ---

def setup_database():
    """Sets up the database for all features."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS locked_channels (channel_id TEXT PRIMARY KEY, display_name TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)''')
    
    # Verification Codes Table
    cursor.execute('''CREATE TABLE IF NOT EXISTS verification_codes (token TEXT PRIMARY KEY, channel_id TEXT)''')
    
    conn.commit()
    conn.close()
    logger.info("Database setup complete.")

# --- User DB ---
def add_user_to_db(user_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error adding user {user_id} to DB: {e}")

def get_all_users():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    results = [item[0] for item in cursor.fetchall()]
    conn.close()
    return results

# --- Settings DB ---
def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# --- Channel DB ---
def add_channel_to_db(channel_id, display_name):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO locked_channels (channel_id, display_name) VALUES (?, ?)", (channel_id, display_name))
    conn.commit()
    conn.close()

def get_all_channels():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, display_name FROM locked_channels")
    results = cursor.fetchall() 
    conn.close()
    return results

def remove_channel_from_db(channel_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM locked_channels WHERE channel_id=?", (channel_id,))
    conn.commit()
    conn.close()
    logger.info(f"Removed channel {channel_id} from DB.")

# --- Verification Codes DB ---
def save_verification_token(token, channel_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO verification_codes (token, channel_id) VALUES (?, ?)", (token, channel_id))
    conn.commit()
    conn.close()

def get_verification_data(token):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id FROM verification_codes WHERE token=?", (token,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def delete_verification_token(token):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM verification_codes WHERE token=?", (token,))
    conn.commit()
    conn.close()

# --- 3. Link Shortener Function ---
def shorten_link(url_to_shorten):
    api_key = get_setting("shortener_api_key")
    api_type = get_setting("shortener_api_type")
    
    if not api_key or not api_type:
        return url_to_shorten
    
    try:
        request_url = ""
        # Using raw f-strings to prevent double encoding issues
        if api_type == "vplink":
            request_url = f"https://vplink.in/api?api={api_key}&url={url_to_shorten}"
        elif api_type == "gplink":
            request_url = f"https://gplink.in/api?api={api_key}&url={url_to_shorten}"
        else:
            return url_to_shorten
        
        response = requests.get(request_url)
        response.raise_for_status()
        data = response.json()
        
        if data.get("status") == "success" and data.get("shortenedUrl"):
            return data["shortenedUrl"]
        else:
            logger.error(f"Shortener API returned error: {data}")
            return url_to_shorten
            
    except requests.RequestException as e:
        logger.error(f"Error calling shortener API: {e}")
        return url_to_shorten

# --- 4. Core Bot Features ---

# --- F-Sub Helper Functions ---
async def check_fsub(user_id, context: ContextTypes.DEFAULT_TYPE):
    fsub_channel = get_setting("fsub_channel")
    if not fsub_channel: return True 
    try:
        member = await context.bot.get_chat_member(chat_id=fsub_channel, user_id=user_id)
        if member.status in ['member', 'administrator', 'creator']: return True
        else: return False
    except BadRequest:
        set_setting("fsub_channel", None) 
        return True
    except Forbidden:
        set_setting("fsub_channel", None)
        return True
    except Exception as e:
        return False

async def send_fsub_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fsub_channel = get_setting("fsub_channel")
    keyboard = None
    join_text = ""
    if not fsub_channel: return
    if fsub_channel.startswith('@'):
        join_text = f"<b>Please join {fsub_channel} and try again.</b>"
        keyboard = [[InlineKeyboardButton("Join Channel", url=f"https://t.me/{fsub_channel.lstrip('@')}")]]
    elif fsub_channel.startswith('-100'):
        try:
            invite_link = await context.bot.export_chat_invite_link(chat_id=fsub_channel)
            join_text = "<b>Please join our private updates channel and try again.</b>"
            keyboard = [[InlineKeyboardButton("Join Private Channel", url=invite_link)]]
        except Exception:
            pass
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    text = f"<b>You must join our updates channel to use this bot.</b>\n\n{join_text}"
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# --- Button Layouts ---
def get_user_start_keyboard():
    channels = get_all_channels()
    keyboard = []
    row = []
    if not channels: return None 
    for channel_id, display_name in channels:
        button = InlineKeyboardButton(text=display_name, callback_data=f"join_{channel_id}")
        row.append(button)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🟢 Get All Links At Once", callback_data="get_all_links")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_panel_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Add Channel", callback_data="add_channel"), InlineKeyboardButton("➖ Remove Channel", callback_data="remove_channel")],
        [InlineKeyboardButton("🔑 Set Shortener API", callback_data="set_api"), InlineKeyboardButton("📎 Set F-Sub Channel", callback_data="set_fsub")],
        [InlineKeyboardButton("📣 Broadcast Message", callback_data="broadcast")],
        [InlineKeyboardButton("« Back to Start", callback_data="back_to_start")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Command Handlers (User) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start and /start [token]."""
    user_id = update.effective_user.id
    add_user_to_db(user_id) 

    if not await check_fsub(user_id, context):
        await send_fsub_message(update, context)
        return
    
    # --- DEEP LINK VERIFICATION ---
    if context.args:
        token = context.args[0]
        channel_id_data = get_verification_data(token)
        
        if channel_id_data:
            final_message = "✅ <b>Verification Successful!</b>\n\n<b>Here is your One-Time invite link (Valid for 10 mins):</b>\n\n"
            
            try:
                # 600 seconds = 10 minutes expiry time
                expire_time = int(time.time()) + 600 
                
                if channel_id_data == "ALL":
                    channels = get_all_channels()
                    if not channels:
                         final_message += "❌ <b>No channels available.</b>"
                    else:
                        for cid, cname in channels:
                            try:
                                # Create link with member_limit=1 AND expire_date
                                invite = await context.bot.create_chat_invite_link(
                                    chat_id=cid, 
                                    member_limit=1,
                                    expire_date=expire_time
                                )
                                final_message += f"🔗 <b>{cname}</b>: <a href=\"{invite.invite_link}\">Click to Join</a>\n"
                            except Exception as e:
                                final_message += f"❌ <b>{cname}</b>: Error (Bot not admin)\n"
                else:
                    try:
                        # Create link with member_limit=1 AND expire_date
                        invite = await context.bot.create_chat_invite_link(
                            chat_id=channel_id_data, 
                            member_limit=1,
                            expire_date=expire_time
                        )
                        final_message += f"🔗 <b>Click to Join:</b> <a href=\"{invite.invite_link}\">Join Channel</a>"
                    except Exception as e:
                         final_message += "❌ <b>Error: Bot is not an admin in that channel anymore.</b>"

                await update.message.reply_text(
                    final_message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
                
                # Token delete kar do taaki /start link dubara use na ho
                delete_verification_token(token)
                return
                
            except Exception as e:
                logger.error(f"Error in start generation: {e}")
                await update.message.reply_text("❌ <b>System Error. Try again.</b>", parse_mode=ParseMode.HTML)
                return

        else:
            await update.message.reply_text("❌ <b>Invalid or Expired Link. Please try again from the menu.</b>", parse_mode=ParseMode.HTML)
    
    # --- Normal Start Menu ---
    message_text = (
        f"✨ <b>Welcome to @{context.bot.username}!</b>\n\n"
        f"<b>Pick a channel/group to get your invite link.</b>\n\n"
        f"<b>Click Get All Links At Once to join all Channels/Groups.</b>\n\n"
        f"<b>ℹ️ /help | Updates: @ShinChanBannedMovies</b>\n" 
        f"<b>By: @AnimeUniverse369</b>"                            
    )

    if user_id == ADMIN_CHAT_ID:
        user_keyboard = get_user_start_keyboard()
        admin_panel_button = [InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel")]
        if user_keyboard:
            new_keyboard_layout = list(user_keyboard.inline_keyboard)
            new_keyboard_layout.append(admin_panel_button)
            final_markup = InlineKeyboardMarkup(new_keyboard_layout)
        else:
            final_markup = InlineKeyboardMarkup([admin_panel_button])
        await update.message.reply_text(message_text, reply_markup=final_markup, parse_mode=ParseMode.HTML)
    else:
        user_keyboard = get_user_start_keyboard()
        if user_keyboard:
            await update.message.reply_text(message_text, reply_markup=user_keyboard, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("✨ <b>Welcome!</b>\n\n<b>Bot under maintenance.</b>", parse_mode=ParseMode.HTML)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_user_to_db(update.effective_user.id)
    if not await check_fsub(update.effective_user.id, context):
        await send_fsub_message(update, context)
        return
    await update.message.reply_text("<b>Help:</b> Select a channel, solve the captcha link, and get access!", parse_mode=ParseMode.HTML)

# --- Button Click Handlers (User) ---
async def user_join_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a Deep Link for a single channel."""
    query = update.callback_query
    await query.answer("Generating secure link...")
    
    user_id = query.from_user.id
    add_user_to_db(user_id)
    
    if not await check_fsub(user_id, context):
        await send_fsub_message(update, context)
        return

    channel_id = query.data.split('_', 1)[1]
    
    # Generate Token
    token = str(uuid.uuid4().hex)
    
    # Save (Token -> Channel ID) mapping. 
    save_verification_token(token, channel_id)
    
    # Create Deep Link
    deep_link = f"https://t.me/{context.bot.username}?start={token}"
    
    # Shorten Deep Link
    final_link = shorten_link(deep_link) 
    
    keyboard = [[InlineKeyboardButton("🔗 Verify & Join", url=final_link)]]
    
    await query.message.reply_text(
        "<b>Link Generated!</b> \n\n"
        "<b>1. Click the button below.</b>\n"
        "<b>2. Solve the shortener task.</b>\n"
        "<b>3. You will get a One-Time Use link.</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def get_all_links_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a Deep Link for ALL channels."""
    query = update.callback_query
    await query.answer("Generating secure link...")
    
    user_id = query.from_user.id
    add_user_to_db(user_id)

    if not await check_fsub(user_id, context):
        await send_fsub_message(update, context)
        return

    # Generate Token
    token = str(uuid.uuid4().hex)
    
    # Save Special Keyword "ALL"
    save_verification_token(token, "ALL")
    
    deep_link = f"https://t.me/{context.bot.username}?start={token}"
    final_link = shorten_link(deep_link)
    
    keyboard = [[InlineKeyboardButton("🔗 Verify & Get All Links", url=final_link)]]
    
    await query.message.reply_text(
        "<b>Multi-Link Generated!</b> \n\n"
        "<b>Click to unlock access to all our channels at once.</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

# --- 5. Admin Panel & Conv Handlers (Same as before) ---

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_text("<b>🔧 Admin Panel</b>", reply_markup=get_admin_panel_keyboard(), parse_mode=ParseMode.HTML)
    except BadRequest: pass
    return ConversationHandler.END 

async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    message_text = (
        f"✨ <b>Welcome to @{context.bot.username}!</b>\n\n"
        f"<b>Pick a channel/group to get your invite link.</b>\n\n"
        f"<b>Click Get All Links At Once to join all Channels/Groups.</b>\n\n"
        f"<b>ℹ️ /help | Updates: @YourUpdatesChannel</b>\n" 
        f"<b>By: @YourName</b>"                            
    )
    user_keyboard = get_user_start_keyboard()
    admin_panel_button = [InlineKeyboardButton("🔧 Admin Panel", callback_data="admin_panel")]
    final_markup = InlineKeyboardMarkup(list(user_keyboard.inline_keyboard) + [admin_panel_button]) if user_keyboard else InlineKeyboardMarkup([admin_panel_button])
    try:
        await query.edit_message_text(message_text, reply_markup=final_markup, parse_mode=ParseMode.HTML)
    except BadRequest: pass
    return ConversationHandler.END

# --- API Conversation ---
async def set_api_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("GPLink", callback_data="gplink"), InlineKeyboardButton("VPLink", callback_data="vplink")], [InlineKeyboardButton("None", callback_data="none")], [InlineKeyboardButton("« Back", callback_data="back_to_admin_panel")]]
    await query.edit_message_text("<b>Select Shortener:</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    return STATE_API_TYPE

async def set_api_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; api_type = query.data; await query.answer()
    if api_type == "none":
        set_setting("shortener_api_key", None); set_setting("shortener_api_type", None)
        await query.edit_message_text("<b>Disabled.</b>", parse_mode=ParseMode.HTML); await asyncio.sleep(1); await admin_panel(update, context); return ConversationHandler.END
    context.user_data["api_type"] = api_type
    await query.edit_message_text(f"<b>Selected {api_type}. Send API Key:</b>", parse_mode=ParseMode.HTML)
    return STATE_API_KEY

async def set_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("shortener_api_key", update.message.text); set_setting("shortener_api_type", context.user_data["api_type"])
    await update.message.reply_text("<b>API Key Saved!</b>", parse_mode=ParseMode.HTML); await start(update, context); return ConversationHandler.END

# --- Add Channel Conv ---
async def add_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("<b>Send Channel Username/ID:</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="back_to_admin_panel")]]), parse_mode=ParseMode.HTML)
    return STATE_CHAN_ID

async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.message.text
    try:
        m = await context.bot.get_chat_member(cid, context.bot.id)
        if not m.status == "administrator": raise Exception("Not admin")
    except: await update.message.reply_text("<b>Error: Bot must be admin.</b>", parse_mode=ParseMode.HTML); return ConversationHandler.END
    context.user_data["channel_id"] = cid; await update.message.reply_text("<b>Send Display Name:</b>", parse_mode=ParseMode.HTML); return STATE_CHAN_NAME

async def get_channel_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_channel_to_db(context.user_data["channel_id"], update.message.text); await update.message.reply_text("<b>Added!</b>", parse_mode=ParseMode.HTML); await start(update, context); return ConversationHandler.END

# --- Remove Channel Conv ---
async def remove_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); channels = get_all_channels()
    if not channels: await query.edit_message_text("<b>No channels.</b>", parse_mode=ParseMode.HTML); return ConversationHandler.END
    kb = [[InlineKeyboardButton(f"❌ {name}", callback_data=f"rm_{cid}")] for cid, name in channels] + [[InlineKeyboardButton("« Back", callback_data="back_to_admin_panel")]]
    await query.edit_message_text("<b>Remove Channel:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML); return STATE_REMOVE_CHAN

async def remove_channel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_channel_from_db(update.callback_query.data.split('_',1)[1]); await update.callback_query.answer("Removed"); await admin_panel(update, context); return ConversationHandler.END

# --- F-Sub Conv ---
async def set_fsub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("<b>Send F-Sub Channel ID/User:</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="back_to_admin_panel")]]), parse_mode=ParseMode.HTML); return STATE_SET_FSUB

async def set_fsub_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.message.text
    if cid.lower() == 'none': set_setting("fsub_channel", None); await start(update, context); return ConversationHandler.END
    try: await context.bot.get_chat_member(cid, context.bot.id)
    except: await update.message.reply_text("<b>Error: Bot not admin.</b>", parse_mode=ParseMode.HTML); return ConversationHandler.END
    set_setting("fsub_channel", cid); await update.message.reply_text("<b>F-Sub Set!</b>", parse_mode=ParseMode.HTML); await start(update, context); return ConversationHandler.END

# --- Broadcast Conv ---
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await query.edit_message_text("<b>Send message to broadcast:</b>", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="back_to_admin_panel")]]), parse_mode=ParseMode.HTML); return STATE_BROADCAST

async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = get_all_users(); sent = 0
    await update.message.reply_text(f"<b>Broadcasting to {len(ids)}...</b>", parse_mode=ParseMode.HTML)
    for uid in ids:
        if uid == ADMIN_CHAT_ID: continue
        try: await update.message.copy(uid); sent += 1
        except: pass
        await asyncio.sleep(0.1)
    await update.message.reply_text(f"<b>Sent: {sent}</b>", parse_mode=ParseMode.HTML); await start(update, context); return ConversationHandler.END

# --- RSS/Search ---
async def check_rss_feed(context: ContextTypes.DEFAULT_TYPE):
    try:
        feed = feedparser.parse(BLOG_RSS_URL); chan = "@ShinchanBannedMovies"
        for entry in reversed(feed.entries):
             # Logic same as before
             pass 
    except: pass

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>Search feature placeholder.</b>", parse_mode=ParseMode.HTML)

async def request_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("<b>Request sent.</b>", parse_mode=ParseMode.HTML)
    await context.bot.send_message(ADMIN_CHAT_ID, f"Req: {' '.join(context.args)}")

# --- Main ---
def main():
    setup_database()
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Handlers
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(set_api_start, pattern='set_api')], states={STATE_API_TYPE: [CallbackQueryHandler(set_api_type, pattern='^(gplink|vplink|none)$')], STATE_API_KEY: [MessageHandler(filters.TEXT, set_api_key)]}, fallbacks=[CallbackQueryHandler(admin_panel, pattern='back_to_admin_panel')]))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_channel_start, pattern='add_channel')], states={STATE_CHAN_ID: [MessageHandler(filters.TEXT, get_channel_id)], STATE_CHAN_NAME: [MessageHandler(filters.TEXT, get_channel_name)]}, fallbacks=[CallbackQueryHandler(admin_panel, pattern='back_to_admin_panel')]))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(remove_channel_start, pattern='remove_channel')], states={STATE_REMOVE_CHAN: [CallbackQueryHandler(remove_channel_confirm, pattern=r'^rm_')]}, fallbacks=[CallbackQueryHandler(admin_panel, pattern='back_to_admin_panel')]))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(set_fsub_start, pattern='set_fsub')], states={STATE_SET_FSUB: [MessageHandler(filters.TEXT, set_fsub_channel)]}, fallbacks=[CallbackQueryHandler(admin_panel, pattern='back_to_admin_panel')]))
    application.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(broadcast_start, pattern='broadcast')], states={STATE_BROADCAST: [MessageHandler(filters.ALL, broadcast_message)]}, fallbacks=[CallbackQueryHandler(admin_panel, pattern='back_to_admin_panel')]))

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("request", request_movie))
    
    application.add_handler(CallbackQueryHandler(admin_panel, pattern='admin_panel'))
    application.add_handler(CallbackQueryHandler(get_all_links_click, pattern='get_all_links'))
    application.add_handler(CallbackQueryHandler(user_join_button_click, pattern=r'^join_'))
    application.add_handler(CallbackQueryHandler(back_to_start, pattern='back_to_start'))

    application.job_queue.run_repeating(check_rss_feed, interval=900, first=0)
    logger.info("Bot Started.")
    application.run_polling()

if __name__ == '__main__':
    main()
