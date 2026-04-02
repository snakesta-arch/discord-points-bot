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

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0"))
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "America/New_York")
DB_PATH = os.getenv("DB_PATH", "/data/points_bot.db")
LEADERBOARD_POST_HOURS = os.getenv("LEADERBOARD_POST_HOURS", "9,21")
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "1344514802898309220"))

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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_db_dir() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def init_db() -> None:
    ensure_db_dir()
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                name TEXT NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, name)
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


def normalize_date_input(value: str, *, end_of_day: bool = False) -> str:
    try:
        date_part = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Dates must be in YYYY-MM-DD format.") from exc

    tz = ZoneInfo(BOT_TIMEZONE)
    if end_of_day:
        dt = date_part.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
    else:
        dt = date_part.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    return dt.astimezone(timezone.utc).isoformat()


def utc_iso_to_display(iso_value: str) -> str:
    dt = datetime.fromisoformat(iso_value)
    return dt.astimezone(ZoneInfo(BOT_TIMEZONE)).strftime("%Y-%m-%d")


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
                utc_now_iso(),
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
                utc_now_iso(),
            ),
        )
        conn.commit()


def record_manual_adjustment(
    guild_id: int,
    awarded_user_id: int,
    moderator_user_id: int,
    points: int,
    reason: str,
    month_key: Optional[str] = None,
) -> None:
    effective_month = normalize_month_key(month_key)
    synthetic_message_id = f"manual-{moderator_user_id}-{awarded_user_id}-{datetime.now(timezone.utc).timestamp()}"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO score_events (
                guild_id, month_key, message_id, source_channel_id, category_tag,
                points, poster_user_id, awarded_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(guild_id),
                effective_month,
                synthetic_message_id,
                "manual_adjustment",
                reason,
                int(points),
                str(moderator_user_id),
                str(awarded_user_id),
                utc_now_iso(),
            ),
        )
        conn.commit()


def create_or_update_season(guild_id: int, name: str, start_at: str, end_at: Optional[str]) -> None:
    now = utc_now_iso()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM seasons WHERE guild_id = ? AND name = ?",
            (str(guild_id), name),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE seasons
                SET start_at = ?, end_at = ?, updated_at = ?
                WHERE guild_id = ? AND name = ?
                """,
                (start_at, end_at, now, str(guild_id), name),
            )
        else:
            conn.execute(
                """
                INSERT INTO seasons (guild_id, name, start_at, end_at, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (str(guild_id), name, start_at, end_at, now, now),
            )
        conn.commit()


def set_active_season(guild_id: int, name: str) -> bool:
    now = utc_now_iso()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM seasons WHERE guild_id = ? AND name = ?",
            (str(guild_id), name),
        ).fetchone()
        if not existing:
            return False
        conn.execute("UPDATE seasons SET is_active = 0, updated_at = ? WHERE guild_id = ?", (now, str(guild_id)))
        conn.execute(
            "UPDATE seasons SET is_active = 1, updated_at = ? WHERE guild_id = ? AND name = ?",
            (now, str(guild_id), name),
        )
        conn.commit()
        return True


def close_season(guild_id: int, name: str, end_at: Optional[str] = None) -> bool:
    now = utc_now_iso()
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM seasons WHERE guild_id = ? AND name = ?",
            (str(guild_id), name),
        ).fetchone()
        if not existing:
            return False
        effective_end = end_at or now
        conn.execute(
            "UPDATE seasons SET end_at = ?, is_active = 0, updated_at = ? WHERE guild_id = ? AND name = ?",
            (effective_end, now, str(guild_id), name),
        )
        conn.commit()
        return True


def get_active_season(guild_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM seasons WHERE guild_id = ? AND is_active = 1 ORDER BY updated_at DESC LIMIT 1",
            (str(guild_id),),
        ).fetchone()


def get_season_by_name(guild_id: int, name: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM seasons WHERE guild_id = ? AND name = ?",
            (str(guild_id), name),
        ).fetchone()


def list_seasons(guild_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM seasons WHERE guild_id = ? ORDER BY start_at DESC, id DESC",
            (str(guild_id),),
        ).fetchall()


