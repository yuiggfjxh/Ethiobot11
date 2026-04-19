from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, send_from_directory
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut, NetworkError
import sqlite3
from datetime import datetime
import os
import threading
import asyncio
from collections import OrderedDict
import logging
from concurrent.futures import ThreadPoolExecutor
import time
import re

# ============= SIMPLE CACHE IMPLEMENTATION =============
class SimpleCache:
    def __init__(self, maxsize=100, ttl=60):
        self.maxsize = maxsize
        self.ttl = ttl
        self.cache = OrderedDict()
    
    def get(self, key):
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                self.cache.move_to_end(key)
                return value
            else:
                del self.cache[key]
        return None
    
    def __setitem__(self, key, value):
        if key in self.cache:
            del self.cache[key]
        elif len(self.cache) >= self.maxsize:
            self.cache.popitem(last=False)
        self.cache[key] = (value, time.time())
    
    def __contains__(self, key):
        return self.get(key) is not None
    
    def pop(self, key, default=None):
        if key in self.cache:
            del self.cache[key]
            return True
        return default
    
    def clear(self):
        self.cache.clear()

# Disable debug logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

# Thread pool for database operations
db_executor = ThreadPoolExecutor(max_workers=4)

# Create caches
user_cache = SimpleCache(maxsize=100, ttl=60)
task_cache = SimpleCache(maxsize=50, ttl=60)
balance_cache = SimpleCache(maxsize=200, ttl=30)

# ============= CONFIGURATION =============
app = Flask(__name__)
app.secret_key = "your_secret_key_here_12345"
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

ADMIN_PASSWORD = "admin123"
TOKEN = "8238129909:AAH_HB3Ht7xa12oe1ys4c3YUNelFRWtH3F8"
ADMIN_ID = 8294096366
MIN_WITHDRAW = 100
DB_NAME = "earn_bot.db"

# ============= DATABASE FUNCTIONS =============
def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA cache_size=10000')
    return conn

def init_db():
    conn = get_db()
    
    conn.execute('DROP TABLE IF EXISTS user_submissions')
    conn.execute('DROP TABLE IF EXISTS withdrawals')
    conn.execute('DROP TABLE IF EXISTS tasks')
    conn.execute('DROP TABLE IF EXISTS users')
    conn.execute('DROP TABLE IF EXISTS announcements')
    conn.execute('DROP TABLE IF EXISTS banned_users')
    
    conn.execute('''
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            balance INTEGER DEFAULT 0,
            total_earned INTEGER DEFAULT 0,
            joined_date TEXT,
            bank_name TEXT,
            bank_account_number TEXT,
            bank_account_name TEXT,
            telebirr_number TEXT,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT,
            ban_date TEXT
        )
    ''')
    
    conn.execute('''
        CREATE TABLE banned_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            reason TEXT,
            banned_date TEXT,
            banned_by TEXT
        )
    ''')
    
    conn.execute('''
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            reward INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            link TEXT NOT NULL,
            created_date TEXT,
            is_active INTEGER DEFAULT 1,
            total_completions INTEGER DEFAULT 0
        )
    ''')
    
    conn.execute('''
        CREATE TABLE user_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            task_id INTEGER,
            screenshot_path TEXT,
            status TEXT DEFAULT 'pending',
            submitted_date TEXT,
            reviewed_date TEXT,
            admin_notes TEXT,
            reward_amount INTEGER,
            cancelled_by_user INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    ''')
    
    conn.execute('''
        CREATE TABLE withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            phone_number TEXT,
            bank_name TEXT,
            bank_account_number TEXT,
            bank_account_name TEXT,
            status TEXT DEFAULT 'pending',
            request_date TEXT,
            processed_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    ''')
    
    conn.execute('''
        CREATE TABLE announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            image_path TEXT,
            created_date TEXT,
            sent_to_all INTEGER DEFAULT 0,
            send_date TEXT
        )
    ''')
    
    conn.execute('CREATE INDEX IF NOT EXISTS idx_submissions_user ON user_submissions(user_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_submissions_status ON user_submissions(status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_active ON tasks(is_active)')
    
    # Sample tasks
    sample_tasks = [
        ('Subscribe to YouTube Channel', 'Subscribe to our YouTube channel for tutorials', 15, 'youtube', 'https://youtube.com', datetime.now().isoformat(), 1, 0),
        ('Follow on TikTok', 'Follow our TikTok account', 10, 'tiktok', 'https://tiktok.com', datetime.now().isoformat(), 1, 0),
        ('Join Telegram', 'Join our Telegram channel', 20, 'telegram', 'https://t.me/example', datetime.now().isoformat(), 1, 0)
    ]
    
    for task in sample_tasks:
        conn.execute('INSERT INTO tasks (title, description, reward, task_type, link, created_date, is_active, total_completions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', task)
    
    conn.commit()
    conn.close()
    print("✅ Database initialized!")

def add_user(user_id, username, full_name):
    conn = get_db()
    existing = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not existing:
        conn.execute("INSERT INTO users (user_id, username, full_name, joined_date) VALUES (?, ?, ?, ?)",
                     (user_id, username, full_name, datetime.now().isoformat()))
        conn.commit()
        user_cache.pop(user_id, None)
        balance_cache.pop(user_id, None)
    conn.close()

def is_user_banned(user_id):
    conn = get_db()
    result = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return result and result[0] == 1

