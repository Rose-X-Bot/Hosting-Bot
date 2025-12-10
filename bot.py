# -*- coding: utf-8 -*-
import os
import telebot
import subprocess
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import threading
import re
import sys
import signal
import atexit
import hashlib
import mimetypes
import struct

# --- Configuration ---
# IMPORTANT: Render ke Environment Variables se token lena hai
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')  # CHANGE THIS!
OWNER_ID = 8039591060  # Apna ID rakho
ADMIN_ID = 8039591060  # Same as owner if only you
YOUR_USERNAME = '@ROSE_X_FILE'
UPDATE_CHANNEL = 'https://t.me/ROSE_X_FILE'

# Render compatible paths
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

# File upload limits
FREE_USER_LIMIT = 10
SUBSCRIBED_USER_LIMIT = 15
ADMIN_LIMIT = 999
OWNER_LIMIT = float('inf')

# Create necessary directories
os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

# Initialize bot
bot = telebot.TeleBot(TOKEN)

# --- Data structures ---
bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
bot_locked = False

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- Simplified for Render ---
# Flask hata diya kyunki Render par polling kaam karega
# Agar 24/7 chahiye to UptimeRobot use karna

# --- Command Buttons ---
COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["📤 Send Command", "📞 Contact Owner"]
]

ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💳 Subscriptions", "📢 Broadcast"],
    ["🔒 Lock Bot", "🟢 Running All Code"],
    ["📤 Send Command", "👑 Admin Panel"],
    ["📞 Contact Owner"]
]

# --- Database Setup ---
def init_db():
    """Initialize the database"""
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        
        # Create tables
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        
        # Insert default admins
        c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (ADMIN_ID,))
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}", exc_info=True)

def load_data():
    """Load data from database"""
    logger.info("Loading data from database...")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()

        # Load subscriptions
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError:
                logger.warning(f"Invalid expiry for user {user_id}: {expiry}")

        # Load user files
        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))

        # Load active users
        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())

        # Load admins
        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())

        conn.close()
        logger.info(f"Data loaded: {len(active_users)} users, {len(user_subscriptions)} subscriptions")
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

# Initialize DB and Load Data
init_db()
load_data()

# --- Helper Functions ---
def get_user_folder(user_id):
    """Get user's folder"""
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder

def get_user_file_limit(user_id):
    """Get file upload limit"""
    if user_id == OWNER_ID: 
        return OWNER_LIMIT
    if user_id in admin_ids: 
        return ADMIN_LIMIT
    if user_id in user_subscriptions:
        expiry = user_subscriptions[user_id].get('expiry')
        if expiry and expiry > datetime.now():
            return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    """Get number of files uploaded by user"""
    return len(user_files.get(user_id, []))

def is_bot_running(script_owner_id, file_name):
    """Check if script is running"""
    script_key = f"{script_owner_id}_{file_name}"
    script_info = bot_scripts.get(script_key)
    
    if script_info and script_info.get('process'):
        try:
            proc = psutil.Process(script_info['process'].pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Clean up if process not found
            if script_key in bot_scripts:
                del bot_scripts[script_key]
            return False
    return False

def kill_process_tree(process_info):
    """Kill process and its children"""
    try:
        process = process_info.get('process')
        if process and hasattr(process, 'pid'):
            pid = process.pid
            parent = psutil.Process(pid)
            
            # Kill children first
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except:
                    pass
            
            # Kill parent
            try:
                parent.terminate()
            except:
                try:
                    parent.kill()
                except:
                    pass
            
            # Close log file if exists
            if 'log_file' in process_info:
                try:
                    process_info['log_file'].close()
                except:
                    pass
    except:
        pass

# --- Database Operations ---
DB_LOCK = threading.Lock()

def save_user_file(user_id, file_name, file_type='py'):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)',
                      (user_id, file_name, file_type))
            conn.commit()
            if user_id not in user_files:
                user_files[user_id] = []
            user_files[user_id] = [(fn, ft) for fn, ft in user_files[user_id] if fn != file_name]
            user_files[user_id].append((file_name, file_type))
        except Exception as e:
            logger.error(f"Error saving file: {e}")
        finally:
            conn.close()

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('DELETE FROM user_files WHERE user_id = ? AND file_name = ?', (user_id, file_name))
            conn.commit()
            if user_id in user_files:
                user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
                if not user_files[user_id]:
                    del user_files[user_id]
        except Exception as e:
            logger.error(f"Error removing file: {e}")
        finally:
            conn.close()

