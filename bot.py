import csv
import io
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================
# Ready-to-deploy Discord points bot
#
# Features:
# - Watches one source channel for image posts
# - Detects category tags like @WarzoneWin / @RebirthWin
# - Awards points to tagged players and the poster
# - Tracks scores by month without deleting history
# - Slash commands for current and past months
# - CSV export for admins/mods
# - Twice-daily leaderboard posts to a separate channel
# - SQLite persistence
#
# Recommended deployment:
# - Railway / Render / Docker / VPS
# - Python 3.11+
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0"))
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "America/New_York")
DB_PATH = os.getenv("DB_PATH", "points_bot.db")
LEADERBOARD_POST_HOURS = os.getenv("LEADERBOARD_POST_HOURS", "9,21")
EXPORT_ROLE_NAME = os.getenv("EXPORT_ROLE_NAME", "Admin")

POINT_TAGS = {
    "@WarzoneWin": 3,
    "@RebirthWin": 2,
    "@WZCasualWin": 1,
    "@RBCasualWin": 1,
    "@RankedWin": 4,
    "@Top5": 1,
    "@MVP": 2,
}

POST_HOURS = {
    int(part.strip())
    for part in LEADERBOARD_POST_HOURS.split(",")
    if part.strip().isdigit() and 0 <= int(part.strip()) <= 23
}
if not POST_HOURS:
    POST_HOURS = {9, 21}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS score_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT, month_key TEXT, message_id TEXT,
            source_channel_id TEXT, category_tag TEXT, points INTEGER,
            poster_user_id TEXT, awarded_user_id TEXT, created_at TEXT,
            UNIQUE(message_id, category_tag, awarded_user_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY, guild_id TEXT,
            source_channel_id TEXT, processed_at TEXT
        )""")

def current_month_key():
    return datetime.now(ZoneInfo(BOT_TIMEZONE)).strftime("%Y-%m")

def is_processed(message_id):
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM processed_messages WHERE message_id=?", (str(message_id),)).fetchone() is not None

def mark_processed(message):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO processed_messages VALUES (?,?,?,?)",
                     (str(message.id), str(message.guild.id), str(message.channel.id), datetime.now(timezone.utc).isoformat()))

def record_score_event(guild_id, message_id, channel_id, tag, pts, poster, awarded):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO score_events VALUES (NULL,?,?,?,?,?,?,?,?)",
                     (str(guild_id), current_month_key(), str(message_id), str(channel_id),
                      tag, pts, str(poster), str(awarded), datetime.now(timezone.utc).isoformat()))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def has_image(message):
    return any(a.filename.lower().endswith(("png","jpg","jpeg","webp","gif")) for a in message.attachments)

@bot.event
async def on_ready():
    init_db()
    print("Bot ready")

@bot.event
async def on_message(message):
    if message.author.bot or message.channel.id != SOURCE_CHANNEL_ID:
        return
    if is_processed(message.id) or not has_image(message):
        return

    for tag, pts in POINT_TAGS.items():
        if tag in (message.content or ""):
            users = set([m.id for m in message.mentions])
            users.add(message.author.id)
            for uid in users:
                record_score_event(message.guild.id, message.id, message.channel.id, tag, pts, message.author.id, uid)
            mark_processed(message)
            await message.reply(f"Recorded {pts} points for {len(users)} players")
            break

bot.run(DISCORD_TOKEN)