def ban_user(user_id, reason, admin_id):
    conn = get_db()
    user = conn.execute("SELECT username, full_name FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if user:
        conn.execute("UPDATE users SET is_banned = 1, ban_reason = ?, ban_date = ? WHERE user_id = ?", 
                     (reason, datetime.now().isoformat(), user_id))
        conn.execute("INSERT INTO banned_users (user_id, username, full_name, reason, banned_date, banned_by) VALUES (?, ?, ?, ?, ?, ?)",
                     (user_id, user['username'], user['full_name'], reason, datetime.now().isoformat(), str(admin_id)))
        conn.commit()
    conn.close()
    user_cache.clear()
    balance_cache.clear()

def unban_user(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET is_banned = 0, ban_reason = NULL, ban_date = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    user_cache.clear()
    balance_cache.clear()

def get_banned_users():
    conn = get_db()
    users = conn.execute("SELECT * FROM banned_users ORDER BY banned_date DESC").fetchall()
    conn.close()
    return users

def get_balance(user_id):
    cached = balance_cache.get(user_id)
    if cached is not None:
        return cached
    conn = get_db()
    result = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    balance = result[0] if result else 0
    balance_cache[user_id] = balance
    return balance

def update_balance(user_id, amount):
    conn = get_db()
    conn.execute("UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE user_id = ?",
                 (amount, amount, user_id))
    conn.commit()
    conn.close()
    balance_cache.pop(user_id, None)
    user_cache.pop(user_id, None)

def get_available_tasks(user_id):
    cache_key = f"tasks_{user_id}"
    cached = task_cache.get(cache_key)
    if cached is not None:
        return cached
    conn = get_db()
    tasks = conn.execute("""
        SELECT t.* FROM tasks t
        WHERE t.is_active = 1 
        AND t.id NOT IN (
            SELECT task_id FROM user_submissions 
            WHERE user_id = ? AND status IN ('approved', 'pending') AND cancelled_by_user = 0
        )
        ORDER BY t.id DESC LIMIT 50
    """, (user_id,)).fetchall()
    conn.close()
    task_cache[cache_key] = tasks
    return tasks

def get_task_by_id(task_id):
    cache_key = f"task_{task_id}"
    cached = task_cache.get(cache_key)
    if cached is not None:
        return cached
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    if task:
        task_cache[cache_key] = task
    return task

def submit_task(user_id, task_id, screenshot_path):
    conn = get_db()
    task = get_task_by_id(task_id)
    conn.execute("INSERT INTO user_submissions (user_id, task_id, screenshot_path, submitted_date, reward_amount, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                 (user_id, task_id, screenshot_path, datetime.now().isoformat(), task[3] if task else 0))
    conn.commit()
    conn.close()
    task_cache.pop(f"tasks_{user_id}", None)

def get_user_pending_submissions(user_id):
    cache_key = f"pending_{user_id}"
    cached = task_cache.get(cache_key)
    if cached is not None:
        return cached
    conn = get_db()
    submissions = conn.execute("""
        SELECT us.id, us.reward_amount, us.submitted_date, t.title
        FROM user_submissions us
        JOIN tasks t ON us.task_id = t.id
        WHERE us.user_id = ? AND us.status = 'pending' AND us.cancelled_by_user = 0
        ORDER BY us.submitted_date DESC LIMIT 20
    """, (user_id,)).fetchall()
    conn.close()
    task_cache[cache_key] = submissions
    return submissions

def get_user_stats(user_id):
    cache_key = f"stats_{user_id}"
    cached = user_cache.get(cache_key)
    if cached is not None:
        return cached
    conn = get_db()
    user = conn.execute("SELECT balance, total_earned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    completed = conn.execute("SELECT COUNT(*) FROM user_submissions WHERE user_id = ? AND status = 'approved'", (user_id,)).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM user_submissions WHERE user_id = ? AND status = 'pending' AND cancelled_by_user = 0", (user_id,)).fetchone()[0]
    conn.close()
    stats = {'balance': user[0] if user else 0, 'total_earned': user[1] if user else 0, 'completed_tasks': completed, 'pending_tasks': pending}
    user_cache[cache_key] = stats
    return stats

def cancel_submission(submission_id, user_id):
    conn = get_db()
    conn.execute("UPDATE user_submissions SET status='cancelled', cancelled_by_user=1 WHERE id=? AND user_id=? AND status='pending'", (submission_id, user_id))
    conn.commit()
    conn.close()
    task_cache.pop(f"pending_{user_id}", None)
    task_cache.pop(f"tasks_{user_id}", None)
    return True

def request_withdraw(user_id, amount, phone_number, bank_name, bank_account_number, bank_account_name):
    conn = get_db()
    conn.execute("INSERT INTO withdrawals (user_id, amount, phone_number, bank_name, bank_account_number, bank_account_name, request_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (user_id, amount, phone_number, bank_name, bank_account_number, bank_account_name, datetime.now().isoformat()))
    conn.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()
    balance_cache.pop(user_id, None)

def get_pending_submissions():
    conn = get_db()
    submissions = conn.execute("""
        SELECT us.*, u.username, u.full_name, t.title
        FROM user_submissions us
        JOIN users u ON us.user_id = u.user_id
        JOIN tasks t ON us.task_id = t.id
        WHERE us.status = 'pending' AND us.cancelled_by_user = 0
        ORDER BY us.submitted_date DESC LIMIT 50
    """).fetchall()
    conn.close()
    return submissions

def get_pending_withdrawals():
    conn = get_db()
    withdrawals = conn.execute("""
        SELECT w.*, u.username, u.full_name 
        FROM withdrawals w
        JOIN users u ON w.user_id = u.user_id
        WHERE w.status = 'pending'
        ORDER BY w.request_date DESC
    """).fetchall()
    conn.close()
    return withdrawals

def approve_submission(submission_id, admin_note=""):
    conn = get_db()
    sub = conn.execute("SELECT user_id, reward_amount FROM user_submissions WHERE id = ? AND status = 'pending'", (submission_id,)).fetchone()
    if sub:
        update_balance(sub[0], sub[1])
        conn.execute("UPDATE user_submissions SET status='approved', reviewed_date=?, admin_notes=? WHERE id=?", 
                     (datetime.now().isoformat(), admin_note, submission_id))
        conn.commit()
        conn.close()
        return sub[0], sub[1]
    conn.close()
    return None, 0

def reject_submission(submission_id, admin_note=""):
    conn = get_db()
    conn.execute("UPDATE user_submissions SET status='rejected', reviewed_date=?, admin_notes=? WHERE id=? AND status='pending'", 
                 (datetime.now().isoformat(), admin_note, submission_id))
    conn.commit()
    conn.close()

def approve_withdrawal(withdrawal_id):
    conn = get_db()
    conn.execute("UPDATE withdrawals SET status='approved', processed_date=? WHERE id=?", (datetime.now().isoformat(), withdrawal_id))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_db()
    users = conn.execute("SELECT * FROM users WHERE is_banned = 0 ORDER BY user_id DESC").fetchall()
    conn.close()
    return users

def add_task(title, description, reward, task_type, link):
    conn = get_db()
    conn.execute("INSERT INTO tasks (title, description, reward, task_type, link, created_date) VALUES (?, ?, ?, ?, ?, ?)",
                 (title, description, reward, task_type, link, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    task_cache.clear()

def delete_task(task_id):
    conn = get_db()
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    task_cache.clear()

def update_task(task_id, title, description, reward, task_type, link):
    conn = get_db()
    conn.execute("UPDATE tasks SET title=?, description=?, reward=?, task_type=?, link=? WHERE id=?",
                 (title, description, reward, task_type, link, task_id))
    conn.commit()
    conn.close()
    task_cache.clear()

def create_announcement(title, message):
    conn = get_db()
    conn.execute("INSERT INTO announcements (title, message, created_date) VALUES (?, ?, ?)",
                 (title, message, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_all_announcements():
    conn = get_db()
    announcements = conn.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return announcements

# ============= BOT HANDLERS =============
async def fast_reply(update, text, reply_markup=None):
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer(cache_time=60)
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            except:
                await update.callback_query.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        print(f"Reply error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    full_name = update.effective_user.full_name or ""
    
    def add():
        add_user(user_id, username, full_name)
    await asyncio.get_event_loop().run_in_executor(db_executor, add)
    
    welcome_text = f"""🌟 *Welcome {full_name}!* 🌟

🎯 *How to Earn:*
1️⃣ Click "📋 Tasks"
2️⃣ Complete the task (Subscribe/Follow/Join)
3️⃣ Send screenshot as proof
4️⃣ Get credited after admin approval

⚠️ *Important Rules:*
• ❌ DO NOT unfollow/unsubscribe after completing tasks
• 📸 Send clear screenshots showing completion
• 🔄 Each task can only be completed ONCE
• ⏳ Be patient - reviews take 24-48 hours

💰 *Withdrawal Info:*
• Minimum withdrawal: *{MIN_WITHDRAW} ETB*
• Methods: Telebirr or Bank Transfer
• Processing time: 24-72 hours

Let's start earning! 🚀"""

    keyboard = [[InlineKeyboardButton("📋 Tasks", callback_data="tasks")],
                [InlineKeyboardButton("💰 Balance", callback_data="balance")],
                [InlineKeyboardButton("📊 Stats", callback_data="stats")],
                [InlineKeyboardButton("⏳ Pending", callback_data="my_submissions")],
                [InlineKeyboardButton("📤 Withdraw", callback_data="withdraw")],
                [InlineKeyboardButton("ℹ️ Rules", callback_data="rules")]]
    
    await fast_reply(update, welcome_text, InlineKeyboardMarkup(keyboard))

async def rules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules_text = """📜 *Earn Bot Rules* 📜

1️⃣ *Task Completion Rules:*
• Complete tasks exactly as described
• Send CLEAR screenshots showing completion
• Include URL bar in screenshot when possible
• DO NOT send fake or edited screenshots

2️⃣ *Fair Play Policy:*
• Be honest in all submissions
• Follow task instructions carefully
• Respect other users and admins

3️⃣ *Withdrawal Info:*
• Minimum withdrawal: 100 ETB
• Provide correct payment details
• Processing takes 24-72 hours

4️⃣ *Support:*
• Contact admin for any issues
• Be patient with reviews"""

    keyboard = [[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]
    await fast_reply(update, rules_text, InlineKeyboardMarkup(keyboard))

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📋 Tasks", callback_data="tasks")],
                [InlineKeyboardButton("💰 Balance", callback_data="balance")],
                [InlineKeyboardButton("📊 Stats", callback_data="stats")],
                [InlineKeyboardButton("⏳ Pending", callback_data="my_submissions")],
                [InlineKeyboardButton("📤 Withdraw", callback_data="withdraw")],
                [InlineKeyboardButton("ℹ️ Rules", callback_data="rules")]]
    await fast_reply(update, "🎯 *Main Menu*\n\nSelect an option below:", InlineKeyboardMarkup(keyboard))

async def show_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def check_ban():
        return is_user_banned(user_id)
    
    if await asyncio.get_event_loop().run_in_executor(db_executor, check_ban):
        await fast_reply(update, "🚫 *You are BANNED!*\n\nContact admin for more information.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    def get_tasks():
        return get_available_tasks(user_id)
    
    tasks = await asyncio.get_event_loop().run_in_executor(db_executor, get_tasks)
    
    if not tasks:
        await fast_reply(update, "❌ *No tasks available!*\n\nAll tasks completed or none active. Check back later for new tasks!", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    keyboard = [[InlineKeyboardButton(f"🎯 {t[1][:30]} - {t[3]} ETB", callback_data=f"task_{t[0]}")] for t in tasks[:10]]
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await fast_reply(update, "📋 *Available Tasks*\n\nComplete tasks to earn rewards!", InlineKeyboardMarkup(keyboard))

async def task_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def check_ban():
        return is_user_banned(user_id)
    
    if await asyncio.get_event_loop().run_in_executor(db_executor, check_ban):
        await fast_reply(update, "🚫 *You are BANNED!*", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    task_id = int(update.callback_query.data.split("_")[1])
    
    def get_task():
        return get_task_by_id(task_id)
    
    task = await asyncio.get_event_loop().run_in_executor(db_executor, get_task)
    
    if not task:
        await fast_reply(update, "❌ Task not found!")
        return
    
    context.user_data['pending_task_id'] = task_id
    
    icons = {'youtube': '📺', 'tiktok': '📱', 'instagram': '📸', 'telegram': '✈️', 'facebook': '👍', 'other': '🔗'}
    icon = icons.get(task[4], '🔗')
    
    text = f"""{icon} *{task[1]}*\n\n📝 {task[2][:100]}\n\n💰 *Reward: {task[3]} ETB*\n\n🔗 [Click Here to Complete Task]({task[5]})\n\n⚠️ *IMPORTANT:*\n1. Click the link above and complete the task\n2. Take a CLEAR screenshot showing you completed it\n3. Send the screenshot here\n4. DO NOT unfollow/unsubscribe after!\n\n📸 Send your screenshot after completing the task!"""
    
    keyboard = [[InlineKeyboardButton("📸 Submit Screenshot", callback_data=f"upload_{task_id}")],
                [InlineKeyboardButton("🔙 Back to Tasks", callback_data="tasks")]]
    
    await fast_reply(update, text, InlineKeyboardMarkup(keyboard))

async def upload_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = int(update.callback_query.data.split("_")[1])
    context.user_data['pending_task_id'] = task_id
    await fast_reply(update, "📸 *Send your screenshot*\n\nMake sure the screenshot clearly shows you completed the task (subscription/follow).\n\nSend the image now:", 
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"task_{task_id}")]]))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def check_ban():
        return is_user_banned(user_id)
    
    if await asyncio.get_event_loop().run_in_executor(db_executor, check_ban):
        await fast_reply(update, "🚫 *You are BANNED!* You cannot submit tasks.")
        return
    
    task_id = context.user_data.get('pending_task_id')
    
    if not task_id:
        await fast_reply(update, "❌ *No task selected!*\n\nPlease select a task first from the Tasks menu.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Tasks", callback_data="tasks")]]))
        return
    
    photo = update.message.photo[-1]
    file = await photo.get_file()
    
    os.makedirs('screenshots', exist_ok=True)
    filename = f"{user_id}_{task_id}_{int(datetime.now().timestamp())}.jpg"
    file_path = os.path.join('screenshots', filename)
    await file.download_to_drive(file_path)
    
    def save():
        submit_task(user_id, task_id, file_path)
    
    await asyncio.get_event_loop().run_in_executor(db_executor, save)
    context.user_data.pop('pending_task_id', None)
    
    def get_task():
        return get_task_by_id(task_id)
    
    task = await asyncio.get_event_loop().run_in_executor(db_executor, get_task)
    
    await fast_reply(update, f"✅ *Screenshot Submitted!*\n\nTask: {task[1]}\nReward: {task[3]} ETB\n\n⏳ Admin will review your submission. You'll be credited once approved.\n\n📌 Please DO NOT unfollow/unsubscribe while waiting for approval!", 
                    InlineKeyboardMarkup([[InlineKeyboardButton("📋 More Tasks", callback_data="tasks")], 
                                         [InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def check_ban():
        return is_user_banned(user_id)
    
    if await asyncio.get_event_loop().run_in_executor(db_executor, check_ban):
        await fast_reply(update, "🚫 *You are BANNED!* Balance is frozen.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    def get_bal():
        return get_balance(user_id)
    
    balance = await asyncio.get_event_loop().run_in_executor(db_executor, get_bal)
    await fast_reply(update, f"💰 *Your Balance*\n\nCurrent Balance: *{balance} ETB*\n\nMinimum Withdrawal: *{MIN_WITHDRAW} ETB*\n\nNeed {MIN_WITHDRAW - balance if balance < MIN_WITHDRAW else 0} more ETB to withdraw", 
                    InlineKeyboardMarkup([[InlineKeyboardButton("📤 Withdraw", callback_data="withdraw")],
                                         [InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def check_ban():
        return is_user_banned(user_id)
    
    if await asyncio.get_event_loop().run_in_executor(db_executor, check_ban):
        await fast_reply(update, "🚫 *You are BANNED!*", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    def get_stats():
        return get_user_stats(user_id)
    
    stats = await asyncio.get_event_loop().run_in_executor(db_executor, get_stats)
    text = f"""📊 *Your Statistics*

💰 Balance: *{stats['balance']} ETB*
💵 Total Earned: *{stats['total_earned']} ETB*
✅ Completed Tasks: *{stats['completed_tasks']}*
⏳ Pending Review: *{stats['pending_tasks']}*

📌 Keep completing tasks to earn more!"""
    await fast_reply(update, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

async def my_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def get_pending():
        return get_user_pending_submissions(user_id)
    
    submissions = await asyncio.get_event_loop().run_in_executor(db_executor, get_pending)
    
    if not submissions:
        await fast_reply(update, "📭 *No pending submissions*\n\nAll your submissions have been processed or you haven't submitted any yet.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Tasks", callback_data="tasks")],
                                            [InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    keyboard = [[InlineKeyboardButton(f"❌ Cancel: {s[3][:20]} ({s[1]} ETB)", callback_data=f"cancel_{s[0]}")] for s in submissions[:5]]
    keyboard.append([InlineKeyboardButton("🔙 Menu", callback_data="menu")])
    await fast_reply(update, f"⏳ *{len(submissions)} Pending Submission(s)*\n\nClick to cancel a submission (no penalty):", 
                    InlineKeyboardMarkup(keyboard))

async def cancel_submission_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    submission_id = int(update.callback_query.data.split("_")[1])
    user_id = update.effective_user.id
    
    def cancel():
        cancel_submission(submission_id, user_id)
    
    await asyncio.get_event_loop().run_in_executor(db_executor, cancel)
    await fast_reply(update, "✅ *Submission cancelled!*\n\nYou can now retry this task if you want.", 
                    InlineKeyboardMarkup([[InlineKeyboardButton("📋 View Tasks", callback_data="tasks")],
                                         [InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))

async def withdraw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    def check_ban():
        return is_user_banned(user_id)
    
    if await asyncio.get_event_loop().run_in_executor(db_executor, check_ban):
        await fast_reply(update, "🚫 *You are BANNED!* Withdrawals are disabled for banned users.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    def get_bal():
        return get_balance(user_id)
    
    balance = await asyncio.get_event_loop().run_in_executor(db_executor, get_bal)
    
    if balance < MIN_WITHDRAW:
        await fast_reply(update, f"❌ *Insufficient Balance*\n\nYour balance: *{balance} ETB*\nMinimum withdrawal: *{MIN_WITHDRAW} ETB*\n\nNeed {MIN_WITHDRAW - balance} more ETB to withdraw.", 
                        InlineKeyboardMarkup([[InlineKeyboardButton("📋 Complete Tasks", callback_data="tasks")],
                                            [InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        return
    
    keyboard = [[InlineKeyboardButton("📱 Telebirr", callback_data="withdraw_telebirr")],
                [InlineKeyboardButton("🏦 Bank Transfer", callback_data="withdraw_bank")],
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")]]
    await fast_reply(update, f"💰 *Withdrawal Request*\n\nAmount: *{balance} ETB*\n\nSelect your withdrawal method:", 
                    InlineKeyboardMarkup(keyboard))

async def withdraw_telebirr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await fast_reply(update, "📱 *Telebirr Withdrawal*\n\nPlease send your details in this format:\n\n`Name | Phone Number`\n\nExample: `John Doe | 0912345678`\n\nPhone must be 10 digits starting with 09", 
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="withdraw")]]))
    context.user_data['withdraw_method'] = 'telebirr'

async def withdraw_bank_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await fast_reply(update, "🏦 *Bank Withdrawal*\n\nPlease send your bank details in this format:\n\n`Full Name | Bank Name | Account Number`\n\nExample: `John Doe | Commercial Bank of Ethiopia | 1000123456789`\n\nSupported banks: CBE, Awash, Dashen, Abyssinia, Hibret", 
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="withdraw")]]))
    context.user_data['withdraw_method'] = 'bank'

async def handle_withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    method = context.user_data.get('withdraw_method')
    
    if not method:
        return
    
    def get_bal():
        return get_balance(user_id)
    
    balance = await asyncio.get_event_loop().run_in_executor(db_executor, get_bal)
    
    if balance < MIN_WITHDRAW:
        await fast_reply(update, f"❌ Balance too low! Need {MIN_WITHDRAW} ETB")
        return
    
    if method == 'telebirr':
        # Parse format: Name | Phone
        parts = [p.strip() for p in update.message.text.split('|')]
        if len(parts) >= 2:
            full_name = parts[0]
            phone = parts[1]
            
            # Validate phone number
            if not re.match(r'^09[0-9]{8}$', phone):
                await fast_reply(update, "❌ *Invalid phone number!*\n\nPlease use format:\n`Name | 0912345678`\n\nPhone must start with 09 and be 10 digits", 
                                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))
                return
            
            if len(full_name) < 3:
                await fast_reply(update, "❌ *Invalid name!*\n\nPlease provide your full name.\n\nFormat: `Full Name | Phone Number`", 
                                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))
                return
            
            def save_telebirr():
                request_withdraw(user_id, balance, phone, None, None, full_name)
            
            await asyncio.get_event_loop().run_in_executor(db_executor, save_telebirr)
            context.user_data.pop('withdraw_method', None)
            await fast_reply(update, f"✅ *Withdrawal Request Submitted!*\n\n💰 Amount: *{balance} ETB*\n👤 Name: *{full_name}*\n📱 Phone: *{phone}*\n\n⏳ Processing time: 24-72 hours\nYou will receive a confirmation once processed.", 
                            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        else:
            await fast_reply(update, "❌ *Invalid format!*\n\nPlease use:\n`Full Name | Phone Number`\n\nExample: `John Doe | 0912345678`", 
                            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))
    
    elif method == 'bank':
        # Parse format: Name | Bank Name | Account Number
        parts = [p.strip() for p in update.message.text.split('|')]
        if len(parts) >= 3:
            full_name = parts[0]
            bank_name = parts[1]
            account_number = parts[2]
            
            # Basic validation
            if len(full_name) < 3:
                await fast_reply(update, "❌ *Invalid name!*\n\nPlease provide your full name.", 
                                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))
                return
            
            if len(bank_name) < 2:
                await fast_reply(update, "❌ *Invalid bank name!*\n\nPlease provide a valid bank name (CBE, Awash, Dashen, etc.)", 
                                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))
                return
            
            if len(account_number) < 5:
                await fast_reply(update, "❌ *Invalid account number!*\n\nPlease provide a valid bank account number.", 
                                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))
                return
            
            def save_bank():
                request_withdraw(user_id, balance, None, bank_name, account_number, full_name)
            
            await asyncio.get_event_loop().run_in_executor(db_executor, save_bank)
            context.user_data.pop('withdraw_method', None)
            await fast_reply(update, f"✅ *Withdrawal Request Submitted!*\n\n💰 Amount: *{balance} ETB*\n👤 Name: *{full_name}*\n🏦 Bank: *{bank_name}*\n🔢 Account: *{account_number}*\n\n⏳ Processing time: 24-72 hours\nYou will receive a confirmation once processed.", 
                            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]]))
        else:
            await fast_reply(update, "❌ *Invalid format!*\n\nPlease use:\n`Full Name | Bank Name | Account Number`\n\nExample: `John Doe | CBE | 1000123456789`", 
                            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Try Again", callback_data="withdraw")]]))

# Global variable for bot application
bot_application = None

# ============= FLASK ADMIN PANEL =============
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Earn Bot Admin Panel</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        
        .header { background: white; padding: 25px 30px; border-radius: 15px; margin-bottom: 25px; box-shadow: 0 5px 15px rgba(0,0,0,0.1); display: flex; justify-content: space-between; align-items: center; }
        .header h1 { color: #667eea; font-size: 24px; }
        .logout-btn { background: #f56565; color: white; padding: 10px 20px; border-radius: 8px; text-decoration: none; transition: 0.3s; }
        .logout-btn:hover { background: #e53e3e; }
        
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 25px; }
        .stat-card { background: white; padding: 20px; border-radius: 15px; text-align: center; box-shadow: 0 3px 10px rgba(0,0,0,0.1); cursor: pointer; transition: transform 0.3s; }
        .stat-card:hover { transform: translateY(-5px); }
        .stat-card h3 { color: #666; font-size: 14px; margin-bottom: 10px; }
        .stat-card .value { font-size: 28px; font-weight: bold; color: #667eea; }
        
        .nav { display: flex; gap: 10px; margin-bottom: 25px; flex-wrap: wrap; }
        .nav-btn { background: white; padding: 12px 24px; border: none; border-radius: 10px; cursor: pointer; font-size: 14px; font-weight: 600; transition: 0.3s; }
        .nav-btn:hover { transform: translateY(-2px); box-shadow: 0 3px 10px rgba(0,0,0,0.1); }
        .nav-btn.active { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        
        .card { background: white; border-radius: 15px; padding: 25px; margin-bottom: 25px; box-shadow: 0 3px 10px rgba(0,0,0,0.1); }
        .card-title { font-size: 20px; font-weight: bold; margin-bottom: 20px; color: #333; border-left: 4px solid #667eea; padding-left: 15px; }
        
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; font-weight: 600; color: #333; }
        .form-group input, .form-group select, .form-group textarea { width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px; font-size: 14px; transition: 0.3s; }
        .form-group input:focus, .form-group select:focus, .form-group textarea:focus { outline: none; border-color: #667eea; }
        
        .btn { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; transition: 0.3s; }
        .btn-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }
        .btn-success { background: #48bb78; color: white; }
        .btn-danger { background: #f56565; color: white; }
        .btn-warning { background: #ed8936; color: white; }
        .btn-sm { padding: 5px 12px; font-size: 12px; }
        .btn:hover { transform: translateY(-2px); }
        
        .table-container { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #e0e0e0; vertical-align: middle; }
        th { background: #f8f9fa; font-weight: 600; color: #333; }
        tr:hover { background: #f8f9fa; }
        
        .badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .badge-pending { background: #feebc8; color: #ed8936; }
        .badge-approved { background: #c6f6d5; color: #38a169; }
        .badge-rejected { background: #fed7d7; color: #e53e3e; }
        .badge-banned { background: #fc8181; color: #fff; }
        
        .submission-img { 
            max-width: 80px; 
            max-height: 80px; 
            border-radius: 8px; 
            cursor: pointer;
            object-fit: cover;
            border: 2px solid #e0e0e0;
            transition: transform 0.2s;
        }
        .submission-img:hover {
            transform: scale(1.05);
            border-color: #667eea;
        }
        
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); }
        .modal-content { background: white; margin: 5% auto; padding: 20px; border-radius: 15px; width: 90%; max-width: 800px; text-align: center; }
        .modal-content img { max-width: 100%; max-height: 70vh; border-radius: 10px; }
        .close { float: right; font-size: 28px; cursor: pointer; color: #999; margin-top: -10px; margin-right: -10px; }
        .close:hover { color: #333; }
        
        .login-wrapper { min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .login-form { background: white; padding: 40px; border-radius: 20px; width: 400px; max-width: 90%; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
        .login-form h2 { margin-bottom: 20px; color: #667eea; }
        
        .task-youtube { background: #ff0000; color: white; padding: 4px 8px; border-radius: 5px; font-size: 12px; display: inline-block; }
        .task-tiktok { background: #000000; color: white; padding: 4px 8px; border-radius: 5px; font-size: 12px; display: inline-block; }
        .task-telegram { background: #0088cc; color: white; padding: 4px 8px; border-radius: 5px; font-size: 12px; display: inline-block; }
        .task-instagram { background: #e4405f; color: white; padding: 4px 8px; border-radius: 5px; font-size: 12px; display: inline-block; }
        .task-facebook { background: #1877f2; color: white; padding: 4px 8px; border-radius: 5px; font-size: 12px; display: inline-block; }
        .task-other { background: #718096; color: white; padding: 4px 8px; border-radius: 5px; font-size: 12px; display: inline-block; }
        
        .refresh-btn { background: #4299e1; color: white; padding: 8px 16px; border-radius: 8px; border: none; cursor: pointer; margin-left: 15px; }
        .alert-badge { background: #ff9800; color: white; border-radius: 50%; padding: 2px 8px; font-size: 12px; margin-left: 8px; }
        
        .success-msg { background: #d4edda; color: #155724; padding: 10px; border-radius: 8px; margin-bottom: 15px; }
        .error-msg { background: #f8d7da; color: #721c24; padding: 10px; border-radius: 8px; margin-bottom: 15px; }
        
        .send-points-form { display: inline-flex; gap: 5px; margin-top: 5px; }
        .send-points-form input { width: 80px; padding: 4px; border: 1px solid #ddd; border-radius: 4px; }
        
        .announcement-list { margin-top: 30px; border-top: 2px solid #e0e0e0; padding-top: 20px; }
        .announcement-item { background: #f8f9fa; padding: 15px; border-radius: 10px; margin-bottom: 10px; }
        .announcement-item h4 { color: #667eea; margin-bottom: 8px; }
        .announcement-item p { color: #555; }
        .announcement-item small { color: #999; font-size: 11px; }
    </style>
</head>
<body>
    {% if not session.logged_in %}
    <div class="login-wrapper">
        <div class="login-form">
            <h2>🔐 Admin Login</h2>
            <form method="POST" action="/login">
                <div class="form-group">
                    <input type="password" name="password" placeholder="Enter admin password" required style="width: 100%; padding: 12px; border: 2px solid #e0e0e0; border-radius: 8px;">
                </div>
                <button type="submit" class="btn btn-primary" style="width: 100%;">Login</button>
            </form>
            {% if error %}
            <div class="error-msg" style="margin-top: 15px;">{{ error }}</div>
            {% endif %}
        </div>
    </div>
    {% else %}
    <div class="container">
        <div class="header">
            <h1>🤖 Earn Bot Admin Panel</h1>
            <a href="/logout" class="logout-btn">🚪 Logout</a>
        </div>
        
        <div class="nav">
            <button class="nav-btn active" onclick="showPage('dashboard')">📊 Dashboard</button>
            <button class="nav-btn" onclick="showPage('submissions')" id="submissionsBtn">📸 Submissions <span id="pendingBadge" style="display:none;" class="alert-badge">0</span></button>
            <button class="nav-btn" onclick="showPage('tasks')">📋 Tasks</button>
            <button class="nav-btn" onclick="showPage('addtask')">➕ Add Task</button>
            <button class="nav-btn" onclick="showPage('withdrawals')">💰 Withdrawals</button>
            <button class="nav-btn" onclick="showPage('users')">👥 Users</button>
            <button class="nav-btn" onclick="showPage('banned')">🚫 Banned Users</button>
            <button class="nav-btn" onclick="showPage('announcements')">📢 Announcements</button>
        </div>
        
        <div id="dashboard">
            <div class="stats-grid" id="stats"></div>
            <div class="card">
                <div class="card-title">📈 Recent Users</div>
                <div id="recentUsers"></div>
            </div>
        </div>
        
        <div id="submissions" style="display:none;">
            <div class="card">
                <div class="card-title">
                    📸 Pending Submissions
                    <button class="refresh-btn" onclick="loadSubmissions()">🔄 Refresh</button>
                </div>
                <div id="submissionsList"></div>
            </div>
        </div>
        
        <div id="tasks" style="display:none;">
            <div class="card">
                <div class="card-title">📋 All Tasks</div>
                <div id="tasksList"></div>
            </div>
        </div>
        
        <div id="addtask" style="display:none;">
            <div class="card">
                <div class="card-title">➕ Create New Task</div>
                <div id="taskMessage"></div>
                <form id="taskForm">
                    <div class="form-group">
                        <label>Task Type</label>
                        <select id="task_type" required>
                            <option value="youtube">📺 YouTube</option>
                            <option value="tiktok">📱 TikTok</option>
                            <option value="instagram">📸 Instagram</option>
                            <option value="telegram">✈️ Telegram</option>
                            <option value="facebook">👍 Facebook</option>
                            <option value="other">🔗 Other</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Title</label>
                        <input type="text" id="title" required>
                    </div>
                    <div class="form-group">
                        <label>Description</label>
                        <textarea id="description" rows="3" required></textarea>
                    </div>
                    <div class="form-group">
                        <label>Reward (ETB)</label>
                        <input type="number" id="reward" min="1" required>
                    </div>
                    <div class="form-group">
                        <label>Link</label>
                        <input type="url" id="link" required>
                    </div>
                    <button type="submit" class="btn btn-primary">✅ Create Task</button>
                </form>
            </div>
        </div>
        
        <div id="withdrawals" style="display:none;">
            <div class="card">
                <div class="card-title">💰 Withdrawal Requests</div>
                <div id="withdrawalsList"></div>
            </div>
        </div>
        
        <div id="users" style="display:none;">
            <div class="card">
                <div class="card-title">👥 All Users</div>
                <div id="usersList"></div>
            </div>
        </div>
        
        <div id="banned" style="display:none;">
            <div class="card">
                <div class="card-title">🚫 Banned Users</div>
                <div id="bannedList"></div>
            </div>
        </div>
        
        <div id="announcements" style="display:none;">
            <div class="card">
                <div class="card-title">📢 Send Announcement</div>
                <div id="announcementMsg"></div>
                <form id="announcementForm" onsubmit="sendAnnouncement(event)">
                    <div class="form-group">
                        <label>Title</label>
                        <input type="text" id="announcement_title" required>
                    </div>
                    <div class="form-group">
                        <label>Message</label>
                        <textarea id="announcement_message" rows="3" required></textarea>
                    </div>
                    <button type="submit" class="btn btn-primary" id="sendAnnounceBtn">📢 Send to All Users</button>
                </form>
                <div class="announcement-list">
                    <h3>📜 Recent Announcements</h3>
                    <div id="announcementHistory"></div>
                </div>
            </div>
        </div>
    </div>
    
    <div id="imageModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <img id="fullImage" src="" alt="Screenshot">
        </div>
    </div>
    
    <script>
        let currentUser = null;
        
        function showPage(page) {
            document.querySelectorAll('#dashboard, #submissions, #tasks, #addtask, #withdrawals, #users, #banned, #announcements').forEach(p => p.style.display = 'none');
            document.getElementById(page).style.display = 'block';
            
            if(page === 'dashboard') loadDashboard();
            else if(page === 'submissions') loadSubmissions();
            else if(page === 'tasks') loadTasks();
            else if(page === 'withdrawals') loadWithdrawals();
            else if(page === 'users') loadUsers();
            else if(page === 'banned') loadBannedUsers();
            else if(page === 'announcements') loadAnnouncementHistory();
        }
        
        async function loadDashboard() {
            try {
                const users = await fetch('/api/users').then(r => r.json());
                const tasks = await fetch('/api/tasks').then(r => r.json());
                const submissions = await fetch('/api/pending-submissions').then(r => r.json());
                const withdrawals = await fetch('/api/withdrawals').then(r => r.json());
                const banned = await fetch('/api/banned-users').then(r => r.json());
                
                const totalUsers = users.length;
                const totalEarned = users.reduce((s, u) => s + (u.total_earned || 0), 0);
                const pendingWithdrawals = withdrawals.filter(w => w.status === 'pending').reduce((s, w) => s + (w.amount || 0), 0);
                const pendingSubmissions = submissions.length;
                
                document.getElementById('stats').innerHTML = `
                    <div class="stat-card" onclick="showPage('users')"><h3>👥 Active Users</h3><div class="value">${totalUsers}</div></div>
                    <div class="stat-card" onclick="showPage('banned')"><h3>🚫 Banned Users</h3><div class="value">${banned.length}</div></div>
                    <div class="stat-card" onclick="showPage('withdrawals')"><h3>💰 Total Paid</h3><div class="value">${totalEarned} ETB</div></div>
                    <div class="stat-card" onclick="showPage('withdrawals')"><h3>⏳ Pending Withdrawals</h3><div class="value">${pendingWithdrawals} ETB</div></div>
                    <div class="stat-card" onclick="showPage('tasks')"><h3>📋 Total Tasks</h3><div class="value">${tasks.length}</div></div>
                    <div class="stat-card" onclick="showPage('submissions')"><h3>📸 Pending Reviews</h3><div class="value">${pendingSubmissions}</div></div>
                `;
                
                const recentUsers = users.slice(-5).reverse();
                document.getElementById('recentUsers').innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead><tr><th>ID</th><th>Username</th><th>Balance</th><th>Total Earned</th><th>Joined</th></tr></thead>
                            <tbody>${recentUsers.map(u => `<tr><td>${u.user_id}</td><td>${u.username || 'Anonymous'}</td><td>${u.balance || 0} ETB</td><td>${u.total_earned || 0} ETB</td><td>${u.joined_date ? new Date(u.joined_date).toLocaleDateString() : 'N/A'}</td></tr>`).join('')}</tbody>
                        </table>
                    </div>
                `;
            } catch(e) {
                console.error('Error loading dashboard:', e);
            }
        }
        
        async function loadSubmissions() {
            try {
                const submissions = await fetch('/api/pending-submissions').then(r => r.json());
                const badge = document.getElementById('pendingBadge');
                if (submissions.length > 0) {
                    badge.style.display = 'inline';
                    badge.textContent = submissions.length;
                } else {
                    badge.style.display = 'none';
                }
                
                if (submissions.length === 0) {
                    document.getElementById('submissionsList').innerHTML = '<div style="text-align:center; padding:40px;">✅ No pending submissions</div>';
                    return;
                }
                
                document.getElementById('submissionsList').innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>User</th>
                                    <th>Task</th>
                                    <th>Reward</th>
                                    <th>Screenshot</th>
                                    <th>Date</th>
                                    <th>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                ${submissions.map(s => {
                                    let imageUrl = '';
                                    let hasImage = false;
                                    if (s.screenshot_path) {
                                        let filename = s.screenshot_path.split('/').pop().split('\\\\').pop();
                                        imageUrl = `/screenshots/${filename}`;
                                        hasImage = true;
                                    }
                                    return `
                                    <tr>
                                        <td>#${s.id}</td>
                                        <td>
                                            <strong>${s.full_name || s.username || 'User'}</strong><br>
                                            <small>ID: ${s.user_id}</small>
                                        </td>
                                        <td>${s.title}</td>
                                        <td><strong>${s.reward_amount} ETB</strong></td>
                                        <td>
                                            ${hasImage ? 
                                                `<img src="${imageUrl}" class="submission-img" onclick="showImage('${imageUrl}')" onerror="this.style.display='none'">` : 
                                                '<span class="no-image">No image</span>'}
                                        </td>
                                        <td>${new Date(s.submitted_date).toLocaleString()}</td>
                                        <td>
                                            <button class="btn btn-success btn-sm" onclick="approve(${s.id})">✅ Approve</button>
                                            <button class="btn btn-danger btn-sm" onclick="reject(${s.id})">❌ Reject</button>
                                        </td>
                                    </tr>
                                `}).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } catch(e) {
                console.error('Error loading submissions:', e);
            }
        }
        
        async function loadTasks() {
            try {
                const tasks = await fetch('/api/tasks').then(r => r.json());
                document.getElementById('tasksList').innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead><tr><th>ID</th><th>Type</th><th>Title</th><th>Reward</th><th>Link</th><th>Actions</th></tr></thead>
                            <tbody>
                                ${tasks.map(t => `
                                    <tr>
                                        <td>#${t.id}</td>
                                        <td><span class="task-${t.task_type}">${t.task_type}</span></td>
                                        <td>${t.title}</td>
                                        <td><strong>${t.reward} ETB</strong></td>
                                        <td><a href="${t.link}" target="_blank" class="btn btn-sm">🔗 Visit</a></td>
                                        <td>
                                            <button class="btn btn-warning btn-sm" onclick="editTask(${t.id})">✏️ Edit</button>
                                            <button class="btn btn-danger btn-sm" onclick="deleteTask(${t.id})">🗑️ Delete</button>
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } catch(e) {
                console.error('Error loading tasks:', e);
            }
        }
        
        async function loadWithdrawals() {
            try {
                const withdrawals = await fetch('/api/withdrawals').then(r => r.json());
                document.getElementById('withdrawalsList').innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead><tr><th>ID</th><th>User</th><th>Amount</th><th>Account Details</th><th>Status</th><th>Date</th><th>Action</th></tr></thead>
                            <tbody>
                                ${withdrawals.map(w => `
                                    <tr>
                                        <td>#${w.id}</td>
                                        <td><strong>${w.full_name || w.username || w.user_id}</strong><br><small>ID: ${w.user_id}</small></td>
                                        <td><strong>${w.amount} ETB</strong></td>
                                        <td>
                                            ${w.phone_number ? `📱 ${w.phone_number}<br>👤 ${w.bank_account_name || 'N/A'}` : ''}
                                            ${w.bank_name ? `🏦 ${w.bank_name}<br>🔢 ${w.bank_account_number}<br>👤 ${w.bank_account_name || 'N/A'}` : ''}
                                        </td>
                                        <td><span class="badge badge-${w.status}">${w.status}</span></td>
                                        <td>${w.request_date ? new Date(w.request_date).toLocaleDateString() : 'N/A'}</td>
                                        <td>${w.status === 'pending' ? `<button class="btn btn-success btn-sm" onclick="approveWithdrawal(${w.id})">Approve</button>` : '-'}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } catch(e) {
                console.error('Error loading withdrawals:', e);
            }
        }
        
        async function loadUsers() {
            try {
                const users = await fetch('/api/users').then(r => r.json());
                document.getElementById('usersList').innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead><tr><th>ID</th><th>Username</th><th>Full Name</th><th>Balance</th><th>Total Earned</th><th>Payment Info</th><th>Actions</th><th>Send Points</th></tr></thead>
                            <tbody>
                                ${users.map(u => `
                                    <tr>
                                        <td>${u.user_id}</td>
                                        <td>${u.username || 'N/A'}</td>
                                        <td>${u.full_name || 'N/A'}</td>
                                        <td><strong>${u.balance || 0} ETB</strong></td>
                                        <td>${u.total_earned || 0} ETB</td>
                                        <td>
                                            ${u.telebirr_number ? `📱 ${u.telebirr_number}` : ''}
                                            ${u.bank_name ? `🏦 ${u.bank_name}` : ''}
                                            ${!u.telebirr_number && !u.bank_name ? 'Not set' : ''}
                                        </td>
                                        <td>
                                            <button class="btn btn-danger btn-sm" onclick="banUser(${u.user_id})">🚫 Ban</button>
                                        </td>
                                        <td>
                                            <div class="send-points-form">
                                                <input type="number" id="points_${u.user_id}" placeholder="Points" min="1" style="width:70px;">
                                                <button class="btn btn-success btn-sm" onclick="sendPoints(${u.user_id})">Send</button>
                                            </div>
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } catch(e) {
                console.error('Error loading users:', e);
            }
        }
        
        async function loadBannedUsers() {
            try {
                const banned = await fetch('/api/banned-users').then(r => r.json());
                if (banned.length === 0) {
                    document.getElementById('bannedList').innerHTML = '<div style="text-align:center; padding:40px;">✅ No banned users</div>';
                    return;
                }
                document.getElementById('bannedList').innerHTML = `
                    <div class="table-container">
                        <table>
                            <thead><tr><th>ID</th><th>Username</th><th>Full Name</th><th>Reason</th><th>Banned Date</th><th>Action</th></tr></thead>
                            <tbody>
                                ${banned.map(b => `
                                    <tr>
                                        <td>${b.user_id}</td>
                                        <td>${b.username || 'N/A'}</td>
                                        <td>${b.full_name || 'N/A'}</td>
                                        <td>${b.reason || 'Rule violation'}</td>
                                        <td>${b.banned_date ? new Date(b.banned_date).toLocaleString() : 'N/A'}</td>
                                        <td>
                                            <button class="btn btn-warning btn-sm" onclick="unbanUser(${b.user_id})">🔓 Unban</button>
                                        </td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                `;
            } catch(e) {
                console.error('Error loading banned users:', e);
            }
        }
        
        async function loadAnnouncementHistory() {
            try {
                const announcements = await fetch('/api/announcements-history').then(r => r.json());
                if (announcements.length === 0) {
                    document.getElementById('announcementHistory').innerHTML = '<div style="text-align:center; padding:20px;">No announcements yet</div>';
                    return;
                }
                document.getElementById('announcementHistory').innerHTML = announcements.map(a => `
                    <div class="announcement-item">
                        <h4>📢 ${a.title}</h4>
                        <p>${a.message}</p>
                        <small>📅 ${new Date(a.created_date).toLocaleString()}</small>
                    </div>
                `).join('');
            } catch(e) {
                console.error('Error loading announcements:', e);
                document.getElementById('announcementHistory').innerHTML = '<div class="error-msg">Failed to load announcement history</div>';
            }
        }
        
        document.getElementById('taskForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const data = {
                title: document.getElementById('title').value,
                description: document.getElementById('description').value,
                reward: parseInt(document.getElementById('reward').value),
                task_type: document.getElementById('task_type').value,
                link: document.getElementById('link').value
            };
            const res = await fetch('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
            if (res.ok) {
                document.getElementById('taskMessage').innerHTML = '<div class="success-msg">✅ Task created successfully!</div>';
                document.getElementById('taskForm').reset();
                setTimeout(() => document.getElementById('taskMessage').innerHTML = '', 3000);
                loadTasks();
            } else {
                document.getElementById('taskMessage').innerHTML = '<div class="error-msg">❌ Failed to create task!</div>';
                setTimeout(() => document.getElementById('taskMessage').innerHTML = '', 3000);
            }
        });
        
        async function sendAnnouncement(e) {
            e.preventDefault();
            const title = document.getElementById('announcement_title').value;
            const message = document.getElementById('announcement_message').value;
            
            if (!title || !message) {
                document.getElementById('announcementMsg').innerHTML = '<div class="error-msg">❌ Please fill in both title and message!</div>';
                setTimeout(() => document.getElementById('announcementMsg').innerHTML = '', 3000);
                return;
            }
            
            const btn = document.getElementById('sendAnnounceBtn');
            const originalText = btn.textContent;
            btn.textContent = '📢 Sending...';
            btn.disabled = true;
            
            try {
                const res = await fetch('/api/announcements', { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ title: title, message: message }) 
                });
                
                const result = await res.json();
                
                if (res.ok && result.success) {
                    document.getElementById('announcementMsg').innerHTML = '<div class="success-msg">✅ Announcement sent to all users successfully!</div>';
                    document.getElementById('announcementForm').reset();
                    loadAnnouncementHistory();
                    setTimeout(() => document.getElementById('announcementMsg').innerHTML = '', 5000);
                } else {
                    document.getElementById('announcementMsg').innerHTML = '<div class="error-msg">❌ Failed to send announcement: ' + (result.error || 'Unknown error') + '</div>';
                    setTimeout(() => document.getElementById('announcementMsg').innerHTML = '', 5000);
                }
            } catch(e) {
                console.error('Error sending announcement:', e);
                document.getElementById('announcementMsg').innerHTML = '<div class="error-msg">❌ Network error: ' + e.message + '</div>';
                setTimeout(() => document.getElementById('announcementMsg').innerHTML = '', 5000);
            } finally {
                btn.textContent = originalText;
                btn.disabled = false;
            }
        }
        
        async function approve(id) {
            const note = prompt('Admin note (optional):');
            const res = await fetch('/api/approve-submission', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ submission_id: id, admin_note: note || '' }) });
            if (res.ok) { 
                alert('✅ Submission approved! User has been credited.');
                loadSubmissions(); 
                loadDashboard(); 
            }
        }
        
        async function reject(id) {
            const reason = prompt('Rejection reason (required):', 'Screenshot not clear or task not completed properly');
            if (reason) {
                const res = await fetch('/api/reject-submission', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ submission_id: id, admin_note: reason }) });
                if (res.ok) { 
                    alert('❌ Submission rejected'); 
                    loadSubmissions(); 
                    loadDashboard(); 
                }
            }
        }
        
        async function approveWithdrawal(id) {
            if (confirm('Approve this withdrawal? User will receive the amount.')) {
                const res = await fetch('/api/approve-withdrawal', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ withdrawal_id: id }) });
                if (res.ok) {
                    alert('✅ Withdrawal approved!');
                    loadWithdrawals();
                    loadDashboard();
                }
            }
        }
        
        async function sendPoints(userId) {
            const pointsInput = document.getElementById(`points_${userId}`);
            const points = pointsInput.value;
            if (!points || points < 1) {
                alert('Please enter valid points amount (minimum 1)');
                return;
            }
            const res = await fetch('/api/send-points', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json' }, 
                body: JSON.stringify({ user_id: userId, points: parseInt(points) }) 
            });
            if (res.ok) {
                alert(`✅ Sent ${points} points to user ${userId}`);
                pointsInput.value = '';
                loadUsers();
                loadDashboard();
            } else {
                alert('❌ Failed to send points');
            }
        }
        
        async function banUser(userId) {
            const reason = prompt('Ban reason:', 'Rule violation');
            if (reason) {
                if (confirm(`⚠️ WARNING: This will permanently ban user ${userId}. They will lose all balance. Continue?`)) {
                    const res = await fetch('/api/ban-user', { 
                        method: 'POST', 
                        headers: { 'Content-Type': 'application/json' }, 
                        body: JSON.stringify({ user_id: userId, reason: reason }) 
                    });
                    if (res.ok) {
                        alert(`✅ User ${userId} has been banned!`);
                        loadUsers();
                        loadBannedUsers();
                        loadDashboard();
                    } else {
                        alert('❌ Failed to ban user');
                    }
                }
            }
        }
        
        async function unbanUser(userId) {
            if (confirm(`Unban user ${userId}? They will regain access to their account.`)) {
                const res = await fetch('/api/unban-user', { 
                    method: 'POST', 
                    headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ user_id: userId }) 
                });
                if (res.ok) {
                    alert(`✅ User ${userId} has been unbanned!`);
                    loadUsers();
                    loadBannedUsers();
                    loadDashboard();
                } else {
                    alert('❌ Failed to unban user');
                }
            }
        }
        
        async function editTask(id) {
            const tasks = await fetch('/api/tasks').then(r => r.json());
            const task = tasks.find(t => t.id === id);
            if (task) {
                const title = prompt('New title:', task.title);
                if (title) {
                    const desc = prompt('New description:', task.description);
                    const reward = prompt('New reward (ETB):', task.reward);
                    const link = prompt('New link:', task.link);
                    if (desc && reward && link) {
                        const res = await fetch('/api/tasks', { 
                            method: 'PUT', 
                            headers: { 'Content-Type': 'application/json' }, 
                            body: JSON.stringify({ 
                                id, 
                                title, 
                                description: desc, 
                                reward: parseInt(reward), 
                                task_type: task.task_type, 
                                link 
                            }) 
                        });
                        if (res.ok) {
                            alert('✅ Task updated!');
                            loadTasks();
                        } else {
                            alert('❌ Failed to update task');
                        }
                    }
                }
            }
        }
        
        async function deleteTask(id) {
            if (confirm('⚠️ Delete this task permanently? This action cannot be undone.')) {
                const res = await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
                if (res.ok) {
                    alert('✅ Task deleted!');
                    loadTasks();
                } else {
                    alert('❌ Failed to delete task');
                }
            }
        }
        
        function showImage(url) {
            document.getElementById('fullImage').src = url;
            document.getElementById('imageModal').style.display = 'block';
        }
        
        function closeModal() {
            document.getElementById('imageModal').style.display = 'none';
        }
        
        window.onclick = function(event) {
            const modal = document.getElementById('imageModal');
            if (event.target == modal) {
                modal.style.display = 'none';
            }
        }
        
        setInterval(() => {
            if (document.getElementById('submissions').style.display !== 'none') loadSubmissions();
            if (document.getElementById('dashboard').style.display !== 'none') loadDashboard();
        }, 15000);
        
        loadDashboard();
    </script>
    {% endif %}
</body>
</html>
"""

# ============= FLASK ROUTES =============
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(ADMIN_TEMPLATE, error="Invalid password", session=session)
    return render_template_string(ADMIN_TEMPLATE, session=session)

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template_string(ADMIN_TEMPLATE, session=session)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    return send_from_directory('screenshots', filename)

@app.route('/api/users')
def api_users():
    users = get_all_users()
    return jsonify([dict(u) for u in users])

@app.route('/api/banned-users')
def api_banned_users():
    users = get_banned_users()
    return jsonify([dict(u) for u in users])

@app.route('/api/announcements-history')
def api_announcements_history():
    announcements = get_all_announcements()
    return jsonify([dict(a) for a in announcements])

@app.route('/api/tasks')
def api_tasks():
    conn = get_db()
    tasks = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(t) for t in tasks])

@app.route('/api/tasks', methods=['POST'])
@login_required
def api_create_task():
    data = request.json
    add_task(data['title'], data['description'], data['reward'], data['task_type'], data['link'])
    return jsonify({"success": True})

@app.route('/api/tasks', methods=['PUT'])
@login_required
def api_update_task():
    data = request.json
    update_task(data['id'], data['title'], data['description'], data['reward'], data['task_type'], data['link'])
    return jsonify({"success": True})

@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
@login_required
def api_delete_task(task_id):
    delete_task(task_id)
    return jsonify({"success": True})

@app.route('/api/pending-submissions')
def api_pending_submissions():
    submissions = get_pending_submissions()
    return jsonify([dict(s) for s in submissions])

@app.route('/api/approve-submission', methods=['POST'])
@login_required
def api_approve_submission():
    data = request.json
    user_id, reward = approve_submission(data['submission_id'], data.get('admin_note', ''))
    return jsonify({"success": True})

@app.route('/api/reject-submission', methods=['POST'])
@login_required
def api_reject_submission():
    data = request.json
    reject_submission(data['submission_id'], data.get('admin_note', ''))
    return jsonify({"success": True})

@app.route('/api/send-points', methods=['POST'])
@login_required
def api_send_points():
    data = request.json
    update_balance(data['user_id'], data['points'])
    return jsonify({"success": True})

@app.route('/api/ban-user', methods=['POST'])
@login_required
def api_ban_user():
    data = request.json
    ban_user(data['user_id'], data['reason'], session.get('admin_id', 'Admin'))
    return jsonify({"success": True})

@app.route('/api/unban-user', methods=['POST'])
@login_required
def api_unban_user():
    data = request.json
    unban_user(data['user_id'])
    return jsonify({"success": True})

@app.route('/api/withdrawals')
def api_withdrawals():
    withdrawals = get_pending_withdrawals()
    return jsonify([dict(w) for w in withdrawals])

@app.route('/api/approve-withdrawal', methods=['POST'])
@login_required
def api_approve_withdrawal():
    data = request.json
    approve_withdrawal(data['withdrawal_id'])
    return jsonify({"success": True})

@app.route('/api/announcements', methods=['POST'])
@login_required
def api_announcements():
    try:
        data = request.json
        title = data.get('title', '')
        message = data.get('message', '')
        
        if not title or not message:
            return jsonify({"success": False, "error": "Title and message are required"}), 400
        
        # Save to database
        create_announcement(title, message)
        
        # Send to all users asynchronously
        async def send_announce():
            conn = get_db()
            users = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
            conn.close()
            success_count = 0
            for user in users:
                try:
                    if bot_application:
                        await bot_application.bot.send_message(
                            user['user_id'], 
                            f"📢 *{title}*\n\n{message}", 
                            parse_mode="Markdown"
                        )
                        success_count += 1
                        await asyncio.sleep(0.05)
                except Exception as e:
                    print(f"Failed to send to {user['user_id']}: {e}")
            print(f"📢 Announcement sent to {success_count}/{len(users)} users")
        
        if bot_application:
            asyncio.create_task(send_announce())
        
        return jsonify({"success": True, "message": f"Announcement sent to all users"})
    
    except Exception as e:
        print(f"Error sending announcement: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ============= BOT SETUP =============
def setup_bot():
    application = ApplicationBuilder().token(TOKEN).connect_timeout(10.0).read_timeout(10.0).write_timeout(10.0).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(main_menu, pattern="^menu$"))
    application.add_handler(CallbackQueryHandler(rules_handler, pattern="^rules$"))
    application.add_handler(CallbackQueryHandler(show_tasks, pattern="^tasks$"))
    application.add_handler(CallbackQueryHandler(task_detail, pattern="^task_"))
    application.add_handler(CallbackQueryHandler(upload_screenshot, pattern="^upload_"))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(CallbackQueryHandler(my_submissions, pattern="^my_submissions$"))
    application.add_handler(CallbackQueryHandler(cancel_submission_handler, pattern="^cancel_"))
    application.add_handler(CallbackQueryHandler(balance_handler, pattern="^balance$"))
    application.add_handler(CallbackQueryHandler(stats_handler, pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(withdraw_handler, pattern="^withdraw$"))
    application.add_handler(CallbackQueryHandler(withdraw_telebirr_handler, pattern="^withdraw_telebirr$"))
    application.add_handler(CallbackQueryHandler(withdraw_bank_handler, pattern="^withdraw_bank$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_details))
    
    return application

def run_bot():
    global bot_application
    bot_application = setup_bot()
    print("🤖 Telegram bot is running...")
    bot_application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

# ============= MAIN =============
if __name__ == '__main__':
    init_db()
    os.makedirs('screenshots', exist_ok=True)
    
    print("=" * 50)
    print("🚀 Earn Bot System - Fully Working")
    print("=" * 50)
    print("📱 Admin Panel: http://localhost:5000")
    print("🔑 Password: admin123")
    print("=" * 50)
    print("✅ Features Added/Fixed:")
    print("   - Welcome message with withdrawal info (Min: 100 ETB)")
    print("   - Rules page with fair play policy")
    print("   - User ban/unban system (admin only)")
    print("   - Admin can send points to users")
    print("   - View all images in admin panel")
    print("   - Announcements page - FIXED! Now works properly")
    print("   - Withdrawal requires Name + Phone OR Name + Bank + Account")
    print("   - Users can complete social media tasks (Subscribe/Follow/Join)")
    print("=" * 50)
    print("📌 MINIMUM WITHDRAWAL: 100 ETB")
    print("=" * 50)
    
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    app.run(debug=False, host='0.0.0.0', port=5000, use_reloader=False, threaded=True)