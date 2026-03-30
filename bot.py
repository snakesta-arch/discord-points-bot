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

# Configure your scoring categories here.
# These use Discord ROLE IDs, so the bot awards points when those roles are mentioned in the post.
# Example message:
#   <@&1487884949787902063> @PlayerOne @PlayerTwo GG
#
# Each entry is:
# role_id: {"label": display name, "points": value}
POINT_TAGS = {
    1487884581259579462: {"label": "WZ Casual Big Map", "points": 2},
    1487884949787902063: {"label": "WZ Regular Big Map", "points": 3},
    1487885091710570546: {"label": "WZ Casual Resurg", "points": 1},
    1487885229245858003: {"label": "WZ Resurg Regular", "points": 2},
    1487885330626384114: {"label": "WZ Ranked", "points": 3},
    1487885402491719721: {"label": "Black Ops Royale", "points": 3},
    1487885478475731046: {"label": "MP Game", "points": 1},
    1487885554673651844: {"label": "MP Ranked", "points": 2},
}

POST_HOURS = {
    int(part.strip())
    for part in LEADERBOARD_POST_HOURS.split(",")
    if part.strip().isdigit() and 0 <= int(part.strip()) <= 23
}
if not POST_HOURS:
    POST_HOURS = {9, 21}


# ============================================================
# Database helpers
# ============================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS score_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                month_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                source_channel_id TEXT NOT NULL,
                category_tag TEXT NOT NULL,
                points INTEGER NOT NULL,
                poster_user_id TEXT NOT NULL,
                awarded_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(message_id, category_tag, awarded_user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                source_channel_id TEXT NOT NULL,
                processed_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def current_month_key() -> str:
    return datetime.now(ZoneInfo(BOT_TIMEZONE)).strftime("%Y-%m")


def normalize_month_key(month: Optional[str]) -> str:
    if not month:
        return current_month_key()
    try:
        parsed = datetime.strptime(month, "%Y-%m")
        return parsed.strftime("%Y-%m")
    except ValueError as exc:
        raise ValueError("Month must be in YYYY-MM format, e.g. 2026-03") from exc


def is_processed(message_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?",
            (str(message_id),),
        ).fetchone()
        return row is not None


def mark_processed(message_id: int, guild_id: int, source_channel_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_messages (message_id, guild_id, source_channel_id, processed_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(message_id),
                str(guild_id),
                str(source_channel_id),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def record_score_event(
    guild_id: int,
    message_id: int,
    source_channel_id: int,
    category_tag: str,
    points: int,
    poster_user_id: int,
    awarded_user_id: int,
    month_key: str,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO score_events (
                guild_id, month_key, message_id, source_channel_id, category_tag,
                points, poster_user_id, awarded_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(guild_id),
                month_key,
                str(message_id),
                str(source_channel_id),
                category_tag,
                points,
                str(poster_user_id),
                str(awarded_user_id),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def get_points_for_user(guild_id: int, user_id: int, month_key: str) -> int:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(points), 0) AS total
            FROM score_events
            WHERE guild_id = ? AND awarded_user_id = ? AND month_key = ?
            """,
            (str(guild_id), str(user_id), month_key),
        ).fetchone()
        return int(row["total"] if row else 0)


def get_top_users(guild_id: int, month_key: str, limit: int = 10):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT awarded_user_id, SUM(points) AS total
            FROM score_events
            WHERE guild_id = ? AND month_key = ?
            GROUP BY awarded_user_id
            ORDER BY total DESC, awarded_user_id ASC
            LIMIT ?
            """,
            (str(guild_id), month_key, limit),
        ).fetchall()
        return rows


def get_breakdown_rows(guild_id: int, month_key: str):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT awarded_user_id, category_tag, SUM(points) AS total
            FROM score_events
            WHERE guild_id = ? AND month_key = ?
            GROUP BY awarded_user_id, category_tag
            ORDER BY awarded_user_id ASC, category_tag ASC
            """,
            (str(guild_id), month_key),
        ).fetchall()
        return rows


def get_event_rows(guild_id: int, month_key: str):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT created_at, message_id, source_channel_id, poster_user_id, awarded_user_id, category_tag, points
            FROM score_events
            WHERE guild_id = ? AND month_key = ?
            ORDER BY created_at ASC, id ASC
            """,
            (str(guild_id), month_key),
        ).fetchall()
        return rows


# ============================================================
# Discord bot setup
# ============================================================
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def has_image_attachment(message: discord.Message) -> bool:
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return True
    return False


def extract_matching_category(message: discord.Message) -> Optional[tuple[str, int, int]]:
    for role in message.role_mentions:
        config = POINT_TAGS.get(role.id)
        if config:
            return role.name, int(config["points"]), role.id
    return None


def get_awarded_members(message: discord.Message) -> list[discord.Member]:
    unique: dict[int, discord.Member] = {}

    for member in message.mentions:
        if isinstance(member, discord.Member) and not member.bot:
            unique[member.id] = member

    if isinstance(message.author, discord.Member) and not message.author.bot:
        unique[message.author.id] = message.author

    return list(unique.values())


async def process_message_for_points(message: discord.Message) -> bool:
    if not message.guild:
        return False
    if message.author.bot:
        return False
    if message.channel.id != SOURCE_CHANNEL_ID:
        return False
    if is_processed(message.id):
        return False
    if not has_image_attachment(message):
        return False

    category = extract_matching_category(message)
    if not category:
        return False

    recipients = get_awarded_members(message)
    if not recipients:
        return False

    month_key = datetime.now(ZoneInfo(BOT_TIMEZONE)).strftime("%Y-%m")
    category_tag, points, _role_id = category

    for member in recipients:
        record_score_event(
            guild_id=message.guild.id,
            message_id=message.id,
            source_channel_id=message.channel.id,
            category_tag=category_tag,
            points=points,
            poster_user_id=message.author.id,
            awarded_user_id=member.id,
            month_key=month_key,
        )

    mark_processed(message.id, message.guild.id, message.channel.id)
    return True


def is_export_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.name == EXPORT_ROLE_NAME for role in member.roles)


def resolve_member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"User {user_id}"


async def build_leaderboard_embed(guild: discord.Guild, month_key: str) -> discord.Embed:
    rows = get_top_users(guild.id, month_key, limit=20)
    embed = discord.Embed(title=f"Leaderboard - {month_key}")

    if not rows:
        embed.description = "No points recorded for this month yet."
        return embed

    lines = []
    for idx, row in enumerate(rows, start=1):
        user_id = int(row["awarded_user_id"])
        total = int(row["total"])
        name = resolve_member_name(guild, user_id)
        lines.append(f"**{idx}.** {name} - **{total}**")

    embed.description = "\n".join(lines)
    return embed


def build_summary_csv(guild: discord.Guild, month_key: str) -> bytes:
    rows = get_top_users(guild.id, month_key, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rank", "player_name", "user_id", "month", "total_points"])

    for idx, row in enumerate(rows, start=1):
        user_id = int(row["awarded_user_id"])
        total = int(row["total"])
        writer.writerow([idx, resolve_member_name(guild, user_id), user_id, month_key, total])

    return output.getvalue().encode("utf-8")


def build_breakdown_csv(guild: discord.Guild, month_key: str) -> bytes:
    rows = get_breakdown_rows(guild.id, month_key)
    totals_by_user: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    categories = set()

    for row in rows:
        user_id = int(row["awarded_user_id"])
        category = str(row["category_tag"])
        total = int(row["total"])
        totals_by_user[user_id][category] = total
        categories.add(category)

    sorted_categories = sorted(categories)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["player_name", "user_id", *sorted_categories, "total_points", "month"])

    for user_id in sorted(totals_by_user.keys(), key=lambda uid: resolve_member_name(guild, uid).lower()):
        row_totals = totals_by_user[user_id]
        values = [row_totals.get(category, 0) for category in sorted_categories]
        writer.writerow([
            resolve_member_name(guild, user_id),
            user_id,
            *values,
            sum(values),
            month_key,
        ])

    return output.getvalue().encode("utf-8")


def build_events_csv(guild: discord.Guild, month_key: str) -> bytes:
    rows = get_event_rows(guild.id, month_key)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "created_at_utc",
        "message_id",
        "source_channel_id",
        "poster_name",
        "poster_user_id",
        "awarded_name",
        "awarded_user_id",
        "category_tag",
        "points",
        "month",
    ])

    for row in rows:
        poster_id = int(row["poster_user_id"])
        awarded_id = int(row["awarded_user_id"])
        writer.writerow([
            row["created_at"],
            row["message_id"],
            row["source_channel_id"],
            resolve_member_name(guild, poster_id),
            poster_id,
            resolve_member_name(guild, awarded_id),
            awarded_id,
            row["category_tag"],
            row["points"],
            month_key,
        ])

    return output.getvalue().encode("utf-8")