def resolve_period(
    guild_id: int,
    *,
    season_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    if season_name:
        season = get_season_by_name(guild_id, season_name)
        if not season:
            raise ValueError(f"Season '{season_name}' was not found.")
        label = f"Season: {season['name']}"
        return label, str(season["start_at"]), str(season["end_at"]) if season["end_at"] else None

    if start_date or end_date:
        if not start_date or not end_date:
            raise ValueError("You must provide both start_date and end_date together.")
        start_at = normalize_date_input(start_date, end_of_day=False)
        end_at = normalize_date_input(end_date, end_of_day=True)
        if start_at > end_at:
            raise ValueError("start_date must be on or before end_date.")
        return f"Range: {start_date} to {end_date}", start_at, end_at

    active = get_active_season(guild_id)
    if active:
        label = f"Season: {active['name']}"
        return label, str(active["start_at"]), str(active["end_at"]) if active["end_at"] else None

    month_key = current_month_key()
    start_at = normalize_date_input(f"{month_key}-01", end_of_day=False)
    year, month = month_key.split("-")
    year_i, month_i = int(year), int(month)
    if month_i == 12:
        next_month = datetime(year_i + 1, 1, 1)
    else:
        next_month = datetime(year_i, month_i + 1, 1)
    next_month_start = next_month.strftime("%Y-%m-%d")
    end_at = normalize_date_input(next_month_start, end_of_day=False)
    return f"Month: {month_key}", start_at, end_at


def query_total_points(guild_id: int, user_id: int, start_at: Optional[str], end_at: Optional[str]) -> int:
    sql = """
        SELECT COALESCE(SUM(points), 0) AS total
        FROM score_events
        WHERE guild_id = ? AND awarded_user_id = ?
    """
    params: list[str] = [str(guild_id), str(user_id)]
    if start_at:
        sql += " AND created_at >= ?"
        params.append(start_at)
    if end_at:
        sql += " AND created_at <= ?"
        params.append(end_at)
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
        return int(row["total"] if row else 0)


def query_top_users(guild_id: int, start_at: Optional[str], end_at: Optional[str], limit: int = 10):
    sql = """
        SELECT awarded_user_id, SUM(points) AS total
        FROM score_events
        WHERE guild_id = ?
    """
    params: list[str] = [str(guild_id)]
    if start_at:
        sql += " AND created_at >= ?"
        params.append(start_at)
    if end_at:
        sql += " AND created_at <= ?"
        params.append(end_at)
    sql += " GROUP BY awarded_user_id ORDER BY total DESC, awarded_user_id ASC LIMIT ?"
    params.append(str(limit))
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def query_breakdown_rows(guild_id: int, start_at: Optional[str], end_at: Optional[str]):
    sql = """
        SELECT awarded_user_id, category_tag, SUM(points) AS total
        FROM score_events
        WHERE guild_id = ?
    """
    params: list[str] = [str(guild_id)]
    if start_at:
        sql += " AND created_at >= ?"
        params.append(start_at)
    if end_at:
        sql += " AND created_at <= ?"
        params.append(end_at)
    sql += " GROUP BY awarded_user_id, category_tag ORDER BY awarded_user_id ASC, category_tag ASC"
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


def query_event_rows(guild_id: int, start_at: Optional[str], end_at: Optional[str]):
    sql = """
        SELECT created_at, message_id, source_channel_id, poster_user_id, awarded_user_id, category_tag, points
        FROM score_events
        WHERE guild_id = ?
    """
    params: list[str] = [str(guild_id)]
    if start_at:
        sql += " AND created_at >= ?"
        params.append(start_at)
    if end_at:
        sql += " AND created_at <= ?"
        params.append(end_at)
    sql += " ORDER BY created_at ASC, id ASC"
    with get_db() as conn:
        return conn.execute(sql, params).fetchall()


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

    month_key = current_month_key()
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


def has_mod_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if MOD_ROLE_ID and any(role.id == MOD_ROLE_ID for role in member.roles):
        return True
    return False


def resolve_member_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"User {user_id}"


async def build_leaderboard_embed(guild: discord.Guild, label: str, start_at: Optional[str], end_at: Optional[str]) -> discord.Embed:
    rows = query_top_users(guild.id, start_at, end_at, limit=20)
    embed = discord.Embed(title=f"Leaderboard - {label}")

    if not rows:
        embed.description = "No points recorded for this period yet."
        return embed

    lines = []
    for idx, row in enumerate(rows, start=1):
        user_id = int(row["awarded_user_id"])
        total = int(row["total"])
        name = resolve_member_name(guild, user_id)
        lines.append(f"**{idx}.** {name} - **{total}**")

    embed.description = "\n".join(lines)
    return embed


def build_summary_csv(guild: discord.Guild, label: str, start_at: Optional[str], end_at: Optional[str]) -> bytes:
    rows = query_top_users(guild.id, start_at, end_at, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["rank", "player_name", "user_id", "period", "total_points"])

    for idx, row in enumerate(rows, start=1):
        user_id = int(row["awarded_user_id"])
        total = int(row["total"])
        writer.writerow([idx, resolve_member_name(guild, user_id), user_id, label, total])

    return output.getvalue().encode("utf-8")


def build_breakdown_csv(guild: discord.Guild, label: str, start_at: Optional[str], end_at: Optional[str]) -> bytes:
    rows = query_breakdown_rows(guild.id, start_at, end_at)
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
    writer.writerow(["player_name", "user_id", *sorted_categories, "total_points", "period"])

    for user_id in sorted(totals_by_user.keys(), key=lambda uid: resolve_member_name(guild, uid).lower()):
        row_totals = totals_by_user[user_id]
        values = [row_totals.get(category, 0) for category in sorted_categories]
        writer.writerow([
            resolve_member_name(guild, user_id),
            user_id,
            *values,
            sum(values),
            label,
        ])

    return output.getvalue().encode("utf-8")


def build_events_csv(guild: discord.Guild, label: str, start_at: Optional[str], end_at: Optional[str]) -> bytes:
    rows = query_event_rows(guild.id, start_at, end_at)
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
        "period",
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
            label,
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
@bot.tree.command(name="mypoints", description="Show your points for the active season, named season, or a date range.")
@app_commands.describe(
    player="Optional player to check",
    season="Optional season name. Defaults to active season if one exists",
    start_date="Optional start date in YYYY-MM-DD format",
    end_date="Optional end date in YYYY-MM-DD format",
)
async def mypoints(
    interaction: discord.Interaction,
    player: Optional[discord.Member] = None,
    season: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    try:
        label, start_at, end_at = resolve_period(
            interaction.guild.id,
            season_name=season,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    target = player or interaction.user
    total = query_total_points(interaction.guild.id, target.id, start_at, end_at)

    embed = discord.Embed(
        title="Points Lookup",
        description=f"**{target.display_name}** has **{total}** point(s) for **{label}**.",
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Show the leaderboard for the active season, named season, or a date range.")
@app_commands.describe(
    season="Optional season name. Defaults to active season if one exists",
    start_date="Optional start date in YYYY-MM-DD format",
    end_date="Optional end date in YYYY-MM-DD format",
)
async def leaderboard(
    interaction: discord.Interaction,
    season: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    try:
        label, start_at, end_at = resolve_period(
            interaction.guild.id,
            season_name=season,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    embed = await build_leaderboard_embed(interaction.guild, label, start_at, end_at)
    await interaction.response.send_message(embed=embed)


EXPORT_TYPE_CHOICES = [
    app_commands.Choice(name="summary", value="summary"),
    app_commands.Choice(name="breakdown", value="breakdown"),
    app_commands.Choice(name="events", value="events"),
]


@bot.tree.command(name="addpoints", description="Add points to a user for the active season, a named season, or a date range.")
@app_commands.describe(
    player="Player to receive points",
    value="How many points to add",
    reason="Reason label for the manual adjustment",
    month="Optional month bucket in YYYY-MM format for bookkeeping",
)
async def addpoints(
    interaction: discord.Interaction,
    player: discord.Member,
    value: int,
    reason: Optional[str] = None,
    month: Optional[str] = None,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    if value <= 0:
        await interaction.response.send_message("Value must be greater than 0.", ephemeral=True)
        return

    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    record_manual_adjustment(
        guild_id=interaction.guild.id,
        awarded_user_id=player.id,
        moderator_user_id=interaction.user.id,
        points=value,
        reason=reason or "manual_add",
        month_key=month_key,
    )

    label, start_at, end_at = resolve_period(interaction.guild.id)
    new_total = query_total_points(interaction.guild.id, player.id, start_at, end_at)
    await interaction.response.send_message(
        f"Added **{value}** point(s) to **{player.display_name}**. Current total for **{label}**: **{new_total}**.",
        ephemeral=True,
    )


@bot.tree.command(name="removepoints", description="Remove points from a user for the active season, a named season, or a date range.")
@app_commands.describe(
    player="Player to remove points from",
    value="How many points to remove",
    reason="Reason label for the manual adjustment",
    month="Optional month bucket in YYYY-MM format for bookkeeping",
)
async def removepoints(
    interaction: discord.Interaction,
    player: discord.Member,
    value: int,
    reason: Optional[str] = None,
    month: Optional[str] = None,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    if value <= 0:
        await interaction.response.send_message("Value must be greater than 0.", ephemeral=True)
        return

    try:
        month_key = normalize_month_key(month)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    record_manual_adjustment(
        guild_id=interaction.guild.id,
        awarded_user_id=player.id,
        moderator_user_id=interaction.user.id,
        points=-value,
        reason=reason or "manual_remove",
        month_key=month_key,
    )

    label, start_at, end_at = resolve_period(interaction.guild.id)
    new_total = query_total_points(interaction.guild.id, player.id, start_at, end_at)
    await interaction.response.send_message(
        f"Removed **{value}** point(s) from **{player.display_name}**. Current total for **{label}**: **{new_total}**.",
        ephemeral=True,
    )


@bot.tree.command(name="export", description="Export results for the active season, named season, or a date range.")
@app_commands.describe(
    export_type="summary, breakdown, or events",
    season="Optional season name. Defaults to active season if one exists",
    start_date="Optional start date in YYYY-MM-DD format",
    end_date="Optional end date in YYYY-MM-DD format",
)
@app_commands.choices(export_type=EXPORT_TYPE_CHOICES)
async def export_data(
    interaction: discord.Interaction,
    export_type: app_commands.Choice[str],
    season: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return

    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    try:
        label, start_at, end_at = resolve_period(
            interaction.guild.id,
            season_name=season,
            start_date=start_date,
            end_date=end_date,
        )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if export_type.value == "summary":
        data = build_summary_csv(interaction.guild, label, start_at, end_at)
        safe_label = label.replace(":", "").replace(" ", "_")
        filename = f"leaderboard_summary_{safe_label}.csv"
    elif export_type.value == "breakdown":
        data = build_breakdown_csv(interaction.guild, label, start_at, end_at)
        safe_label = label.replace(":", "").replace(" ", "_")
        filename = f"leaderboard_breakdown_{safe_label}.csv"
    else:
        data = build_events_csv(interaction.guild, label, start_at, end_at)
        safe_label = label.replace(":", "").replace(" ", "_")
        filename = f"leaderboard_events_{safe_label}.csv"

    discord_file = discord.File(io.BytesIO(data), filename=filename)
    await interaction.response.send_message(
        content=f"Export ready for **{label}** ({export_type.value}).",
        file=discord_file,
        ephemeral=True,
    )


@bot.tree.command(name="seasoncreate", description="Create or update a season.")
@app_commands.describe(
    name="Season name",
    start_date="Start date in YYYY-MM-DD format",
    end_date="Optional end date in YYYY-MM-DD format",
)
async def seasoncreate(
    interaction: discord.Interaction,
    name: str,
    start_date: str,
    end_date: Optional[str] = None,
):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    try:
        start_at = normalize_date_input(start_date, end_of_day=False)
        end_at = normalize_date_input(end_date, end_of_day=True) if end_date else None
        if end_at and start_at > end_at:
            raise ValueError("start_date must be on or before end_date.")
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    create_or_update_season(interaction.guild.id, name, start_at, end_at)
    await interaction.response.send_message(
        f"Saved season **{name}** with start **{start_date}**" + (f" and end **{end_date}**." if end_date else "."),
        ephemeral=True,
    )


@bot.tree.command(name="seasonsetactive", description="Set the active season.")
@app_commands.describe(name="Season name")
async def seasonsetactive(interaction: discord.Interaction, name: str):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    if not set_active_season(interaction.guild.id, name):
        await interaction.response.send_message(f"Season '{name}' was not found.", ephemeral=True)
        return

    await interaction.response.send_message(f"Set **{name}** as the active season.", ephemeral=True)


@bot.tree.command(name="seasonclose", description="Close a season and optionally set its end date.")
@app_commands.describe(
    name="Season name",
    end_date="Optional end date in YYYY-MM-DD format",
)
async def seasonclose(interaction: discord.Interaction, name: str, end_date: Optional[str] = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    try:
        end_at = normalize_date_input(end_date, end_of_day=True) if end_date else None
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if not close_season(interaction.guild.id, name, end_at=end_at):
        await interaction.response.send_message(f"Season '{name}' was not found.", ephemeral=True)
        return

    await interaction.response.send_message(
        f"Closed season **{name}**" + (f" with end date **{end_date}**." if end_date else "."),
        ephemeral=True,
    )


@bot.tree.command(name="seasonlist", description="List configured seasons.")
async def seasonlist(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command only works in a server.", ephemeral=True)
        return
    if not has_mod_access(interaction.user):
        await interaction.response.send_message(
            "You need administrator permission or the configured mod role to use this command.",
            ephemeral=True,
        )
        return

    seasons = list_seasons(interaction.guild.id)
    if not seasons:
        await interaction.response.send_message("No seasons have been configured yet.", ephemeral=True)
        return

    lines = []
    for season in seasons[:20]:
        start_str = utc_iso_to_display(str(season["start_at"]))
        end_str = utc_iso_to_display(str(season["end_at"])) if season["end_at"] else "open"
        active_marker = " [ACTIVE]" if int(season["is_active"]) == 1 else ""
        lines.append(f"**{season['name']}** - {start_str} to {end_str}{active_marker}")

    embed = discord.Embed(title="Configured Seasons", description="\n".join(lines))
    await interaction.response.send_message(embed=embed, ephemeral=True)


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

    for guild in bot.guilds:
        channel = guild.get_channel(LEADERBOARD_CHANNEL_ID)
        if channel is None:
            continue
        try:
            label, start_at, end_at = resolve_period(guild.id)
            embed = await build_leaderboard_embed(guild, label, start_at, end_at)
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