def add_active_user(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute('INSERT OR IGNORE INTO active_users (user_id) VALUES (?)', (user_id,))
            conn.commit()
            active_users.add(user_id)
        except Exception as e:
            logger.error(f"Error adding active user: {e}")
        finally:
            conn.close()

# --- Menu Creation ---
def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    # Common buttons
    buttons = [
        types.InlineKeyboardButton('📢 Updates Channel', url=UPDATE_CHANNEL),
        types.InlineKeyboardButton('📤 Upload File', callback_data='upload'),
        types.InlineKeyboardButton('📂 Check Files', callback_data='check_files'),
        types.InlineKeyboardButton('⚡ Bot Speed', callback_data='speed'),
        types.InlineKeyboardButton('📤 Send Command', callback_data='send_command'),
        types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}')
    ]
    
    if user_id in admin_ids:
        # Admin buttons
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3])
        markup.add(
            types.InlineKeyboardButton('💳 Subscriptions', callback_data='subscription'),
            types.InlineKeyboardButton('📢 Broadcast', callback_data='broadcast')
        )
        markup.add(
            types.InlineKeyboardButton('🔒 Lock Bot' if not bot_locked else '🔓 Unlock Bot',
                                     callback_data='lock_bot' if not bot_locked else 'unlock_bot'),
            types.InlineKeyboardButton('🟢 Run All Scripts', callback_data='run_all_scripts')
        )
        markup.add(buttons[4])  # Send Command
        markup.add(
            types.InlineKeyboardButton('👑 Admin Panel', callback_data='admin_panel'),
            types.InlineKeyboardButton('📊 Statistics', callback_data='stats')
        )
        markup.add(buttons[5])
    else:
        # User buttons
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3], types.InlineKeyboardButton('📊 Statistics', callback_data='stats'))
        markup.add(buttons[4])  # Send Command
        markup.add(buttons[5])
    
    return markup

def create_reply_keyboard_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    if user_id in admin_ids:
        layout = ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC
    else:
        layout = COMMAND_BUTTONS_LAYOUT_USER_SPEC
    
    for row_buttons_text in layout:
        markup.add(*[types.KeyboardButton(text) for text in row_buttons_text])
    
    return markup

# --- Command Handlers ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # Add to active users
    add_active_user(user_id)
    
    # Welcome message
    welcome_text = f"""
🤖 Welcome to File Host Bot!

🆔 Your ID: `{user_id}`
📁 Files Uploaded: {get_user_file_count(user_id)}/{get_user_file_limit(user_id)}

Features:
• Upload Python/JS scripts
• Auto install dependencies
• Run scripts in background
• Check logs and manage files

👇 Use buttons below or commands.
    """
    
    bot.send_message(
        chat_id,
        welcome_text,
        reply_markup=create_reply_keyboard_main_menu(user_id),
        parse_mode='Markdown'
    )

@bot.message_handler(commands=['upload'])
def upload_file(message):
    user_id = message.from_user.id
    
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot is locked by admin.")
        return
    
    limit = get_user_file_limit(user_id)
    current = get_user_file_count(user_id)
    
    if current >= limit:
        bot.reply_to(message, f"⚠️ File limit reached ({current}/{limit}). Delete some files first.")
        return
    
    bot.reply_to(message, "📤 Send your Python (.py), JavaScript (.js), or ZIP (.zip) file.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, "⚠️ Bot is locked.")
        return
    
    # Check file limit
    limit = get_user_file_limit(user_id)
    current = get_user_file_count(user_id)
    
    if current >= limit:
        bot.reply_to(message, f"⚠️ File limit reached ({current}/{limit}).")
        return
    
    document = message.document
    file_name = document.file_name
    
    if not file_name:
        bot.reply_to(message, "❌ File has no name.")
        return
    
    # Check file extension
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in ['.py', '.js', '.zip']:
        bot.reply_to(message, "❌ Only .py, .js, and .zip files are allowed.")
        return
    
    # Download file
    try:
        file_info = bot.get_file(document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        user_folder = get_user_folder(user_id)
        file_path = os.path.join(user_folder, file_name)
        
        with open(file_path, 'wb') as f:
            f.write(downloaded_file)
        
        # Save to database
        if ext == '.py':
            save_user_file(user_id, file_name, 'py')
            bot.reply_to(message, f"✅ Python file '{file_name}' saved successfully!")
        elif ext == '.js':
            save_user_file(user_id, file_name, 'js')
            bot.reply_to(message, f"✅ JavaScript file '{file_name}' saved successfully!")
        elif ext == '.zip':
            bot.reply_to(message, f"✅ ZIP file '{file_name}' saved. Extract manually.")
        
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        bot.reply_to(message, f"❌ Error: {str(e)}")

# --- Callback Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    user_id = call.from_user.id
    data = call.data
    
    try:
        if data == 'upload':
            upload_callback(call)
        elif data == 'check_files':
            check_files_callback(call)
        elif data == 'speed':
            speed_test(call)
        elif data == 'stats':
            show_stats(call)
        elif data == 'back_to_main':
            back_to_main(call)
        else:
            bot.answer_callback_query(call.id, "Coming soon...")
    except Exception as e:
        logger.error(f"Callback error: {e}")
        bot.answer_callback_query(call.id, "Error occurred")

def upload_callback(call):
    user_id = call.from_user.id
    limit = get_user_file_limit(user_id)
    current = get_user_file_count(user_id)
    
    if current >= limit:
        bot.answer_callback_query(
            call.id,
            f"File limit reached ({current}/{limit})",
            show_alert=True
        )
        return
    
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📤 Send your file (.py, .js, or .zip)")

def check_files_callback(call):
    user_id = call.from_user.id
    user_files_list = user_files.get(user_id, [])
    
    if not user_files_list:
        bot.answer_callback_query(call.id, "No files uploaded yet", show_alert=True)
        return
    
    files_text = "📂 Your Files:\n\n"
    for idx, (file_name, file_type) in enumerate(user_files_list, 1):
        files_text += f"{idx}. `{file_name}` ({file_type})\n"
    
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, files_text, parse_mode='Markdown')

def speed_test(call):
    start_time = time.time()
    msg = bot.send_message(call.message.chat.id, "Testing speed...")
    end_time = time.time()
    
    latency = round((end_time - start_time) * 1000, 2)
    
    bot.edit_message_text(
        f"⚡ Bot Speed Test\n\nLatency: {latency} ms",
        call.message.chat.id,
        msg.message_id
    )
    bot.answer_callback_query(call.id)

def show_stats(call):
    user_id = call.from_user.id
    
    stats_text = f"""
📊 Bot Statistics:

👥 Total Users: {len(active_users)}
📁 Total Files: {sum(len(files) for files in user_files.values())}
🟢 Running Scripts: {len(bot_scripts)}
🔒 Bot Status: {'Locked' if bot_locked else 'Unlocked'}
👤 Your Level: {'Admin' if user_id in admin_ids else 'User'}
    """
    
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, stats_text)