# ============================================================
# Events
# ============================================================
@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} guild command(s).")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global command(s).")
    except Exception as exc:
        print(f"Command sync failed: {exc}")

    if not leaderboard_task.is_running():
        leaderboard_task.start()

    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    processed = await process_message_for_points(message)
    if processed:
        try:
            category_tag, points, role_id = extract_matching_category(message)  # type: ignore[misc]
            recipients = get_awarded_members(message)
            mentions = ", ".join(member.mention for member in recipients)
            await message.reply(
                f"Recorded **{points}** point(s) for **{category_tag}** (<@&{role_id}>): {mentions}",
                mention_author=False,
            )
        except Exception as exc:
            print(f"Failed to send scoring reply for message {message.id}: {exc}")

    await bot.process_commands(message)


# ============================================================
# Slash commands
# ============================================================
@bot.tree.command(name="mypoints", description="Show your monthly points, or another player's.")
@app_commands.describe(
    player="Optional player to check",
    month="Optional month in YYYY-MM format. Defaults to current month",
)
async def mypoints(
    interaction: discord.Interaction,
    player: Optional[discord.Member] = None,
    month: Optional[str] = None,
):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    target = player or interaction.user
    total = get_points_for_user(interaction.guild.id, target.id, month_key)

    embed = discord.Embed(
        title="Points Lookup",
        description=f"**{target.display_name}** has **{total}** point(s) for **{month_key}**.",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show the monthly leaderboard.")
@app_commands.describe(month="Optional month in YYYY-MM format. Defaults to current month")
async def leaderboard(interaction: discord.Interaction, month: Optional[str] = None):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    embed = await build_leaderboard_embed(interaction.guild, month_key)
    await interaction.response.send_message(embed=embed)


EXPORT_TYPE_CHOICES = [
    app_commands.Choice(name="summary", value="summary"),
    app_commands.Choice(name="breakdown", value="breakdown"),
    app_commands.Choice(name="events", value="events"),
]


@bot.tree.command(name="export", description="Export monthly results as CSV.")
@app_commands.describe(
    month="Month in YYYY-MM format",
    export_type="summary, breakdown, or events",
)
@app_commands.choices(export_type=EXPORT_TYPE_CHOICES)
async def export_data(
    interaction: discord.Interaction,
    month: str,
    export_type: app_commands.Choice[str],
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if not is_export_admin(interaction.user):
        await interaction.response.send_message(
            f"You need administrator permission or the '{EXPORT_ROLE_NAME}' role to export data.",
            ephemeral=True,
        )
        return

    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if export_type.value == "summary":
        data = build_summary_csv(interaction.guild, month_key)
        filename = f"leaderboard_summary_{month_key}.csv"
    elif export_type.value == "breakdown":
        data = build_breakdown_csv(interaction.guild, month_key)
        filename = f"leaderboard_breakdown_{month_key}.csv"
    else:
        data = build_events_csv(interaction.guild, month_key)
        filename = f"leaderboard_events_{month_key}.csv"

    discord_file = discord.File(io.BytesIO(data), filename=filename)
    await interaction.response.send_message(
        content=f"Export ready for **{month_key}** ({export_type.value}).",
        file=discord_file,
        ephemeral=True,
    )


# ============================================================
# Scheduled leaderboard posts
# ============================================================
@tasks.loop(minutes=1)
async def leaderboard_task():
    now = datetime.now(ZoneInfo(BOT_TIMEZONE))
    if now.minute != 0:
        return
    if now.hour not in POST_HOURS:
        return

    month_key = now.strftime("%Y-%m")

    for guild in bot.guilds:
        channel = guild.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            continue
        try:
            embed = await build_leaderboard_embed(guild, month_key)
            await channel.send(embed=embed)
        except Exception as exc:
            print(f"Failed to post leaderboard for guild {guild.id}: {exc}")


@leaderboard_task.before_loop
async def before_leaderboard_task():
    await bot.wait_until_ready()


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable")
    if SOURCE_CHANNEL_ID == 0:
        raise RuntimeError("Missing SOURCE_CHANNEL_ID environment variable")
    if LEADERBOARD_CHANNEL_ID == 0:
        raise RuntimeError("Missing LEADERBOARD_CHANNEL_ID environment variable")

    bot.run(DISCORD_TOKEN)


# ============================================================
# Deployment notes
# ============================================================
# requirements.txt
# ----------------
# discord.py>=2.4.0
#
# .env example
# ------------
# DISCORD_TOKEN=your_bot_token_here
# GUILD_ID=123456789012345678
# SOURCE_CHANNEL_ID=123456789012345678
# LEADERBOARD_CHANNEL_ID=123456789012345678
# BOT_TIMEZONE=America/New_York
# DB_PATH=points_bot.db
# LEADERBOARD_POST_HOURS=9,21
# EXPORT_ROLE_NAME=Admin
#
# Dockerfile example
# ------------------
# FROM python:3.11-slim
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# CMD ["python", "discord_points_bot.py"]
#
# Ready-to-deploy checklist
# -------------------------
# 1. Create a Discord application + bot.
# 2. Enable MESSAGE CONTENT INTENT in the Discord developer portal.
# 3. Invite the bot with permissions to:
#    - View Channels
#    - Send Messages
#    - Read Message History
#    - Attach Files
#    - Use Application Commands
# 4. Set the env vars above.
# 5. Deploy to Railway / Render / Docker / VPS.
# 6. Post screenshots in the source channel using one category tag and player mentions.
#
# Example valid post
# ------------------
# Screenshot attached + caption:
# <@&1487884949787902063> @Jake @Alex @Sam
#
# Scoring outcome if WZ Regular Big Map = 3:
# - Jake gets 3
# - Alex gets 3
# - Sam gets 3
# - poster also gets 3 (unless already one of those tagged users)
#
# IMPORTANT:
# - The category must be a real Discord role mention, not plain text.
# - The numeric IDs in POINT_TAGS are role IDs from your server.
# - Users being awarded points should still be normal user mentions.