def back_to_main(call):
    user_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        "🏠 Main Menu",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=create_main_menu_inline(user_id)
    )

# --- Button Text Handlers ---
BUTTON_TEXT_HANDLERS = {
    "📢 Updates Channel": lambda msg: bot.reply_to(msg, f"Join our channel: {UPDATE_CHANNEL}"),
    "📤 Upload File": lambda msg: upload_file(msg),
    "📂 Check Files": lambda msg: check_files_handler(msg),
    "⚡ Bot Speed": lambda msg: speed_handler(msg),
    "📤 Send Command": lambda msg: bot.reply_to(msg, "This feature is under development"),
    "📞 Contact Owner": lambda msg: bot.reply_to(msg, f"Contact: {YOUR_USERNAME}"),
    "📊 Statistics": lambda msg: statistics_handler(msg),
}

@bot.message_handler(func=lambda message: message.text in BUTTON_TEXT_HANDLERS)
def handle_button(message):
    handler = BUTTON_TEXT_HANDLERS.get(message.text)
    if handler:
        handler(message)

def check_files_handler(message):
    user_id = message.from_user.id
    files_list = user_files.get(user_id, [])
    
    if not files_list:
        bot.reply_to(message, "📂 No files uploaded yet.")
        return
    
    text = "📂 Your Files:\n\n"
    for file_name, file_type in files_list:
        text += f"• `{file_name}` ({file_type})\n"
    
    bot.reply_to(message, text, parse_mode='Markdown')

def speed_handler(message):
    start = time.time()
    msg = bot.reply_to(message, "Testing speed...")
    end = time.time()
    
    latency = round((end - start) * 1000, 2)
    bot.edit_message_text(f"⚡ Latency: {latency} ms", message.chat.id, msg.message_id)

def statistics_handler(message):
    stats = f"""
📊 Statistics:
Users: {len(active_users)}
Files: {sum(len(files) for files in user_files.values())}
Running: {len(bot_scripts)}
    """
    bot.reply_to(message, stats)

# --- Cleanup ---
def cleanup():
    logger.info("Cleaning up...")
    for script_key, script_info in list(bot_scripts.items()):
        try:
            kill_process_tree(script_info)
        except:
            pass

atexit.register(cleanup)

# --- Main ---
if __name__ == '__main__':
    logger.info("🤖 Bot starting...")
    
    # Check token
    if TOKEN == 'YOUR_BOT_TOKEN_HERE':
        logger.error("❌ PLEASE SET YOUR BOT TOKEN IN ENVIRONMENT VARIABLES!")
        logger.error("On Render: Add TELEGRAM_BOT_TOKEN environment variable")
        exit(1)
    
    logger.info(f"Bot ID: {bot.get_me().id}")
    logger.info("Bot is running...")
    
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Polling error: {e}")
        time.sleep(5)
